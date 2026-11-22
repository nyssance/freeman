"""Pydantic at the two edges of a handler: the JSON body that comes in and the result that goes out.

A handler names a Pydantic model as a body parameter or as its return type.
Rust still does the routing, the path and query parameters, and the guards.
This module does only what needs the model:

- the body: ``validate_json`` from the raw bytes, with every error in one 422;
- the result: ``dump_json`` to bytes, which the JSON encoder passes through
  as they are (``msgspec.Raw``).
"""

import types
from collections.abc import Callable
from typing import Annotated, Any, Union, get_args, get_origin

import msgspec
from django.db.models import QuerySet
from pydantic import BaseModel, TypeAdapter, ValidationError

from .concurrency import run_in_orm_executor
from .exceptions import RequestValidationError

__all__ = (
    "adapter_for",
    "build_body_extractor",
    "compile_response_coercer",
    "encode_model",
    "is_pydantic_model",
    "names_pydantic_model",
)

_ADAPTERS: dict[Any, TypeAdapter[Any]] = {}
_UNION_ORIGINS = (Union, types.UnionType)


def is_pydantic_model(annotation: Any) -> bool:
    """Whether ``annotation`` is a Pydantic model class."""
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def names_pydantic_model(annotation: Any) -> bool:
    """Whether a Pydantic model is at any position of ``annotation``.

    ``Model``, ``Model | None``, ``list[Model]``, ``dict[str, Model]`` and
    ``Annotated[Model, ...]`` all do. Pydantic then owns the whole type.
    """
    if is_pydantic_model(annotation):
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    args = get_args(annotation)
    if origin is Annotated:
        return names_pydantic_model(args[0])
    return any(names_pydantic_model(arg) for arg in args)


def adapter_for(annotation: Any) -> TypeAdapter[Any]:
    """The ``TypeAdapter`` of ``annotation``. Each type has one."""
    try:
        return _ADAPTERS[annotation]
    except KeyError:
        adapter = _ADAPTERS[annotation] = TypeAdapter(annotation)
        return adapter
    except TypeError:
        # An annotation that cannot be a dict key gets its own adapter.
        return TypeAdapter(annotation)


def _errors(exc: ValidationError, where: str) -> list[dict[str, Any]]:
    """The errors of ``exc`` in the shape of the other 422 responses of Bolt."""
    return [
        {"loc": [where, *error["loc"]] if where else list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors(include_url=False, include_input=False, include_context=False)
    ]


def build_body_extractor(annotation: Any) -> Callable[[bytes], Any]:
    """The body extractor of a type that names a Pydantic model.

    Pydantic reads the bytes and validates in one step. It reports every
    error of the body, so the client gets all of them in one response.
    """
    validate_json = adapter_for(annotation).validate_json

    def extract(body_bytes: bytes) -> Any:
        try:
            return validate_json(body_bytes)
        except ValidationError as e:
            raise RequestValidationError(errors=_errors(e, "body"), body=body_bytes) from e

    return extract


def encode_model(value: BaseModel) -> msgspec.Raw:
    """The JSON of a model instance, as bytes that the encoder does not read again."""
    return msgspec.Raw(type(value).__pydantic_serializer__.to_json(value))


def compile_response_coercer(annotation: Any) -> tuple[Callable[[Any], Any], Callable[[Any], Any]]:
    """Build the response step of a return type that names a Pydantic model, once per route.

    The step gives the encoded JSON. A value that already is the model is
    encoded as it is. Each other value (a dict, a Django model instance, a
    list of them) goes through the model first, which reads an object by
    attribute. Thus only the fields of the model reach the client.

    Returns the sync step and its async form. They differ only for a
    QuerySet: the async form loads it on the ORM executor.
    """
    adapter = adapter_for(annotation)
    validate = adapter.validate_python
    dump = adapter.dump_json
    model = annotation if is_pydantic_model(annotation) else None

    def encode(value: Any) -> msgspec.Raw:
        if model is not None and type(value) is model:
            return msgspec.Raw(model.__pydantic_serializer__.to_json(value))
        try:
            return msgspec.Raw(dump(validate(value, from_attributes=True)))
        except ValidationError as e:
            # The field and the reason, not the value: the message goes to the log.
            problems = "; ".join(
                f"{'.'.join(map(str, error['loc'])) or 'value'}: {error['msg']}" for error in _errors(e, "")
            )
            raise TypeError(f"Response validation failed: {problems}") from e

    def coerce(value: Any) -> msgspec.Raw:
        if isinstance(value, QuerySet):
            value = list(value)
        return encode(value)

    async def coerce_async(value: Any) -> msgspec.Raw:
        if isinstance(value, QuerySet):
            value = await run_in_orm_executor(list, value)
        return encode(value)

    return coerce, coerce_async
