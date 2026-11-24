"""Response serialization utilities."""

from __future__ import annotations

import copy
import datetime
import decimal
import enum
import inspect
import mimetypes
import types
import uuid
from collections.abc import AsyncGenerator as AsyncGeneratorABC
from collections.abc import AsyncIterable as AsyncIterableABC
from collections.abc import AsyncIterator as AsyncIteratorABC
from collections.abc import Callable
from collections.abc import Generator as GeneratorABC
from collections.abc import Iterable as IterableABC
from collections.abc import Iterator as IteratorABC
from functools import cache
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Union, get_args, get_origin

import msgspec
from django.db.models import QuerySet
from django.http import HttpResponse as DjangoHttpResponse
from django.http import HttpResponseRedirect as DjangoHttpResponseRedirect
from django.template.response import SimpleTemplateResponse, TemplateResponse
from pydantic import BaseModel
from pydantic.errors import PydanticSchemaGenerationError

from . import _json
from ._pydantic import ResponseFailure, compile_value_coercer, is_pydantic_model, names_pydantic_model
from ._pydantic import compile_queryset_reader as compile_pydantic_queryset_reader
from ._pydantic import compile_response_coercer as compile_pydantic_response_coercer
from ._pydantic import compile_response_encoder as compile_pydantic_response_encoder
from ._template_response import render_template_response
from .concurrency import run_in_orm_executor, run_orm_blocking
from .cookies import Cookie
from .exceptions import ResponseValidationError
from .responses import (
    HTML,
    JSON,
    EventSourceResponse,
    File,
    FileResponse,
    PlainText,
    Redirect,
    ServerSentEvent,
    StreamingResponse,
    format_sse_event,
)
from .responses import Response as ResponseClass

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from .typing import HandlerMetadata

# Type aliases for response formats
# Raw cookie tuple: (name, value, path, max_age, expires, domain, secure, httponly, samesite)
CookieTuple = tuple[str, str, str, int | None, str | None, str | None, bool, bool, str | None]

# ResponseMeta tuple: (response_type, custom_content_type, custom_headers, cookies)
# This is the new format for Rust-side header building
ResponseMetaTuple = tuple[
    str,  # response_type: "json", "html", "plaintext", etc.
    str | None,  # custom_content_type: override content-type or None
    list[tuple[str, str]] | None,  # custom_headers: [(key, value), ...] or None
    list[CookieTuple] | None,  # cookies: list of raw cookie tuples or None
]

# Integer body-kind tags sent to Rust (avoids String alloc per response in parse_response_wire).
# Must match the match arms in src/handler.rs parse_response_wire().
_BODY_BYTES: int = 0
_BODY_STREAM: int = 1
_BODY_FILE: int = 2

BodyKind = Literal[0, 1, 2]
ResponseWireV1 = tuple[int, ResponseMetaTuple | int, BodyKind, bytes | StreamingResponse | str]

# Bare-bytes wire fast path: _sync_executor variants (api.py) may return just the
# encoded JSON body (bytes) instead of a ResponseWireV1 tuple when the response
# is (route default status, JSON meta). Rust's sync dispatch rebuilds the
# envelope from RouteMetadata.default_status_code — see src/handler.rs.


def _build_response_meta(
    response_type: str, custom_headers: dict[str, str] | None, cookies: list[Cookie] | None
) -> ResponseMetaTuple:
    """Build metadata tuple for Rust header construction.

    Args:
        response_type: Type of response ("json", "html", "plaintext", etc.)
        custom_headers: Custom headers dict or None
        cookies: List of Cookie objects or None

    Returns:
        Tuple of (response_type, custom_content_type, custom_headers, cookies)
        suitable for Rust-side header building
    """
    custom_ct: str | None = None
    headers_list: list[tuple[str, str]] | None = None

    if custom_headers:
        # Extract custom content-type if provided
        headers_list = []
        for k, v in custom_headers.items():
            if k.lower() == "content-type":
                custom_ct = v
            else:
                # Don't lowercase here - Rust will do it
                headers_list.append((k, v))
        if not headers_list:
            headers_list = None

    # Convert cookies to raw tuples
    cookies_data: list[CookieTuple] | None = None
    if cookies:
        cookies_data = [c.to_raw_tuple() for c in cookies]

    return (response_type, custom_ct, headers_list, cookies_data)


def _wire_bytes(status: int, meta: ResponseMetaTuple | int, body: bytes) -> ResponseWireV1:
    return status, meta, _BODY_BYTES, body


def _wire_stream(status: int, meta: ResponseMetaTuple | int, stream: StreamingResponse) -> ResponseWireV1:
    return status, meta, _BODY_STREAM, stream


def _wire_file(status: int, meta: ResponseMetaTuple | int, path: str) -> ResponseWireV1:
    return status, meta, _BODY_FILE, path


# Pre-computed response metadata for common cases.
# Integer tags avoid per-response tuple parsing on the Rust side.
# Must match STATIC_META_* constants in src/response_meta.rs.
_RESPONSE_META_JSON: int = 0
_RESPONSE_META_PLAINTEXT: int = 1
_RESPONSE_META_OCTETSTREAM: int = 2
_RESPONSE_META_EMPTY: int = 3
_RESPONSE_META_HTML: int = 4
_TYPED_STREAM_MEDIA_TYPE = "application/x-ndjson"
_RAW_STREAM_CHUNK_TYPES = (bytes, bytearray, memoryview, str)
_STREAM_ANNOTATION_ORIGINS = {
    AsyncIterableABC,
    AsyncIteratorABC,
    AsyncGeneratorABC,
    IterableABC,
    IteratorABC,
    GeneratorABC,
}


_RESPONSE_INSTANCE_TYPES = (
    JSON,
    PlainText,
    HTML,
    Redirect,
    File,
    FileResponse,
    StreamingResponse,
    ResponseClass,
    DjangoHttpResponse,
)
_STATIC_RESPONSE_META = {
    "json": _RESPONSE_META_JSON,
    "plaintext": _RESPONSE_META_PLAINTEXT,
    "octetstream": _RESPONSE_META_OCTETSTREAM,
    "html": _RESPONSE_META_HTML,
}
# The JSON encoder encodes a Pydantic model with no response type. msgspec
# encodes its own Struct type too, so an unannotated handler can return one.
_ENCODED_MODEL_TYPES = (BaseModel, msgspec.Struct)
_VALIDATION_NOOP_TYPES = {dict, list, str, bytes, bytearray, type(None)}
# The scalar results that Bolt sends as JSON. A bool is an int. A str is
# text/plain and bytes are application/octet-stream. None is JSON only when
# the response type allows it (see _allows_none).
_JSON_SCALAR_TYPES = (
    int,
    float,
    decimal.Decimal,
    datetime.date,  # Also datetime.datetime.
    datetime.time,
    datetime.timedelta,
    uuid.UUID,
    enum.Enum,
)


# The response types with no validator, for which a str or bytes result is a mismatch.
_PLAIN_RESPONSE_TYPES = (dict, list, str, bytes, bytearray)


def _raise_plain_type_mismatch(expected: type, result: Any) -> NoReturn:
    _raise_response_validation_error(
        TypeError(
            f"Response type mismatch: expected {expected.__name__}, but handler returned {type(result).__name__}."
        )
    )


def _compile_none_handlers(
    data_handler: Callable[..., Any], data_handler_sync: Callable[..., Any]
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """Compile the data handlers of a route with the response type ``None``.

    Only ``None`` goes to the general data handlers of the route. Each other
    result is a response validation error. ``None`` has no validator, so the
    route keeps the executors of a route with no response validation.
    """

    def refuse(result: Any) -> NoReturn:
        _raise_response_validation_error(
            TypeError(f"Response type mismatch: expected None, but handler returned {type(result).__name__}.")
        )

    def handler_sync(result: Any, status_code: int | None = None) -> ResponseWireV1:
        if result is None:
            return data_handler_sync(result, status_code)
        refuse(result)

    async def handler(result: Any, status_code: int | None = None) -> ResponseWireV1:
        if result is None:
            return await data_handler(result, status_code)
        refuse(result)

    return handler, handler_sync


def _compile_plain_container_handlers(
    container: type,
    default_status_code: int,
    queryset_handler: Callable[..., Any],
    queryset_handler_sync: Callable[..., Any],
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """Compile the data handlers of a route with the bare response type ``dict`` or ``list``.

    ``-> dict`` takes a dict. ``-> list`` takes a list, a tuple or a QuerySet.
    Each other result is a response validation error. The type test of the
    correct result is the first test, so that result pays for no other test.
    The QuerySet handlers are the general data handlers of the route.
    """

    def refuse(result: Any, current_status: int) -> ResponseWireV1:
        if result is None and current_status == 204:
            return _wire_bytes(204, _RESPONSE_META_EMPTY, b"")
        _raise_plain_type_mismatch(container, result)

    if container is dict:

        def handler_sync(result: Any, status_code: int | None = None) -> ResponseWireV1:
            current_status = default_status_code if status_code is None else status_code
            if isinstance(result, dict):
                return _wire_bytes(current_status, _RESPONSE_META_JSON, _json.encode(result))
            return refuse(result, current_status)

        async def handler(result: Any, status_code: int | None = None) -> ResponseWireV1:
            return handler_sync(result, status_code)

        return handler, handler_sync

    def list_handler_sync(result: Any, status_code: int | None = None) -> ResponseWireV1:
        current_status = default_status_code if status_code is None else status_code
        if isinstance(result, list):
            return _wire_bytes(current_status, _RESPONSE_META_JSON, _json.encode(result))
        if isinstance(result, tuple):
            return _wire_bytes(current_status, _RESPONSE_META_JSON, _json.encode(list(result)))
        if isinstance(result, QuerySet):
            return queryset_handler_sync(result, status_code)
        return refuse(result, current_status)

    async def list_handler(result: Any, status_code: int | None = None) -> ResponseWireV1:
        if isinstance(result, QuerySet):
            return await queryset_handler(result, status_code)
        return list_handler_sync(result, status_code)

    return list_handler, list_handler_sync


def _raise_response_validation_error(exc: Exception) -> NoReturn:
    """Convert a low-level validation failure into ResponseValidationError.

    Handler returned data that doesn't match its declared response_model.
    Raising ResponseValidationError lets the central exception handler
    (`error_handlers.response_validation_error_handler`) emit a structured 500
    JSON response, log the bug, and apply debug/prod policy uniformly.
    """
    if isinstance(exc, ResponseFailure):
        raise ResponseValidationError(exc.errors) from exc
    raise ResponseValidationError([{"loc": ["response"], "msg": str(exc), "type": "validation_error"}]) from exc


def _build_wire_meta(
    response_type: str, custom_headers: dict[str, str] | None, cookies: list[Cookie] | None
) -> ResponseMetaTuple | int:
    if not custom_headers and not cookies:
        # The tuple below gives the same response for the types of the table. The
        # integer tag only saves the tuple. Each caller with no headers and no
        # cookies gives a type of the table.
        static_meta = _STATIC_RESPONSE_META.get(response_type)  # pragma: no mutate
        if static_meta is not None:  # pragma: no mutate
            return static_meta
    return _build_response_meta(response_type, custom_headers, cookies)


@cache
def _infer_wire_response_type(media_type: str) -> str:
    normalized = media_type.split(";", 1)[0].strip().lower()
    if normalized == "application/json" or normalized.endswith("+json"):
        return "json"
    if normalized == "text/plain":
        return "plaintext"
    if normalized == "text/html":
        return "html"
    return "octetstream"


@cache
def _is_json_media_type(media_type: str) -> bool:
    normalized = media_type.split(";", 1)[0].strip().lower()
    return normalized == "application/json" or normalized.endswith("+json")


def _render_response_body(content: Any, media_type: str) -> bytes:
    # Bytes-like content is a pre-encoded body. Pass it through verbatim,
    # even for JSON media types — JSON-encoding bytes would base64-wrap
    # them and corrupt pre-compressed payloads (issue #305).
    # One isinstance for the whole bytes-like family keeps the structured
    # content path to a single failed check.
    if isinstance(content, (bytes, bytearray, memoryview)):
        if content.__class__ is bytes:
            return content
        return bytes(content)
    if _is_json_media_type(media_type):
        return _json.encode(content)
    if isinstance(content, str):
        return content.encode()
    return str(content).encode()


# Media types whose exact string equals the static Rust content-type.
# These need no meta tuple — the integer tag already carries the header.
# Must match ResponseType::content_type() in crates/bolt-core/src/response_meta.rs.
_STATIC_MEDIA_TYPE_META = {
    "application/json": _RESPONSE_META_JSON,
    "application/octet-stream": _RESPONSE_META_OCTETSTREAM,
    "text/plain; charset=utf-8": _RESPONSE_META_PLAINTEXT,
    "text/html; charset=utf-8": _RESPONSE_META_HTML,
}


def _response_class_wire_meta(
    media_type: str, headers: dict[str, str] | None, cookies: list[Cookie] | None
) -> ResponseMetaTuple | int:
    """Build the wire meta for a Response, using its media type as content-type.

    A content-type in headers wins over the media type. The headers are read
    once — do not inject the content-type into them first.
    """
    if not headers and not cookies:
        # The tuple below gives the same response for these media types. The
        # integer tag only saves the tuple.
        static_meta = _STATIC_MEDIA_TYPE_META.get(media_type)  # pragma: no mutate
        if static_meta is not None:
            return static_meta
        return (_infer_wire_response_type(media_type), media_type, None, None)

    custom_ct = media_type
    headers_list: list[tuple[str, str]] | None = None
    if headers:
        headers_list = []
        for k, v in headers.items():
            if k.lower() == "content-type":
                custom_ct = v
            else:
                headers_list.append((k, v))
        if not headers_list:
            headers_list = None

    cookies_data = [c.to_raw_tuple() for c in cookies] if cookies else None
    return (_infer_wire_response_type(media_type), custom_ct, headers_list, cookies_data)


def _response_validation_is_required(annotation: Any) -> bool:
    if annotation is None:
        return False
    return annotation not in _VALIDATION_NOOP_TYPES


def _allows_none(annotation: Any) -> bool:
    """Return True when the response type is None or a union with None."""
    if annotation is type(None):
        return True
    return get_origin(annotation) in (Union, types.UnionType) and type(None) in get_args(annotation)


def _names_one_of(annotation: Any, kinds: tuple[type, ...]) -> bool:
    """Return True when the response type is one of ``kinds`` or a union that names one of them."""
    if annotation in kinds:
        return True
    return get_origin(annotation) in (Union, types.UnionType) and any(arg in kinds for arg in get_args(annotation))


def _compile_response_validator(meta: HandlerMetadata | dict[str, Any]) -> tuple[Any, Any]:
    response_type = meta.get("response_type")
    if not _response_validation_is_required(response_type):
        return None, None

    return _compile_type_validators(response_type, meta["validate_response"])


def _names_response_class(annotation: Any) -> bool:
    """Whether the response type is a response class, such as ``HTML``, or a union with one.

    A response object has its own body, so the route validates no data for it.
    """
    if isinstance(annotation, type):
        return issubclass(annotation, _RESPONSE_INSTANCE_TYPES)
    if get_origin(annotation) in (Union, types.UnionType):
        return any(_names_response_class(arg) for arg in get_args(annotation))
    return False


def _compile_type_validators(annotation: Any, validate: bool) -> tuple[Any, Any]:
    """Return the validators of a declared type: the coercion if ``validate``.

    Pydantic validates each declared type. Both validators are None when the
    route does not validate a type that names no Pydantic model.
    """
    # Pydantic owns a type that names one of its models. It also runs when the
    # route does not validate: the model decides which fields go out.
    # A stream type has no adapter: each item gets the validator of the item type.
    if _extract_stream_item_type(annotation)[0] or _names_response_class(annotation):
        return None, None
    if names_pydantic_model(annotation):
        return compile_pydantic_response_coercer(annotation)
    if not validate:
        return None, None
    try:
        return compile_value_coercer(annotation)
    except PydanticSchemaGenerationError:
        # A type that Pydantic does not know, such as msgspec.Raw or
        # ServerSentEvent: the JSON encoder sends the result as it is.
        return None, None


def _compile_stream_item_validators(meta: HandlerMetadata | dict[str, Any]) -> tuple[Any, Any]:
    # A non-stream annotation and a stream with no item type have the item type
    # None. Only a typed stream of JSON items uses the validator, so a stream of
    # raw chunks never does.
    _, item_type = meta["_stream_info"]
    if not _response_validation_is_required(item_type):
        return None, None
    return _compile_type_validators(item_type, meta["validate_response"])


def _extract_stream_item_type(annotation: Any) -> tuple[bool, Any | None]:
    """Return (is_stream_annotation, item_type) for streaming return annotations."""
    if annotation is None:
        return False, None

    if annotation in _STREAM_ANNOTATION_ORIGINS:
        return True, None

    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            is_stream, item_type = _extract_stream_item_type(arg)
            if is_stream:
                return True, item_type
        return False, None

    if origin in _STREAM_ANNOTATION_ORIGINS:
        args = get_args(annotation)
        return True, args[0] if args else None

    return False, None


def _is_stream_protocol_instance(value: Any) -> bool:
    """Check if a runtime value can be consumed as a stream."""
    if isinstance(value, (str, bytes, bytearray, memoryview, dict, list, tuple, set, frozenset, QuerySet)):
        return False
    return (
        hasattr(value, "__aiter__")
        or hasattr(value, "__anext__")
        or hasattr(value, "__iter__")
        or hasattr(value, "__next__")
    )


def _serialize_stream_chunk_sync(chunk: Any, validator: Any | None) -> bytes | str | bytearray | memoryview:
    """Serialize one stream chunk for sync generators."""
    if isinstance(chunk, _RAW_STREAM_CHUNK_TYPES):
        return chunk

    if validator is not None:
        chunk = validator(chunk)
    return _json.encode(chunk) + b"\n"


async def _serialize_stream_chunk_async(chunk: Any, validator: Any | None) -> bytes | str | bytearray | memoryview:
    """Serialize one stream chunk for async generators."""
    if isinstance(chunk, _RAW_STREAM_CHUNK_TYPES):
        return chunk

    if validator is not None:
        chunk = await validator(chunk)
    return _json.encode(chunk) + b"\n"


def _wrap_sync_stream_chunks(content: Any, meta: HandlerMetadata):
    validator = meta["_stream_item_validator"]
    for chunk in content:
        yield _serialize_stream_chunk_sync(chunk, validator)


async def _wrap_async_stream_chunks(content: Any, meta: HandlerMetadata):
    validator = meta["_stream_item_validator_async"]
    async for chunk in content:
        yield await _serialize_stream_chunk_async(chunk, validator)


# ═══════════════════════════════════════════════════════════════════════════
# SSE (Server-Sent Events) auto-framing
# ═══════════════════════════════════════════════════════════════════════════


def _serialize_sse_chunk(chunk: Any) -> bytes | bytearray | memoryview:
    """Serialize a single yielded item into SSE wire-format bytes."""
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        # Rust converts a bytearray or a memoryview chunk, as for any stream.
        return chunk
    if isinstance(chunk, str):
        return chunk.encode()
    if isinstance(chunk, ServerSentEvent):
        if chunk.raw_data is not None:
            return format_sse_event(
                data_str=chunk.raw_data, event=chunk.event, id=chunk.id, retry=chunk.retry, comment=chunk.comment
            )
        return format_sse_event(
            data_bytes=_json.encode(chunk.data) if chunk.data is not None else None,
            event=chunk.event,
            id=chunk.id,
            retry=chunk.retry,
            comment=chunk.comment,
        )
    # Plain objects (dict, list, a model, etc.)
    return format_sse_event(data_bytes=_json.encode(chunk))


def _wrap_sync_sse_chunks(content: Any):
    for chunk in content:
        yield _serialize_sse_chunk(chunk)


async def _wrap_async_sse_chunks(content: Any):
    async for chunk in content:
        yield _serialize_sse_chunk(chunk)


def _to_stream_wire(stream_response: StreamingResponse) -> ResponseWireV1:
    # Auto-wrap EventSourceResponse generator with SSE framing.
    # Mutate in place so ping_interval and other attributes are preserved for Rust.
    if isinstance(stream_response, EventSourceResponse):
        if stream_response.is_async_generator:
            stream_response.content = _wrap_async_sse_chunks(stream_response.content)
        else:
            stream_response.content = _wrap_sync_sse_chunks(stream_response.content)

    # The meta builder finds the content type in any letter case. With a content
    # type set, Rust does not use the response type.
    custom_headers: dict[str, str] = {
        "content-type": stream_response.media_type  # pragma: no mutate
    }
    if stream_response.headers:
        custom_headers.update(stream_response.headers)
    cookies = getattr(stream_response, "_cookies", None)
    resp_meta = _build_response_meta(
        "streaming",  # pragma: no mutate
        custom_headers,
        cookies,
    )
    return _wire_stream(stream_response.status_code, resp_meta, stream_response)


def _build_auto_streaming_response(
    result: Any,
    stream_info: tuple[bool, Any | None],
    meta: HandlerMetadata | dict[str, Any],
    status_code: int,
    response_class: type | None = None,
) -> StreamingResponse | None:
    """Auto-wrap generator/iterator return values into StreamingResponse."""
    is_stream_annotation, item_type = stream_info
    is_async_gen = inspect.isasyncgen(result)
    is_generator_result = is_async_gen or inspect.isgenerator(result)

    if not is_generator_result and not (is_stream_annotation and _is_stream_protocol_instance(result)):
        return None

    # When response_class=EventSourceResponse, wrap as EventSourceResponse
    # SSE framing is applied later in _to_stream_wire
    if response_class is not None and issubclass(response_class, EventSourceResponse):
        return EventSourceResponse(result, status_code=status_code)

    needs_json_chunk_encoding = (
        is_stream_annotation and item_type is not None and item_type not in _RAW_STREAM_CHUNK_TYPES
    )
    if needs_json_chunk_encoding:
        is_async_stream = is_async_gen or hasattr(result, "__aiter__")
        content = _wrap_async_stream_chunks(result, meta) if is_async_stream else _wrap_sync_stream_chunks(result, meta)
        return StreamingResponse(content, status_code=status_code, media_type=_TYPED_STREAM_MEDIA_TYPE)

    return StreamingResponse(result, status_code=status_code)


def _compile_inline_response_validator(meta: HandlerMetadata | dict[str, Any]) -> tuple[type, ...] | None:
    """Pre-classify the response annotation for the inline-encoding fast path.

    Returns the types that the dispatcher in ``api.py`` checks with
    ``isinstance``. On a match, it encodes with ``meta["_inline_encode"]``,
    the encoder of the Pydantic response type. None is no inline path.

    The dispatcher always delegates non-matching runtime values (Response
    objects, QuerySets, generators, ``None``, dicts, ...) to
    ``serialize_response`` so the slow path handles every edge case uniformly.

    Bails out at registration time for setups that need the slow path
    end-to-end:
      - ``validate_response=False``
      - ``response_class`` set (SSE/FileResponse/etc. need slow-path wrapping)
      - ``default_status_code == 204`` (None-body shortcut lives in the slow path)
      - ``is_multi_response`` (multi-status routes have their own dispatcher)
    """
    # A mutant in this function turns the inline path off, or turns it on for
    # a route that gives the same response on both paths. No test can catch it:
    # - A route with validate_response=False or a multi-response route never reads the result.
    # - A response_class route or a 204 route encodes the same body on both paths.
    # pragma: no mutate start
    if not meta["validate_response"]:
        return None
    if meta.get("response_class") is not None:
        return None
    if meta.get("default_status_code") == 204:
        return None
    if meta.get("is_multi_response"):
        return None
    response_type = meta.get("response_type")
    if response_type is None or response_type in _VALIDATION_NOOP_TYPES:
        return None
    # A Pydantic result goes to its own encoder, which validates each other value.
    return _pydantic_inline_match(response_type)
    # pragma: no mutate end


def _pydantic_inline_match(response_type: Any) -> tuple[type, ...] | None:
    """The types that the inline path encodes for a Pydantic response type, or None.

    ``Model`` and a union of models take their instances. ``list[Model]`` takes
    a list, which the encoder validates. Another value, such as a dict, goes
    to the slow path.
    """
    if get_origin(response_type) is list:
        args = get_args(response_type)
        if args and _pydantic_models_of(args[0]) is not None:
            return (list,)
        return None
    return _pydantic_models_of(response_type)


def _pydantic_models_of(annotation: Any) -> tuple[type, ...] | None:
    """The models of ``Model`` or of a union of models (None allowed), or None."""
    if is_pydantic_model(annotation):
        return (annotation,)
    if get_origin(annotation) not in (Union, types.UnionType):
        return None
    args = [a for a in get_args(annotation) if a is not type(None)]
    if not args or not all(is_pydantic_model(a) for a in args):
        return None
    return tuple(args)


def _compile_pydantic_inline_encode(response_type: Any) -> Callable[[Any], bytes]:
    """The encoder of the inline path for a Pydantic response type. A misfit raises as on the slow path."""
    encode = compile_pydantic_response_encoder(response_type)

    def encode_checked(value: Any) -> bytes:
        try:
            return encode(value)
        except ResponseFailure as exc:
            _raise_response_validation_error(exc)

    return encode_checked


def _compile_inline_encode(response_type: Any) -> Callable[[Any], bytes]:
    """Return the encoder of the inline path."""
    return _compile_pydantic_inline_encode(response_type)


def compile_response_handlers(meta: HandlerMetadata | dict[str, Any]) -> None:
    validator_sync, validator_async = _compile_response_validator(meta)
    stream_validator_sync, stream_validator_async = _compile_stream_item_validators(meta)
    queryset_reader_sync = queryset_reader_async = None
    # A list[PydanticModel] reads a QuerySet with values() when its fields are columns.
    if pydantic_readers := compile_pydantic_queryset_reader(meta.get("response_type")):
        queryset_reader_sync, queryset_reader_async = pydantic_readers
    # Without the inline path, the slow path gives the same response.
    inline_validator = _compile_inline_response_validator(meta)  # pragma: no mutate

    meta["_stream_item_validator"] = stream_validator_sync
    meta["_stream_item_validator_async"] = stream_validator_async
    meta["_inline_response_validator"] = inline_validator  # pragma: no mutate
    meta["_inline_encode"] = None if inline_validator is None else _compile_inline_encode(meta["response_type"])
    # The validators come in pairs: both are None, or neither is.
    meta["_has_response_validation"] = validator_sync is not None

    default_status_code = meta["default_status_code"]
    stream_info = meta["_stream_info"]
    response_class = meta.get("response_class")
    # A None result with no such response type is an error: the handler has no return.
    none_is_json = _allows_none(meta.get("response_type"))

    # A str is text/plain and bytes are application/octet-stream, with no check,
    # when the response type names them (-> str, -> str | None, -> bytes | Item)
    # or when the route does not validate. Each other response type validates them.
    validates = validator_sync is not None and meta["validate_response"]
    validate_text = validates and not _names_one_of(meta.get("response_type"), (str,))
    validate_bytes = validates and not _names_one_of(meta.get("response_type"), (bytes, bytearray))
    # dict, list, str and bytes have no validator. A route that validates still
    # refuses a str or bytes result that such a response type does not name.
    plain_type = meta.get("response_type")
    refuses = meta["validate_response"] and plain_type in _PLAIN_RESPONSE_TYPES
    refuse_text = refuses and plain_type is not str
    refuse_bytes = refuses and plain_type not in (bytes, bytearray)

    async def data_handler(result: Any, status_code: int | None = None) -> ResponseWireV1:
        current_status = default_status_code if status_code is None else status_code

        if result is None and current_status == 204:
            return _wire_bytes(204, _RESPONSE_META_EMPTY, b"")

        # The async validator awaits only for a QuerySet or a model instance. A
        # dict gets the same result from the sync validator.
        if isinstance(result, dict):
            return _serialize_json_payload_sync(result, current_status, validator_sync)
        if isinstance(result, list):
            return await _serialize_json_payload_async(result, current_status, validator_async)
        if isinstance(result, (bytes, bytearray)):
            if validate_bytes:
                return _serialize_validated_raw(result, current_status, validator_sync)
            if refuse_bytes:
                _raise_plain_type_mismatch(plain_type, result)
            return _wire_bytes(current_status, _RESPONSE_META_OCTETSTREAM, bytes(result))
        if isinstance(result, str):
            if validate_text:
                return _serialize_validated_raw(result, current_status, validator_sync)
            if refuse_text:
                _raise_plain_type_mismatch(plain_type, result)
            return _wire_bytes(current_status, _RESPONSE_META_PLAINTEXT, result.encode())

        auto_stream = _build_auto_streaming_response(result, stream_info, meta, current_status, response_class)
        if auto_stream is not None:
            return _to_stream_wire(auto_stream)

        if isinstance(result, QuerySet):
            if queryset_reader_async is not None:
                result = await queryset_reader_async(result)
            else:
                # Bounded ORM pool — parallel, but capped connection count.
                result = await run_in_orm_executor(list, result)
            return await _serialize_json_payload_async(result, current_status, validator_async)

        if isinstance(result, _ENCODED_MODEL_TYPES) or validator_async is not None:
            return await _serialize_json_payload_async(result, current_status, validator_async)

        # The last check, so a dict, a list or a model does not pay for it.
        if isinstance(result, _JSON_SCALAR_TYPES) or (result is None and none_is_json):
            return _wire_bytes(current_status, _RESPONSE_META_JSON, _json.encode(result))

        raise TypeError(
            f"Handler returned unsupported type {type(result).__name__!r}. "
            f"Return dict, list, a JSON scalar, or a Bolt response type (JSON, PlainText, HTML, Redirect, etc.)"
        )

    def data_handler_sync(result: Any, status_code: int | None = None) -> ResponseWireV1:
        current_status = default_status_code if status_code is None else status_code

        if result is None and current_status == 204:
            return _wire_bytes(204, _RESPONSE_META_EMPTY, b"")

        if isinstance(result, dict):
            return _serialize_json_payload_sync(result, current_status, validator_sync)
        if isinstance(result, list):
            return _serialize_json_payload_sync(result, current_status, validator_sync)
        if isinstance(result, (bytes, bytearray)):
            if validate_bytes:
                return _serialize_validated_raw(result, current_status, validator_sync)
            if refuse_bytes:
                _raise_plain_type_mismatch(plain_type, result)
            return _wire_bytes(current_status, _RESPONSE_META_OCTETSTREAM, bytes(result))
        if isinstance(result, str):
            if validate_text:
                return _serialize_validated_raw(result, current_status, validator_sync)
            if refuse_text:
                _raise_plain_type_mismatch(plain_type, result)
            return _wire_bytes(current_status, _RESPONSE_META_PLAINTEXT, result.encode())

        auto_stream = _build_auto_streaming_response(result, stream_info, meta, current_status, response_class)
        if auto_stream is not None:
            return _to_stream_wire(auto_stream)

        if isinstance(result, QuerySet):
            result = queryset_reader_sync(result) if queryset_reader_sync is not None else list(result)
            return _serialize_json_payload_sync(result, current_status, validator_sync)

        if isinstance(result, _ENCODED_MODEL_TYPES) or validator_sync is not None:
            return _serialize_json_payload_sync(result, current_status, validator_sync)

        # The last check, so a dict, a list or a model does not pay for it.
        if isinstance(result, _JSON_SCALAR_TYPES) or (result is None and none_is_json):
            return _wire_bytes(current_status, _RESPONSE_META_JSON, _json.encode(result))

        raise TypeError(
            f"Handler returned unsupported type {type(result).__name__!r}. "
            f"Return dict, list, a JSON scalar, or a Bolt response type (JSON, PlainText, HTML, Redirect, etc.)"
        )

    # The response type describes the body of the default status code only. A
    # JSON or Response with another status (a 400 error map, a 202 receipt)
    # carries a different body, so Bolt encodes it as-is. Per-status schemas
    # come from ``response_model={code: type}``.
    async def response_type_handler(result: Any) -> ResponseWireV1:
        # The lookup gives an exact response type its own class. This check only skips it.
        if (cls := result.__class__) is not JSON and cls not in _RESPONSE_CLASSES:  # pragma: no mutate
            cls = _response_base_class(result)
        if cls is JSON:
            if validator_async is not None and result.status_code == default_status_code:
                try:
                    validated = await validator_async(result.data)
                except ResponseValidationError:
                    raise
                except Exception as exc:
                    _raise_response_validation_error(exc)
                data_bytes = _json.encode(validated)
            else:
                data_bytes = result.to_bytes()
            cookies = getattr(result, "_cookies", None)
            return _wire_bytes(result.status_code, _build_wire_meta("json", result.headers, cookies), data_bytes)
        if cls is ResponseClass:
            # Bytes-like content is a pre-encoded body — skip the response
            # validator, it only applies to structured data (issue #305).
            if (
                validator_async is not None
                and result.status_code == default_status_code
                and not isinstance(result.content, (bytes, bytearray, memoryview))
            ):
                try:
                    validated = await validator_async(result.content)
                except ResponseValidationError:
                    raise
                except Exception as exc:
                    _raise_response_validation_error(exc)
                body = _render_response_body(validated, result.media_type)
            else:
                body = _render_response_body(result.content, result.media_type)
            cookies = getattr(result, "_cookies", None)
            meta = _response_class_wire_meta(result.media_type, result.headers, cookies)
            return _wire_bytes(result.status_code, meta, body)
        return _RESPONSE_SERIALIZERS[cls](result)

    def response_type_handler_sync(result: Any) -> ResponseWireV1:
        # The lookup gives an exact response type its own class. This check only skips it.
        if (cls := result.__class__) is not JSON and cls not in _RESPONSE_CLASSES:  # pragma: no mutate
            cls = _response_base_class(result)
        if cls is JSON:
            if validator_sync is not None and result.status_code == default_status_code:
                try:
                    validated = validator_sync(result.data)
                except ResponseValidationError:
                    raise
                except Exception as exc:
                    _raise_response_validation_error(exc)
                data_bytes = _json.encode(validated)
            else:
                data_bytes = result.to_bytes()
            cookies = getattr(result, "_cookies", None)
            return _wire_bytes(result.status_code, _build_wire_meta("json", result.headers, cookies), data_bytes)
        if cls is ResponseClass:
            # Bytes-like content is a pre-encoded body — skip the response
            # validator, it only applies to structured data (issue #305).
            if (
                validator_sync is not None
                and result.status_code == default_status_code
                and not isinstance(result.content, (bytes, bytearray, memoryview))
            ):
                try:
                    validated = validator_sync(result.content)
                except ResponseValidationError:
                    raise
                except Exception as exc:
                    _raise_response_validation_error(exc)
                body = _render_response_body(validated, result.media_type)
            else:
                body = _render_response_body(result.content, result.media_type)
            cookies = getattr(result, "_cookies", None)
            meta = _response_class_wire_meta(result.media_type, result.headers, cookies)
            return _wire_bytes(result.status_code, meta, body)
        return _RESPONSE_SERIALIZERS[cls](result)

    if refuses and plain_type in (dict, list):
        data_handler, data_handler_sync = _compile_plain_container_handlers(
            plain_type, default_status_code, data_handler, data_handler_sync
        )
    elif meta["validate_response"] and plain_type is type(None):
        data_handler, data_handler_sync = _compile_none_handlers(data_handler, data_handler_sync)

    meta["_default_response_handler"] = data_handler
    meta["_default_response_handler_sync"] = data_handler_sync
    meta["_response_type_handler"] = response_type_handler
    meta["_response_type_handler_sync"] = response_type_handler_sync


_BOLT_RESPONSE_TYPES = _RESPONSE_INSTANCE_TYPES[:-1]


def _response_base_class(result: Any) -> type:
    """Return the response type of an instance of a subclass, for example a Django redirect."""
    for cls in _BOLT_RESPONSE_TYPES:
        if isinstance(result, cls):
            return cls
    # A subclass of a template response renders before it serializes.
    if isinstance(result, SimpleTemplateResponse):
        return SimpleTemplateResponse
    # serialize_response sends only the response types, and Django's is the last of them.
    return DjangoHttpResponse


@cache
def is_response_class(cls: type) -> bool:
    """Return True for a response type or a subclass of one. The cache has one entry for each class."""
    return issubclass(cls, _RESPONSE_INSTANCE_TYPES)


def response_with_status(response: Any, status_code: int) -> Any:
    """Return the response object with the given status.

    The response object of the handler keeps its own status. A handler can
    return one shared response object from more than one route.
    """
    if response.status_code == status_code:
        return response
    response = copy.copy(response)
    response.status_code = status_code
    return response


def serialize_response(result: Any, meta: HandlerMetadata | dict[str, Any]) -> Coroutine[Any, Any, ResponseWireV1]:
    """Serialize handler result to HTTP response.

    The caller awaits the coroutine of the route handler. This function adds no
    coroutine of its own.
    """
    if isinstance(result, _RESPONSE_INSTANCE_TYPES):
        return meta["_response_type_handler"](result)
    return meta["_default_response_handler"](result)


def serialize_response_sync(result: Any, meta: HandlerMetadata | dict[str, Any]) -> ResponseWireV1:
    """Serialize handler result to HTTP response (sync version for sync handlers)."""
    if isinstance(result, _RESPONSE_INSTANCE_TYPES):
        return meta["_response_type_handler_sync"](result)
    return meta["_default_response_handler_sync"](result)


async def _serialize_json_payload_async(result: Any, status_code: int, validator: Any) -> ResponseWireV1:
    if validator is not None:
        try:
            result = await validator(result)
        except ResponseValidationError:
            raise
        except Exception as exc:
            _raise_response_validation_error(exc)
    return _wire_bytes(status_code, _RESPONSE_META_JSON, _json.encode(result))


def _serialize_json_payload_sync(result: Any, status_code: int, validator: Any) -> ResponseWireV1:
    if validator is not None:
        try:
            result = validator(result)
        except ResponseValidationError:
            raise
        except Exception as exc:
            _raise_response_validation_error(exc)
    return _wire_bytes(status_code, _RESPONSE_META_JSON, _json.encode(result))


def _serialize_validated_raw(result: Any, status_code: int, validator: Any) -> ResponseWireV1:
    """Validate a str or bytes result against a response type that does not name it.

    A value that stays a str is text/plain, such as a ``Literal`` member.
    Bytes stay application/octet-stream. A value that the validation
    converts, such as a str to a ``UUID``, is JSON.
    """
    try:
        validated = validator(result)
    except ResponseValidationError:
        raise
    except Exception as exc:
        _raise_response_validation_error(exc)
    if isinstance(validated, str):
        return _wire_bytes(status_code, _RESPONSE_META_PLAINTEXT, validated.encode())
    if isinstance(validated, (bytes, bytearray)):
        return _wire_bytes(status_code, _RESPONSE_META_OCTETSTREAM, bytes(validated))
    return _wire_bytes(status_code, _RESPONSE_META_JSON, _json.encode(validated))


def serialize_plaintext_response(result: PlainText) -> ResponseWireV1:
    """Serialize plain text response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_wire_meta("plaintext", result.headers, cookies)
    return _wire_bytes(result.status_code, resp_meta, result.to_bytes())


def serialize_html_response(result: HTML) -> ResponseWireV1:
    """Serialize HTML response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_wire_meta("html", result.headers, cookies)
    return _wire_bytes(result.status_code, resp_meta, result.to_bytes())


def serialize_redirect_response(result: Redirect) -> ResponseWireV1:
    """Serialize redirect response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    # Build custom headers with location
    custom_headers: dict[str, str] = {"location": result.url}
    if result.headers:
        custom_headers.update(result.headers)

    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_wire_meta("redirect", custom_headers, cookies)
    return _wire_bytes(result.status_code, resp_meta, b"")


def django_cookie_tuples(response: DjangoHttpResponse) -> list[CookieTuple] | None:
    """Return the cookies of a Django response as raw tuples for Rust, or None."""
    if not response.cookies:
        return None
    raw_cookies = []
    for morsel in response.cookies.values():
        max_age = morsel.get("max-age")
        raw_cookies.append((
            morsel.key,
            morsel.value,
            morsel.get("path") or "/",
            int(max_age) if max_age else None,
            morsel.get("expires") or None,
            morsel.get("domain") or None,
            bool(morsel.get("secure")),
            bool(morsel.get("httponly")),
            morsel.get("samesite") or None,
        ))
    return raw_cookies


def _django_wire_meta(
    response_type: str, headers: dict[str, str], result: DjangoHttpResponse
) -> ResponseMetaTuple | int:
    raw_cookies = django_cookie_tuples(result)
    if raw_cookies is None:
        return _build_wire_meta(response_type, headers, None)
    response_type, custom_ct, headers_list, _ = _build_response_meta(response_type, headers, None)
    return (response_type, custom_ct, headers_list, raw_cookies)


def serialize_django_response(result: DjangoHttpResponse) -> ResponseWireV1:
    """Serialize Django HttpResponse types (e.g., from @login_required decorator).

    Only called in fallback path - no overhead for normal Bolt responses.
    """
    # Handle redirects specially (HttpResponseRedirect, HttpResponsePermanentRedirect)
    if isinstance(result, DjangoHttpResponseRedirect):
        # Rust lowercases header names, and the loop skips each Location from Django.
        headers: dict[str, str] = {
            "location": result.url  # pragma: no mutate
        }
        # Copy other headers from Django response
        for key, value in result.items():
            if key.lower() != "location":
                headers[key] = value
        return _wire_bytes(result.status_code, _django_wire_meta("redirect", headers, result), b"")

    # Generic Django HttpResponse - extract content and headers. Rust and the
    # middleware bridges read an unknown response type as "octetstream".
    headers = dict(result.items())
    meta = _django_wire_meta(
        "octetstream",  # pragma: no mutate
        headers,
        result,
    )
    return _wire_bytes(result.status_code, meta, result.content)


def serialize_template_response(result: SimpleTemplateResponse) -> ResponseWireV1:
    """Render a TemplateResponse, then serialize it as a Django response.

    The process_template_response hooks of the Django middleware run before the render.
    The render can query the database, so it runs where a sync ORM call can run.
    """
    return serialize_django_response(run_orm_blocking(render_template_response, result))


def serialize_file_response(result: File) -> ResponseWireV1:
    """Serialize file response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    data = result.read_bytes()
    ctype = result.media_type or mimetypes.guess_type(result.path)[0] or "application/octet-stream"

    # The meta builder finds the content type in any letter case. With a content
    # type set, Rust does not use the response type.
    custom_headers: dict[str, str] = {
        "content-type": ctype  # pragma: no mutate
    }
    if result.filename:
        custom_headers["content-disposition"] = f'attachment; filename="{result.filename}"'
    if result.headers:
        custom_headers.update(result.headers)

    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_wire_meta(
        "file",  # pragma: no mutate
        custom_headers,
        cookies,
    )
    return _wire_bytes(result.status_code, resp_meta, data)


def serialize_file_streaming_response(result: FileResponse) -> ResponseWireV1:
    """Serialize file streaming response.

    Uses the new ResponseMeta tuple format for Rust-side header building.
    """
    ctype = result.media_type or mimetypes.guess_type(result.path)[0] or "application/octet-stream"

    # The meta builder finds the content type in any letter case. With a content
    # type set, Rust does not use the response type.
    custom_headers: dict[str, str] = {
        "content-type": ctype  # pragma: no mutate
    }
    if result.filename:
        custom_headers["content-disposition"] = f'attachment; filename="{result.filename}"'
    if result.headers:
        custom_headers.update(result.headers)

    cookies = getattr(result, "_cookies", None)
    resp_meta = _build_wire_meta(
        "file",  # pragma: no mutate
        custom_headers,
        cookies,
    )
    return _wire_file(result.status_code, resp_meta, result.path)


async def serialize_json_data(result: Any, status_code: int) -> ResponseWireV1:
    """Serialize dict/list/other data as JSON."""
    return _wire_bytes(status_code, _RESPONSE_META_JSON, _json.encode(result))


def serialize_json_data_sync(result: Any, status_code: int) -> ResponseWireV1:
    """Serialize dict/list/other data as JSON (sync version)."""
    return _wire_bytes(status_code, _RESPONSE_META_JSON, _json.encode(result))


# The response types that need no route data, and their serializers. JSON and
# Response use the validator of the route, so the route handlers serialize them.
_RESPONSE_SERIALIZERS: dict[type, Callable[[Any], ResponseWireV1]] = {
    PlainText: serialize_plaintext_response,
    HTML: serialize_html_response,
    Redirect: serialize_redirect_response,
    File: serialize_file_response,
    FileResponse: serialize_file_streaming_response,
    StreamingResponse: _to_stream_wire,
    EventSourceResponse: _to_stream_wire,
    DjangoHttpResponse: serialize_django_response,
    SimpleTemplateResponse: serialize_template_response,
    TemplateResponse: serialize_template_response,
}
# The exact classes that the route handlers dispatch on.
_RESPONSE_CLASSES = frozenset((JSON, ResponseClass, *_RESPONSE_SERIALIZERS))


def serialize_bolt_response(result: Any) -> ResponseWireV1:
    """Serialize a Bolt response object that no route handler serializes.

    A Python middleware can return such an object. It has no route data, so
    Bolt applies no response validation. ``result`` must be an instance of a
    Bolt response type.
    """
    if (cls := result.__class__) is not JSON and cls not in _RESPONSE_CLASSES:  # pragma: no mutate
        cls = _response_base_class(result)
    if cls is JSON:
        cookies = getattr(result, "_cookies", None)
        return _wire_bytes(result.status_code, _build_wire_meta("json", result.headers, cookies), result.to_bytes())
    if cls is ResponseClass:
        cookies = getattr(result, "_cookies", None)
        meta = _response_class_wire_meta(result.media_type, result.headers, cookies)
        return _wire_bytes(result.status_code, meta, _render_response_body(result.content, result.media_type))
    return _RESPONSE_SERIALIZERS[cls](result)
