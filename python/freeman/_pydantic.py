"""Pydantic at the two edges of a handler: the JSON body that comes in and the result that goes out.

A handler names a Pydantic model as a body parameter or as its return type.
Rust still does the routing, the path and query parameters, and the guards.
This module does only what needs the model:

- the body: ``validate_json`` from the raw bytes, with every error in one 422;
- the result: ``dump_json`` to bytes, which the JSON encoder passes through
  as they are (``msgspec.Raw``).
"""

import functools
import inspect
import types
from collections.abc import Callable
from typing import Annotated, Any, NamedTuple, Union, get_args, get_origin

import msgspec
from django.db.models import Model, QuerySet
from django.db.models.query import ModelIterable
from django.db.models.query_utils import DeferredAttribute
from django.db.models.signals import post_init
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_core import PydanticUndefined

from .concurrency import run_in_orm_executor
from .exceptions import RequestValidationError

__all__ = (
    "adapter_for",
    "build_body_extractor",
    "compile_queryset_reader",
    "compile_response_coercer",
    "encode_model",
    "is_pydantic_model",
    "names_pydantic_model",
    "param_fields",
    "request_errors",
    "response_failure",
    "ParamField",
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


class ParamField(NamedTuple):
    """One field of a Pydantic model that is a query, header or cookie parameter."""

    name: str
    # The key that Pydantic reads. It is also the query or cookie name. A
    # header name is this key in lowercase, with "-" for "_".
    key: str
    # The type with the constraints of the field.
    annotation: Any
    required: bool
    # ``inspect.Parameter.empty`` when the field has no plain default.
    default: Any
    description: str | None


@functools.cache
def param_fields(model: type[BaseModel]) -> tuple[ParamField, ...]:
    """The fields of a parameter model, with the key that Pydantic reads for each.

    The key is the validation alias when the config reads aliases (the
    default), else the field name. With ``validate_by_name`` too, the request
    still gives the alias: one key for each field. An ``AliasPath`` or an
    ``AliasChoices`` names no single key, so it fails here, when the route registers.
    """
    config = model.model_config
    by_alias = config.get("validate_by_alias", True)
    fields = []
    for name, info in model.__pydantic_fields__.items():
        alias = info.validation_alias
        if alias is not None and not isinstance(alias, str):
            raise TypeError(
                f"The field {name!r} of the parameter model {model.__name__} has the validation alias {alias!r}. "
                "A query, header or cookie parameter reads one key: give the field a str alias."
            )
        key = alias if alias is not None and by_alias else name
        default = inspect.Parameter.empty if info.default is PydanticUndefined else info.default
        fields.append(ParamField(name, key, info.rebuild_annotation(), info.is_required(), default, info.description))
    return tuple(fields)


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


def request_errors(exc: ValidationError, where: str) -> list[dict[str, Any]]:
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
            raise RequestValidationError(errors=request_errors(e, "body"), body=body_bytes) from e

    return extract


def response_failure(exc: ValidationError) -> TypeError:
    """The error of a result that does not fit its model: the field and the reason, not the value.

    The message goes to the log. The response handler makes a 500 of it.
    """
    problems = "; ".join(
        f"{'.'.join(map(str, error['loc'])) or 'value'}: {error['msg']}" for error in request_errors(exc, "")
    )
    return TypeError(f"Response validation failed: {problems}")


def encode_model(value: BaseModel) -> msgspec.Raw:
    """The JSON of a model instance, as bytes that the encoder does not read again."""
    return msgspec.Raw(type(value).__pydantic_serializer__.to_json(value))


_NO_DEFERRED_FIELDS = (frozenset(), True)


def _lookup_keys(model: type[BaseModel]) -> tuple[str, ...] | None:
    """The attribute that ``from_attributes`` reads for each field of ``model``, or None.

    Pydantic tries the alias first and then the name, as the config allows.
    A field gets one key only when one attribute decides its value: it has no
    alias, or the alias is the name, or the config reads only one of them.
    A field that reads both, an ``AliasPath`` or ``AliasChoices``, a model
    validator that sees the raw input, or a root model give None.
    """
    if getattr(model, "__pydantic_root_model__", False):
        return None
    if any(d.info.mode != "after" for d in model.__pydantic_decorators__.model_validators.values()):
        return None
    config = model.model_config
    by_alias = config.get("validate_by_alias", True)
    by_name = config.get("validate_by_name") or config.get("populate_by_name", False)
    keys = []
    for name, info in model.__pydantic_fields__.items():
        alias = info.validation_alias
        if alias is None or alias == name:
            keys.append(name)
        elif not isinstance(alias, str) or (by_alias and by_name):
            return None
        else:
            keys.append(alias if by_alias else name)
    return tuple(keys)


def _reads_columns(django_model: type[Model], keys: tuple[str, ...]) -> bool:
    """Whether each key is a concrete column of ``django_model``, read as values() reads it.

    The class attribute must be the plain column descriptor, so a property or
    a FileField does not pass. A model that builds its instances in its own
    way (``from_db``, ``__init__``, ``__getattribute__``) does not pass either.
    """
    if (
        django_model.from_db.__func__ is not Model.from_db.__func__
        or django_model.__init__ is not Model.__init__
        or django_model.__getattribute__ is not Model.__getattribute__
    ):
        return False
    fields = django_model._meta._forward_fields_map
    for key in keys:
        field = fields.get(key)
        if field is None or not field.concrete or field.attname != key:
            return False
        # The plain descriptor gives the stored value, as values() does. A field
        # with its own descriptor, such as a FileField, gives another object.
        if type(getattr(django_model, key, None)) is not DeferredAttribute:
            return False
    return True


def _projects(queryset: QuerySet) -> bool:
    """Whether ``queryset`` reads the same rows and values through ``values()``.

    An evaluated QuerySet, ``values()``, ``only()``/``defer()``, annotations,
    ``extra()``, ``distinct()``, a combinator such as ``union()``, prefetches,
    and a ``post_init`` receiver keep the instances.
    """
    query = queryset.query
    return (
        queryset._result_cache is None
        and queryset._iterable_class is ModelIterable
        and not queryset._prefetch_related_lookups
        and not query.annotations
        and not query.extra
        and not query.distinct
        and query.combinator is None
        and query.deferred_loading == _NO_DEFERRED_FIELDS
        and not post_init.has_listeners(queryset.model)
    )


def compile_queryset_reader(annotation: Any) -> tuple[Callable[[Any], Any], Callable[[Any], Any]] | None:
    """The QuerySet readers of ``list[Model]``, or None for another type or a model that cannot project.

    A reader gives the rows of a QuerySet as dicts of the attributes that the
    model reads, from ``values()``, so Django builds no instance. A QuerySet
    or a Django model that does not fit is loaded as instances. The model
    then validates the dicts as it validates the instances: same JSON.
    """
    args = get_args(annotation)
    if get_origin(annotation) is not list or len(args) != 1:
        return None
    model = args[0]
    if not is_pydantic_model(model) or (keys := _lookup_keys(model)) is None:
        return None
    # Each Django model gets its verdict once. Benign races under the GIL.
    verdicts: dict[type[Model], bool] = {}

    def read(queryset: QuerySet) -> list[Any]:
        django_model = queryset.model
        fits = verdicts.get(django_model)
        if fits is None:
            fits = verdicts[django_model] = _reads_columns(django_model, keys)
        if fits and _projects(queryset):
            return list(queryset.values(*keys))
        return list(queryset)

    async def read_async(queryset: QuerySet) -> list[Any]:
        return await run_in_orm_executor(read, queryset)

    return read, read_async


def compile_response_coercer(annotation: Any) -> tuple[Callable[[Any], Any], Callable[[Any], Any]]:
    """Build the response step of a return type that names a Pydantic model, once per route.

    The step gives the encoded JSON. A value that already is the model is
    encoded as it is. Each other value (a dict, a Django model instance, a
    list of them) goes through the model first, which reads an object by
    attribute. Thus only the fields of the model reach the client.

    Returns the sync step and its async form. They differ only for a
    QuerySet: the async form loads it on the ORM executor. A ``list[Model]``
    loads the QuerySet with ``values()`` when it can (``compile_queryset_reader``).
    """
    adapter = adapter_for(annotation)
    validate = adapter.validate_python
    dump = adapter.dump_json
    model = annotation if is_pydantic_model(annotation) else None
    readers = compile_queryset_reader(annotation)
    read = list if readers is None else readers[0]

    def encode(value: Any) -> msgspec.Raw:
        if model is not None and type(value) is model:
            return msgspec.Raw(model.__pydantic_serializer__.to_json(value))
        try:
            return msgspec.Raw(dump(validate(value, from_attributes=True)))
        except ValidationError as e:
            raise response_failure(e) from e

    def coerce(value: Any) -> msgspec.Raw:
        if isinstance(value, QuerySet):
            value = read(value)
        return encode(value)

    async def coerce_async(value: Any) -> msgspec.Raw:
        if isinstance(value, QuerySet):
            value = await run_in_orm_executor(read, value)
        return encode(value)

    return coerce, coerce_async
