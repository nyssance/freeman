"""
Parameter binding and extraction with pre-compiled extractors.

This module provides high-performance parameter extraction using pre-compiled
extractor functions that avoid runtime type checking.
"""

from __future__ import annotations

import datetime
import enum
import inspect
from collections.abc import Callable
from typing import Annotated, Any, Literal, get_args, get_origin

import msgspec
from pydantic import Field, ValidationError, create_model

from .._handler_attrs import set_handler_attr
from .._pydantic import adapter_for, is_pydantic_model, param_fields
from .._pydantic import body_errors as pydantic_body_errors
from .._pydantic import build_body_extractor as build_pydantic_body_extractor
from .._pydantic import request_errors as pydantic_errors
from ..datastructures import UploadFile
from ..exceptions import HTTPException, RequestValidationError, msgspec_error_type, parse_msgspec_decode_error
from ..typing import FieldDefinition, is_optional, is_upload_file_type, unwrap_optional

__all__ = [
    "create_extractor_for_field",
    "create_path_extractor",
    "create_query_extractor",
    "create_header_extractor",
    "create_cookie_extractor",
    "create_form_extractor",
    "create_file_extractor",
    "create_body_extractor",
]


_SEQUENCE_ORIGINS = (list, set, frozenset, tuple)
# The msgspec.Meta fields that constrain a value. The other fields only document it.
_META_CONSTRAINTS = ("gt", "ge", "lt", "le", "multiple_of", "pattern", "min_length", "max_length", "tz")
# These sources give one value. A query key can give more than one.
_SINGLE_VALUE_SOURCES = ("path", "header", "cookie")


def _holds_one_item(annotation: Any) -> bool:
    """Return True when msgspec accepts a sequence of exactly one item for this type."""
    node = msgspec.inspect.type_info(annotation)
    if isinstance(node, msgspec.inspect.TupleType):
        return len(node.item_types) == 1
    return (node.min_length or 0) <= 1 and (node.max_length is None or node.max_length >= 1)


def _refuse_many_item_default(loc: str, key: str, annotation: Any, default: Any) -> None:
    """Raise ``TypeError`` when a one-value sequence parameter has a default of two or more items.

    A path, a header or a cookie gives one value. Bolt keeps the last line of a
    repeated header and does not split a comma list. Thus a sequence from such
    a source holds one item, and the OpenAPI schema says ``maxItems: 1``. A
    default of two items is not valid for that schema, so the route fails here.
    """
    if isinstance(default, _SEQUENCE_ORIGINS) and len(default) > 1 and _is_sequence_field(annotation):
        raise TypeError(
            f"The {loc} parameter {key!r} gets at most one item, but its default {default!r} "
            f"has {len(default)} items. Use a default of at most one item, or use Query() for a list of values."
        )


def _param_converter(annotation: Any, loc: str, key: str, *, one_item: bool = False) -> Callable | None:
    """Build the conversion of one parameter value that Rust cannot do, or None.

    Rust converts a scalar value by its type. It does not build a sequence, it
    does not check ``msgspec.Meta`` constraints, and it does not know an
    ``Enum``, a ``Literal`` or a ``timedelta``. For such a parameter, one
    ``msgspec.convert`` builds the ``list``, ``set``, ``frozenset`` or ``tuple``
    from the values, checks the constraints, and gives the ``Enum`` member, the
    ``Literal`` value or the ``timedelta``. A bad value gives a 422 that
    names ``loc`` and ``key``. With ``one_item``, a source that gives one value
    (a path, a header or a cookie) gives a sequence of that one item. A type
    that cannot hold one item, such as ``tuple[int, str]``, then raises
    ``TypeError`` here, when the route registers. A form field (``loc`` is
    ``body``) that is not a sequence keeps the last value of a repeated key.
    """
    target = unwrap_optional(annotation)
    base = get_args(target)[0] if get_origin(target) is Annotated else target
    inner = unwrap_optional(base)
    # A bare `set` has no origin. msgspec reads it as set[Any].
    is_sequence = (get_origin(inner) or inner) in _SEQUENCE_ORIGINS
    bytes_items = _bytes_items(inner, is_sequence)
    if bytes_items is not None:
        return _bytes_param_converter(target, loc, key, items=bytes_items, wrap=is_sequence and one_item)
    constrained = get_origin(target) is Annotated and any(
        getattr(extra, name, None) is not None
        for extra in get_args(target)[1:]
        if isinstance(extra, msgspec.Meta)
        for name in _META_CONSTRAINTS
    )
    is_choice = get_origin(inner) is Literal or (isinstance(inner, type) and issubclass(inner, enum.Enum))
    # Rust has no conversion for a duration. msgspec reads the ISO 8601 form, such as PT5S.
    is_duration = inner is datetime.timedelta
    if not is_sequence and not constrained and not is_choice and not is_duration:
        return None
    wrap = is_sequence and one_item
    # Rust gives each value of a repeated form key as a list.
    take_last = loc == "body" and not is_sequence
    if wrap and loc in _SINGLE_VALUE_SOURCES and not _holds_one_item(target):
        raise TypeError(
            f"The {loc} parameter {key!r} gets one value, but its type {target!r} cannot hold one item. "
            "Use list[T], set[T], tuple[T] or tuple[T, ...]."
        )
    # msgspec caches the type info of a Struct, but not of a type such as list[int].
    # A one-field Struct made here keeps that lookup out of each request.
    holder = msgspec.defstruct("ParamValue", [("value", target)], array_like=True)

    def convert(value: Any) -> Any:
        if wrap and not isinstance(value, list):
            value = [value]
        elif take_last and type(value) is list:
            value = value[-1]
        try:
            return msgspec.convert((value,), holder, strict=False).value
        except msgspec.ValidationError as error:
            # Remove the position of the value in the holder from the error path.
            msg = str(error).replace(" - at `$[0]`", "").replace("`$[0]", "`$")
            raise RequestValidationError(
                errors=[{"type": msgspec_error_type(msg), "loc": (loc, key), "msg": msg, "input": value}]
            ) from None

    return convert


# A text parameter of one of these types gets the UTF-8 bytes of the text.
_BYTES_TYPES = (bytes, bytearray, memoryview)


def _bytes_items(inner: Any, is_sequence: bool) -> bool | frozenset[int] | None:
    """Say which text values of a type become ``bytes``, or None when the type holds no bytes type.

    False is a bytes type that is not a sequence. True is a sequence with one
    item type that is a bytes type, such as ``list[bytes]``. A frozenset holds
    the positions of the bytes items of a tuple such as ``tuple[bytes, int]``.
    """
    if not is_sequence:
        return False if inner in _BYTES_TYPES else None
    is_bytes = [
        (get_args(arg)[0] if get_origin(arg) is Annotated else arg) in _BYTES_TYPES
        for arg in get_args(inner)
        if arg is not Ellipsis
    ]
    if len(is_bytes) == 1:
        return True if is_bytes[0] else None
    return frozenset(position for position, found in enumerate(is_bytes) if found) or None


def _encode_items(values: list[Any], items: bool | frozenset[int]) -> list[Any]:
    """Encode the text items of a sequence that ``_bytes_items`` names."""
    if items is True:
        return [value.encode() if type(value) is str else value for value in values]
    return [
        value.encode() if position in items and type(value) is str else value for position, value in enumerate(values)
    ]


def _bytes_param_converter(target: Any, loc: str, key: str, *, items: bool | frozenset[int], wrap: bool) -> Callable:
    """Build the conversion of a text parameter to ``bytes``, or to a sequence of ``bytes``.

    Rust gives the value of a path segment, a query key, a header, a cookie or
    a form field as text. The parameter gets the UTF-8 bytes of that text.
    msgspec reads a str for a ``bytes`` type as base64. Thus this conversion
    encodes the text first. Then msgspec builds the sequence or the
    ``bytearray`` and checks the ``msgspec.Meta`` constraints.

    Rust gives each value of a repeated form key as a list. A parameter that is
    not a sequence keeps the last value, as for a repeated query key.
    """
    if target is bytes:
        # Plain bytes: msgspec has nothing to build and nothing to check.
        def encode(value: Any) -> bytes:
            return (value[-1] if type(value) is list else value).encode()

        return encode

    if wrap and loc in _SINGLE_VALUE_SOURCES and not _holds_one_item(target):
        raise TypeError(
            f"The {loc} parameter {key!r} gets one value, but its type {target!r} cannot hold one item. "
            "Use list[T], set[T], tuple[T] or tuple[T, ...]."
        )
    holder = msgspec.defstruct("ParamValue", [("value", target)], array_like=True)
    is_sequence = items is not False
    # The other items of a tuple such as tuple[bytes, int] are text. msgspec converts them.
    strict = type(items) is bool

    def convert(value: Any) -> Any:
        if type(value) is list:
            encoded = _encode_items(value, items) if is_sequence else value[-1].encode()
        else:
            encoded = [value.encode()] if wrap else value.encode()
        try:
            return msgspec.convert((encoded,), holder, strict=strict).value
        except msgspec.ValidationError as error:
            # Remove the position of the value in the holder from the error path.
            msg = str(error).replace(" - at `$[0]`", "").replace("`$[0]", "`$")
            raise RequestValidationError(
                errors=[{"type": msgspec_error_type(msg), "loc": (loc, key), "msg": msg, "input": value}]
            ) from None

    return convert


def _converted_extractor(key: str, annotation: Any, default: Any, convert: Callable, missing: str) -> Callable:
    """Build an extractor that reads ``key`` and converts a present value with ``convert``.

    A missing value gives the default of the parameter, which is not converted,
    or a 422 with the ``missing`` detail when the parameter is required.
    """
    if default is not inspect.Parameter.empty or is_optional(annotation):
        default_value = None if default is inspect.Parameter.empty else default

        def extract_optional(source_map: dict[str, Any]) -> Any:
            if key in source_map:
                return convert(source_map[key])
            return default_value

        return extract_optional

    def extract_required(source_map: dict[str, Any]) -> Any:
        try:
            value = source_map[key]
        except KeyError:
            raise HTTPException(status_code=422, detail=missing) from None
        return convert(value)

    return extract_required


def create_path_extractor(name: str, annotation: Any, alias: str | None = None) -> Callable:
    """Create a pre-compiled extractor for path parameters.

    Note: Rust pre-converts values to typed Python objects (int, float, bool,
    str, uuid.UUID, decimal.Decimal, datetime, date, time) for both HTTP and WebSocket.
    """
    key = alias or name
    convert = _param_converter(annotation, "path", key, one_item=True)

    if convert is not None:

        def extract_converted(params_map: dict[str, Any]) -> Any:
            if key not in params_map:
                raise HTTPException(status_code=422, detail=f"Missing required path parameter: {key}")
            return convert(params_map[key])

        return extract_converted

    def extract(params_map: dict[str, Any]) -> Any:
        if key not in params_map:
            raise HTTPException(status_code=422, detail=f"Missing required path parameter: {key}")
        return params_map[key]

    return extract


def create_query_extractor(
    name: str, annotation: Any, default: Any, alias: str | None = None, other_keys: frozenset[str] = frozenset()
) -> Callable:
    """Create a pre-compiled extractor for query parameters.

    A Pydantic model gets all its fields from the query parameters.

    Note: Rust pre-converts values to typed Python objects (int, float, bool,
    str, uuid.UUID, decimal.Decimal, datetime, date, time) for both HTTP and WebSocket.
    """
    # A Pydantic model has its own extractor.
    unwrapped = unwrap_optional(annotation)
    if is_pydantic_model(unwrapped):
        return _create_pydantic_param_extractor(unwrapped, default, "query", other_keys)

    # Individual field extraction
    key = alias or name
    # HTTP routes get each value of a sequence key as a list from Rust. A
    # WebSocket scope gives the last value of a key, so it gives one item.
    convert = _param_converter(annotation, "query", key, one_item=True)
    if convert is not None:
        return _converted_extractor(key, annotation, default, convert, f"Missing required query parameter: {key}")
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract(query_map: dict[str, Any]) -> Any:
            if key in query_map:
                return query_map[key]
            return default_value

    else:

        def extract(query_map: dict[str, Any]) -> Any:
            if key not in query_map:
                raise HTTPException(status_code=422, detail=f"Missing required query parameter: {key}")
            return query_map[key]

    return extract


def create_header_extractor(name: str, annotation: Any, default: Any, alias: str | None = None) -> Callable:
    """Create a pre-compiled extractor for HTTP headers.

    A Pydantic model gets all its fields from the headers.

    Note: The compiler puts the non-string header types into ``header_types``,
    keyed by header name. Rust converts and validates those values for HTTP
    and WebSocket, and a bad value gives a 422 (HTTP) or a rejected upgrade.
    The extractor thus reads typed objects (int, float, bool, uuid.UUID,
    decimal.Decimal, datetime, date, time) and does no conversion.
    """
    # A Pydantic model has its own extractor.
    unwrapped = unwrap_optional(annotation)
    if is_pydantic_model(unwrapped):
        return _create_pydantic_header_extractor(unwrapped, default)

    # Individual field extraction
    # Convert underscores to hyphens for HTTP header lookup
    # e.g., x_custom -> x-custom, content_type -> content-type
    key = (alias or name).lower().replace("_", "-")
    convert = _param_converter(annotation, "header", key, one_item=True)
    if convert is not None:
        return _converted_extractor(key, annotation, default, convert, f"Missing required header: {key}")
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract(headers_map: dict[str, Any]) -> Any:
            if key in headers_map:
                return headers_map[key]
            return default_value

    else:

        def extract(headers_map: dict[str, Any]) -> Any:
            # One dict lookup on the hit path; the try block costs nothing there.
            try:
                return headers_map[key]
            except KeyError:
                raise HTTPException(status_code=422, detail=f"Missing required header: {key}") from None

    return extract


def create_cookie_extractor(
    name: str, annotation: Any, default: Any, alias: str | None = None, other_keys: frozenset[str] = frozenset()
) -> Callable:
    """Create a pre-compiled extractor for cookies.

    A Pydantic model gets all its fields from the cookies.

    Note: The compiler puts the non-string cookie types into ``cookie_types``,
    keyed by cookie name. Rust converts and validates those values for HTTP
    and WebSocket, and a bad value gives a 422 (HTTP) or a rejected upgrade.
    The extractor thus reads typed objects (int, float, bool, uuid.UUID,
    decimal.Decimal, datetime, date, time) and does no conversion.
    """
    # A Pydantic model has its own extractor.
    unwrapped = unwrap_optional(annotation)
    if is_pydantic_model(unwrapped):
        return _create_pydantic_param_extractor(unwrapped, default, "cookie", other_keys)

    # Individual field extraction
    key = alias or name
    convert = _param_converter(annotation, "cookie", key, one_item=True)
    if convert is not None:
        return _converted_extractor(key, annotation, default, convert, f"Missing required cookie: {key}")
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract(cookies_map: dict[str, Any]) -> Any:
            if key in cookies_map:
                return cookies_map[key]
            return default_value

    else:

        def extract(cookies_map: dict[str, Any]) -> Any:
            # One dict lookup on the hit path; the try block costs nothing there.
            try:
                return cookies_map[key]
            except KeyError:
                raise HTTPException(status_code=422, detail=f"Missing required cookie: {key}") from None

    return extract


def create_form_extractor(
    name: str, annotation: Any, default: Any, alias: str | None = None, other_keys: frozenset[str] = frozenset()
) -> Callable:
    """Create a pre-compiled extractor for form fields.

    A Pydantic model gets all its fields from the form data.

    Note: Rust pre-converts values to typed Python objects (int, float, bool, str).
    Rust gives each value of a repeated key as a list. A field that is not a
    sequence keeps the last value, as a query parameter does. An ``Any`` field
    gets the list.
    """
    # A Pydantic model has its own extractor.
    unwrapped = unwrap_optional(annotation)
    if is_pydantic_model(unwrapped):
        return _create_pydantic_form_extractor(unwrapped, default, other_keys)

    # Individual field extraction
    key = alias or name
    convert = _param_converter(annotation, "body", key)
    if convert is not None:
        return _converted_extractor(key, annotation, default, convert, f"Missing required form field: {key}")
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract_all(form_map: dict[str, Any]) -> Any:
            return form_map.get(key, default_value)

        def extract(form_map: dict[str, Any]) -> Any:
            if key not in form_map:
                return default_value
            value = form_map[key]
            return value[-1] if type(value) is list else value

    else:

        def extract_all(form_map: dict[str, Any]) -> Any:
            if key not in form_map:
                raise HTTPException(status_code=422, detail=f"Missing required form field: {key}")
            return form_map[key]

        def extract(form_map: dict[str, Any]) -> Any:
            try:
                value = form_map[key]
            except KeyError:
                raise HTTPException(status_code=422, detail=f"Missing required form field: {key}") from None
            return value[-1] if type(value) is list else value

    return extract_all if unwrapped is Any else extract


def _is_sequence_field(field_type: Any) -> bool:
    """
    Determine whether a type annotation represents a sequence container (list, set, frozenset, or tuple).

    Unwraps Optional[...] and Annotated[...] before checking the underlying origin. A bare ``set`` has no origin, so the class itself counts.

    Parameters:
        field_type (Any): The type annotation to inspect.

    Returns:
        bool: `True` if the (unwrapped) annotation's origin is one of list, set, frozenset, or tuple; `False` otherwise.
    """
    inner = unwrap_optional(field_type)
    if get_origin(inner) is Annotated:
        inner = unwrap_optional(get_args(inner)[0])
    return (get_origin(inner) or inner) in _SEQUENCE_ORIGINS


def _or_model_default(
    extract: Callable, default: Any, names: tuple[str, ...], needs_files: bool | None = None
) -> Callable:
    """Give an optional model parameter its default when the request has none of its keys.

    ``names`` holds the keys that the model reads. With one of them in the
    request, ``extract`` builds and validates the model, as for a required
    parameter. A required parameter (no default) keeps ``extract`` as it is.
    ``needs_files`` is the ``needs_files_map`` flag of a query, cookie or form
    model extractor, and None for a header model extractor.
    """
    if default is inspect.Parameter.empty:
        return extract

    if needs_files:

        def extract_or_default(param_map: dict[str, Any], files_map: dict[str, Any] | None = None) -> Any:
            for name in names:
                if name in param_map or (files_map is not None and name in files_map):
                    return extract(param_map, files_map)
            return default

    else:

        def extract_or_default(param_map: dict[str, Any]) -> Any:
            for name in names:
                if name in param_map:
                    return extract(param_map)
            return default

    if needs_files is not None:
        set_handler_attr(extract_or_default, "needs_files_map", needs_files)
    return extract_or_default


def _header_name(key: str) -> str:
    """The header that a key reads, as for a header parameter: ``x_count`` reads ``x-count``."""
    return key.lower().replace("_", "-")


def _pydantic_validator(model: type, source: str, locs: dict[str, str] | None = None) -> Callable:
    """Validate the collected values with the model, and raise one 422 with every error.

    The values are text, or lists of text for a sequence field. Pydantic in lax
    mode converts them (``validate_strings`` takes no lists). ``locs`` maps a
    key of the model to the name in the ``loc`` of its error, when they differ.
    """
    validate = adapter_for(model).validate_python
    literal_plan = tuple(
        (field.key, convert) for field in param_fields(model) if (convert := _literal_text_converter(field.annotation))
    )

    def run(values: dict[str, Any]) -> Any:
        if literal_plan:
            values = _convert_literal_texts(values, literal_plan)
        try:
            return validate(values, strict=False)
        except ValidationError as e:
            errors = pydantic_errors(e, source)
            if locs:
                for error in errors:
                    loc = error["loc"]
                    if len(loc) > 1 and loc[1] in locs:
                        loc[1] = locs[loc[1]]
            raise RequestValidationError(errors=errors) from e

    return run


_LITERAL_TEXT_TYPES = (bool, int, float)


def _literal_text_converter(annotation: Any) -> Callable[[Any], Any] | None:
    """The converter of the text of a ``Literal`` field with an int, float or bool member, or None.

    Lax Pydantic does not convert the text "2" to the member 2 of a
    ``Literal``, and a parameter value is always text. The converter gives
    the member that the text names: a str member first, then the first other
    member that the text converts to. A text that names no member stays as
    it is, and Pydantic reports it. A sequence field converts each item.
    """
    inner = unwrap_optional(annotation)
    if get_origin(inner) is Annotated:
        inner = unwrap_optional(get_args(inner)[0])
    if _is_sequence_field(inner):
        args = get_args(inner)
        convert_item = _literal_text_converter(args[0]) if len(args) == 1 else None
        if convert_item is None:
            return None

        def convert_items(value: Any) -> Any:
            if isinstance(value, list):
                return [convert_item(item) for item in value]
            return value

        return convert_items
    if get_origin(inner) is not Literal:
        return None
    members = get_args(inner)
    texts = frozenset(member for member in members if isinstance(member, str))
    others = tuple(
        (member, adapter_for(type(member)).validate_python) for member in members if type(member) in _LITERAL_TEXT_TYPES
    )
    if not others:
        return None

    def convert(value: Any) -> Any:
        if not isinstance(value, str) or value in texts:
            return value
        for member, validate in others:
            try:
                converted = validate(value, strict=False)
            except ValidationError:
                continue
            if type(converted) is type(member) and converted == member:
                return member
        return value

    return convert


def _convert_literal_texts(
    values: dict[str, Any], plan: tuple[tuple[str, Callable[[Any], Any]], ...]
) -> dict[str, Any]:
    """A copy of ``values`` with the text of each ``Literal`` field converted, or ``values`` when none is there."""
    converted = values
    for key, convert in plan:
        value = values.get(key)
        if value is None:
            continue
        new = convert(value)
        if new is not value:
            if converted is values:
                converted = dict(values)
            converted[key] = new
    return converted


def _create_pydantic_param_extractor(model: type, default: Any, source: str, other_keys: frozenset[str]) -> Callable:
    """Create an extractor that validates a Pydantic model from the query or the cookies.

    The model gets the map of its source. A model that does not ignore extra
    keys (``extra="forbid"`` or ``"allow"``) does not get the keys of the
    other parameters of the handler. A sequence field gets a list: Rust gives
    one for a repeated query key, and a cookie or a WebSocket query gives one item.
    """
    fields = param_fields(model)
    keys = tuple(field.key for field in fields)
    sequence_keys = tuple(field.key for field in fields if _is_sequence_field(field.annotation))
    takes_extra = model.model_config.get("extra") in ("forbid", "allow")
    dropped = other_keys - set(keys) if takes_extra else frozenset()
    run = _pydantic_validator(model, source)

    def extract(param_map: dict[str, Any]) -> Any:
        values = param_map
        if dropped and not dropped.isdisjoint(values):
            values = {k: v for k, v in values.items() if k not in dropped}
        for key in sequence_keys:
            value = values.get(key)
            if value is not None and not isinstance(value, list):
                if values is param_map:
                    values = dict(param_map)
                values[key] = [value]
        return run(values)

    set_handler_attr(extract, "needs_files_map", False)
    return _or_model_default(extract, default, keys, needs_files=False)


def _create_pydantic_form_extractor(model: type, default: Any, other_keys: frozenset[str]) -> Callable:
    """Create an extractor that validates a Pydantic model from the form fields and the file parts.

    The ``loc`` of an error starts with ``body``, as for a form struct. A text
    field that the form repeats keeps its last value, and a sequence field
    gets each value. An ``UploadFile`` field gets the first file of its part,
    a ``list[UploadFile]`` field each file. The extractor builds the
    UploadFiles before the model validates, and the request closes them after
    the handler. Rust checks the ``File()`` constraints of each part.
    """
    fields = param_fields(model)
    keys = tuple(field.key for field in fields)
    sequence_keys = frozenset(field.key for field in fields if _is_sequence_field(field.annotation))
    upload_keys = tuple(
        (field.key, field.key in sequence_keys) for field in fields if is_upload_file_type(field.annotation)
    )
    takes_extra = model.model_config.get("extra") in ("forbid", "allow")
    dropped = other_keys - set(keys) if takes_extra else frozenset()
    run = _pydantic_validator(model, "body")

    def collect(form_map: dict[str, Any]) -> dict[str, Any]:
        values = {}
        for key, value in form_map.items():
            if key in dropped:
                continue
            if key in sequence_keys:
                values[key] = value if isinstance(value, list) or value is None else [value]
            else:
                values[key] = value[-1] if type(value) is list and value else value
        return values

    if not upload_keys:

        def extract(form_map: dict[str, Any]) -> Any:
            return run(collect(form_map))

        set_handler_attr(extract, "needs_files_map", False)
        return _or_model_default(extract, default, keys, needs_files=False)

    def extract_with_files(form_map: dict[str, Any], files_map: dict[str, Any] | None = None) -> Any:
        values = collect(form_map)
        if files_map:
            for key, value in files_map.items():
                if key != "_upload_files" and key not in dropped:
                    values[key] = value
            uploads = files_map.setdefault("_upload_files", [])
            for key, many in upload_keys:
                infos = files_map.get(key)
                if infos is None:
                    continue
                if not isinstance(infos, list):
                    infos = [infos]
                files = [UploadFile.from_file_info(info) for info in (infos if many else infos[:1])]
                uploads.extend(files)
                values[key] = files if many else files[0]
        return run(values)

    set_handler_attr(extract_with_files, "needs_files_map", True)
    return _or_model_default(extract_with_files, default, keys, needs_files=True)


def _create_pydantic_header_extractor(model: type, default: Any) -> Callable:
    """Create an extractor that validates a Pydantic model from the headers.

    Each field reads the header of its key, as a header parameter does. The
    model gets only these headers, so ``extra="forbid"`` never sees the other
    headers of the request. A sequence field gets one item.
    """
    plan = tuple(
        (_header_name(field.key), field.key, _is_sequence_field(field.annotation)) for field in param_fields(model)
    )
    run = _pydantic_validator(model, "header", {key: header for header, key, _ in plan})

    def extract(headers_map: dict[str, str]) -> Any:
        values = {}
        for header, key, wrap in plan:
            value = headers_map.get(header)
            if value is not None:
                values[key] = [value] if wrap else value
        return run(values)

    return _or_model_default(extract, default, tuple(header for header, _, _ in plan))


def create_file_extractor(name: str, annotation: Any, default: Any, alias: str | None = None) -> Callable:
    """
    Create a pre-compiled extractor for file uploads with UploadFile support.

    Supports both the new UploadFile type and legacy dict annotations for
    backward compatibility. A ``bytes`` parameter gets the content of the
    file, and ``list[bytes]`` gets the content of each file.

    Note: Rust does the file validation (max_size, min_size, allowed_types,
    max_files) at parse time, from the route metadata. This function only
    creates UploadFile objects from the pre-validated file data.

    Args:
        name: Parameter name
        annotation: Type annotation (UploadFile, list[UploadFile], bytes, list[bytes], dict, list[dict])
        default: Default value
        alias: Alternative field name

    Returns:
        Extractor function
    """
    key = alias or name
    optional = default is not inspect.Parameter.empty or is_optional(annotation)

    # Determine expected type
    unwrapped = unwrap_optional(annotation)
    origin = get_origin(unwrapped)
    expects_list = origin is list

    # Check if we should create UploadFile instances
    # Use is_upload_file_type helper which handles all cases including Optional
    expects_upload_file = is_upload_file_type(annotation)

    item_type = (get_args(unwrapped) or (Any,))[0] if expects_list else unwrapped
    if item_type is bytes:
        # The handler gets the content, not the file dict from Rust.
        default_value = None if default is inspect.Parameter.empty else default

        def read_content(file_info: dict[str, Any]) -> bytes:
            content = file_info["content"]
            if content is None:
                # Rust wrote a large file to disk. It deletes the file after the request.
                with open(file_info["temp_path"], "rb") as spooled:
                    return spooled.read()
            return content

        def extract_bytes(files_map: dict[str, Any]) -> Any:
            try:
                file_info = files_map[key]
            except KeyError:
                if optional:
                    return default_value
                raise RequestValidationError(
                    errors=[
                        {"type": "file_missing", "loc": ("body", key), "msg": "Missing required file", "input": None}
                    ]
                ) from None
            # Rust gives one dict for one file, and a list of dicts for more files.
            if expects_list:
                if isinstance(file_info, list):
                    return [read_content(item) for item in file_info]
                return [read_content(file_info)]
            # As for UploadFile: a parameter for one file gets the first file.
            return read_content(file_info[0] if isinstance(file_info, list) else file_info)

        return extract_bytes

    if optional:
        default_value = None if default is inspect.Parameter.empty else default

        def extract_optional(files_map: dict[str, Any]) -> Any:
            if key not in files_map:
                return default_value

            file_info = files_map[key]

            if expects_upload_file:
                if isinstance(file_info, list):
                    uploads = [UploadFile.from_file_info(f) for f in file_info]
                    # Track for auto-cleanup
                    if "_upload_files" not in files_map:
                        files_map["_upload_files"] = []
                    files_map["_upload_files"].extend(uploads)
                    return uploads if expects_list else uploads[0]
                else:
                    upload = UploadFile.from_file_info(file_info)
                    # Track for auto-cleanup
                    if "_upload_files" not in files_map:
                        files_map["_upload_files"] = []
                    files_map["_upload_files"].append(upload)
                    return [upload] if expects_list else upload
            else:
                # Legacy behavior for dict annotations
                if expects_list and not isinstance(file_info, list):
                    return [file_info]
                return file_info

        return extract_optional
    else:

        def extract_required(files_map: dict[str, Any]) -> Any:
            if key not in files_map:
                raise RequestValidationError(
                    errors=[
                        {"type": "file_missing", "loc": ("body", key), "msg": "Missing required file", "input": None}
                    ]
                )

            file_info = files_map[key]

            if expects_upload_file:
                if isinstance(file_info, list):
                    uploads = [UploadFile.from_file_info(f) for f in file_info]
                    # Track for auto-cleanup
                    if "_upload_files" not in files_map:
                        files_map["_upload_files"] = []
                    files_map["_upload_files"].extend(uploads)
                    return uploads if expects_list else uploads[0]
                else:
                    upload = UploadFile.from_file_info(file_info)
                    # Track for auto-cleanup
                    if "_upload_files" not in files_map:
                        files_map["_upload_files"] = []
                    files_map["_upload_files"].append(upload)
                    return [upload] if expects_list else upload
            else:
                # Legacy behavior for dict annotations
                if expects_list and not isinstance(file_info, list):
                    return [file_info]
                return file_info

        return extract_required


_BODY_EXTRACTORS: dict[Any, Callable] = {}
# The extractors of optional bodies with the default None, by the extractor of the type.
_OPTIONAL_BODY_EXTRACTORS: dict[Callable, Callable] = {}


def create_body_extractor(
    name: str, annotation: Any, default: Any = inspect.Parameter.empty, embed_key: str | None = None
) -> Callable:
    """
    Create a pre-compiled extractor for request body.

    With ``embed_key`` (``Body(embed=True)``), the body is a JSON object and
    the value is at that key. See ``_build_embedded_body_extractor``.

    Uses cached msgspec decoder for maximum performance.
    Converts msgspec.DecodeError (JSON parsing errors) to RequestValidationError for proper 422 responses.

    Each body type has one extractor. So a handler and a dependency with the
    same body type decode the body one time (see ``decode_body_once``).

    An empty body gives the default of a parameter that has one, and None for
    an ``Optional`` type without default. A required parameter gets a 422.
    """
    if embed_key is not None:
        return _build_embedded_body_extractor(annotation, embed_key, default)
    try:
        extractor = _BODY_EXTRACTORS[annotation]
    except KeyError:
        extractor = _BODY_EXTRACTORS[annotation] = _build_body_extractor(annotation)
    except TypeError:
        # An unhashable annotation gets its own extractor.
        extractor = _build_body_extractor(annotation)

    if default is inspect.Parameter.empty:
        if not is_optional(annotation):
            return extractor
        default = None
    if default is not None:
        return _with_empty_body_default(extractor, default)
    try:
        return _OPTIONAL_BODY_EXTRACTORS[extractor]
    except KeyError:
        optional = _OPTIONAL_BODY_EXTRACTORS[extractor] = _with_empty_body_default(extractor, None)
        return optional


def _with_empty_body_default(extractor: Callable, default_value: Any) -> Callable:
    def extract_or_default(body_bytes: bytes) -> Any:
        if not body_bytes:
            return default_value
        return extractor(body_bytes)

    return extract_or_default


def _build_embedded_body_extractor(annotation: Any, key: str, default: Any) -> Callable:
    """Build the extractor of a ``Body(embed=True)`` parameter.

    The body is a JSON object, and the value of the parameter is at ``key``:
    ``{"item": {...}}`` for the parameter ``item``. A Pydantic model with one
    field at the alias ``key`` reads the object in one ``validate_json``. Thus
    each error has the ``loc`` ``["body", key, ...]``, and the 422 holds every
    error. The other keys of the object are not read, so several embedded
    parameters share one object.

    A parameter with a default, or with an ``Optional`` type, takes the
    default when the key is missing or the request has no body.
    """
    if default is inspect.Parameter.empty and is_optional(annotation):
        default = None
    has_default = default is not inspect.Parameter.empty
    field = Field(default, alias=key) if has_default else Field(alias=key)
    wrapper = create_model("EmbeddedBody", value=(annotation, field))
    validate_json = wrapper.model_validate_json

    def extract(body_bytes: bytes) -> Any:
        if has_default and not body_bytes:
            return default
        try:
            return validate_json(body_bytes).value
        except ValidationError as e:
            raise RequestValidationError(errors=pydantic_body_errors(e, body_bytes), body=body_bytes) from e

    return extract


# A body of one of these types has no type to check. msgspec reads it: it is
# about 2.5 times faster than Pydantic at plain JSON.
_UNTYPED_BODIES = (Any, inspect.Parameter.empty)


def _build_body_extractor(annotation: Any) -> Callable:
    """Pydantic validates each typed body, so a body has one validator and one error shape."""
    if annotation not in _UNTYPED_BODIES:
        return build_pydantic_body_extractor(annotation)
    decode = msgspec.json.decode

    def extract(body_bytes: bytes) -> Any:
        try:
            return decode(body_bytes)
        except msgspec.DecodeError as e:
            # Bytes that are not JSON: a 422 with the line and the column of the fault.
            raise RequestValidationError(errors=[parse_msgspec_decode_error(e, body_bytes)], body=body_bytes) from e

    return extract


def create_extractor_for_field(field: FieldDefinition, other_keys: frozenset[str] = frozenset()) -> Callable | None:
    """
    Create a pre-compiled extractor function for a FieldDefinition.

    This is the preferred factory that works with FieldDefinition objects.
    The returned function is specialized based on the parameter source,
    eliminating runtime type checking.

    Args:
        field: FieldDefinition object
        other_keys: The keys that the other parameters of the handler read from
            the source of this field. A query, cookie or form struct that
            forbids unknown fields does not get them.

    Returns:
        Extractor function, or None for 'request' and 'dependency' sources
        (which are handled specially by the injector)
    """
    source = field.source
    name = field.name
    annotation = field.annotation
    # An Optional type without default has the default None.
    default = None if field.default is inspect.Parameter.empty and is_optional(annotation) else field.default
    alias = field.alias

    # Return appropriate extractor based on source
    if source in _SINGLE_VALUE_SOURCES:
        if source == "path":
            extractor = create_path_extractor(name, annotation, alias)
        elif source == "header":
            extractor = create_header_extractor(name, annotation, default, alias)
        else:
            extractor = create_cookie_extractor(name, annotation, default, alias, other_keys)
        # After the extractor: a type that cannot hold one item gives its own error first.
        model = unwrap_optional(annotation)
        if is_pydantic_model(model):
            for model_field in param_fields(model):
                key = _header_name(model_field.key) if source == "header" else model_field.key
                _refuse_many_item_default(source, key, model_field.annotation, model_field.default)
        else:
            _refuse_many_item_default(source, alias or name, annotation, default)
        return extractor
    elif source == "query":
        return create_query_extractor(name, annotation, default, alias, other_keys)
    elif source == "form":
        return create_form_extractor(name, annotation, default, alias, other_keys)
    elif source == "file":
        # Rust checks the file constraints of the Param from the route metadata.
        return create_file_extractor(name, annotation, default, alias)
    elif source == "body":
        # Body(embed=True): the value is at the key of the alias or of the parameter name.
        embed_key = (alias or name) if field.embed else None
        return create_body_extractor(name, annotation, default, embed_key)
    elif source == "request":
        # Request source is handled directly in the injector
        return None
    elif source == "dependency":
        # Dependencies are handled specially in the injector
        return None
    raise TypeError(
        f"Parameter {name!r} has an unsupported source {source!r}. "
        "Use one of: path, query, header, cookie, form, file, body."
    )
