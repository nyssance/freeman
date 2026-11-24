from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from freeman import BoltAPI
from freeman.exceptions import ResponseValidationError
from freeman.middleware import Middleware
from freeman.responses import EventSourceResponse
from freeman.testing import TestClient

# The "_type" key names the variant, as a discriminator does.
_TAGGED = ConfigDict(validate_by_name=True, serialize_by_alias=True)


class Cat(BaseModel):
    model_config = _TAGGED
    kind: Literal["Cat"] = Field("Cat", alias="_type")
    name: str
    meows: bool = True


class Dog(BaseModel):
    model_config = _TAGGED
    kind: Literal["Dog"] = Field("Dog", alias="_type")
    name: str
    barks: bool = True


def test_union_response_model():
    api = BoltAPI()

    @api.get("/pet/{pet_type}", response_model=Cat | Dog)
    async def get_pet(pet_type: str):
        if pet_type == "cat":
            return {"name": "Whiskers", "meows": True, "type": "cat", "_type": "Cat"}
        return {"name": "Fido", "barks": True, "type": "dog", "_type": "Dog"}

    with TestClient(api) as client:
        # Test cat response
        resp = client.get("/pet/cat")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Whiskers", "meows": True, "_type": "Cat"}

        # Test dog response
        resp = client.get("/pet/dog")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Fido", "barks": True, "_type": "Dog"}


def test_union_return_annotation():
    api = BoltAPI()

    @api.get("/pet/{pet_type}")
    async def get_pet(pet_type: str) -> Cat | Dog:
        if pet_type == "cat":
            return Cat(name="Whiskers")
        return Dog(name="Fido")

    with TestClient(api) as client:
        resp = client.get("/pet/cat")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Whiskers", "meows": True, "_type": "Cat"}

        resp = client.get("/pet/dog")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Fido", "barks": True, "_type": "Dog"}


def test_union_validation_error():
    api = BoltAPI(validate_response=True)

    @api.get("/bad-pet", response_model=Cat | Dog)
    async def get_bad_pet():
        return {"flies": True}

    with TestClient(api, raise_server_exceptions=False) as client:
        resp = client.get("/bad-pet")
        assert resp.status_code == 500
        assert b"Response validation error" in resp.content


def test_optional_response():
    api = BoltAPI()

    @api.get("/maybe-pet/{pet_type}", response_model=Cat | None)
    async def get_maybe_pet(pet_type: str):
        if pet_type == "cat":
            return Cat(name="Whiskers")
        return None

    with TestClient(api) as client:
        resp = client.get("/maybe-pet/cat")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Whiskers", "meows": True, "_type": "Cat"}

        resp = client.get("/maybe-pet/none")
        assert resp.status_code == 200
        assert resp.json() is None


def test_typing_union_compatibility():
    api = BoltAPI()

    @api.get("/typing-union", response_model=Cat | Dog)
    async def get_typing_union():
        return Dog(name="Fido")

    with TestClient(api) as client:
        resp = client.get("/typing-union")
        assert resp.status_code == 200
        assert resp.json() == {"name": "Fido", "barks": True, "_type": "Dog"}


# ---------------------------------------------------------------------------
# Regression tests: the inline-encoding fast path must delegate edge cases to
# serialize_response. Each test asserts a behavior the slow path provided
# uniformly; the inline path used to short-circuit and emit wrong output.
# ---------------------------------------------------------------------------


class Tagged(BaseModel):
    """A model used to force the inline path."""

    model_config = _TAGGED
    kind: Literal["Tagged"] = Field("Tagged", alias="_type")
    name: str


def test_inline_path_204_with_none_returns_empty_body():
    """status_code=204 + response_model=Struct + handler returns None → empty 204."""
    api = BoltAPI()

    @api.delete("/items/{item_id}", status_code=204, response_model=Tagged)
    async def delete_item(item_id: int):
        return None

    with TestClient(api) as client:
        resp = client.delete("/items/1")
        assert resp.status_code == 204
        assert resp.content == b""
        # RFC 9110 §15.3.5: a 204 must not carry a body.


def test_inline_path_struct_with_response_class_sse():
    """response_class=EventSourceResponse must not be shadowed by the struct inline path."""
    api = BoltAPI()

    @api.get("/events", response_class=EventSourceResponse, response_model=Tagged)
    async def events():
        async def gen():
            yield Tagged(name="a")
            yield Tagged(name="b")

        return gen()

    with TestClient(api) as client:
        resp = client.get("/events")
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert "text/event-stream" in ct, f"expected SSE content-type, got {ct!r}"


def test_inline_path_with_generator_auto_wraps_to_streaming():
    """Async-generator return under a stream-typed annotation must auto-wrap.

    ``msgspec.convert`` refuses to consume an async generator, so the inline
    path must defer to ``_build_auto_streaming_response`` which wraps it as
    ndjson.
    """
    api = BoltAPI()

    @api.get("/feed")
    async def feed() -> AsyncIterator[Tagged]:
        async def gen():
            yield Tagged(name="a")
            yield Tagged(name="b")

        return gen()

    with TestClient(api) as client:
        resp = client.get("/feed")
        assert resp.status_code == 200
        assert b"_type" in resp.content


def test_inline_path_with_queryset_like_object_delegates_to_slow_path():
    """QuerySet-shaped object under list[Struct] must delegate to the slow path.

    The slow path detects ``_iterable_class``/``model`` and runs the values()
    projection via sync_to_async. A direct ``msgspec.convert`` on a non-list
    duck-type raises, so the inline path must defer here.
    """
    api = BoltAPI()

    class FakeQuerySet:
        """Mimics the QuerySet duck-type that coerce_to_response_type checks for."""

        _iterable_class = object
        model = object

        def values(self, *names):
            return []

    @api.get("/qs")
    async def qs_endpoint() -> list[Tagged]:
        return FakeQuerySet()

    # The slow path refuses the duck-type, because it is not a QuerySet. The
    # error of the inline path would name the wire shape, not the handler result.
    with TestClient(api) as client, pytest.raises(ResponseValidationError, match="handler returned FakeQuerySet"):
        client.get("/qs")


class Row(BaseModel):
    id: int
    note: str = "default"


class RowObject:
    """An object with attributes in place of keys, as a model instance has."""

    def __init__(self, id: int):
        self.id = id


ROWS_JSON = [{"id": 1, "note": "default"}, {"id": 2, "note": "default"}]


class PassThroughMiddleware(Middleware):
    """Python middleware: the route cannot use the sync dispatch of Rust."""

    async def process_request(self, request):
        return await self.get_response(request)


@pytest.mark.parametrize(
    "api_kwargs",
    [
        {},
        {"middleware": [PassThroughMiddleware]},
        {"django_middleware": ["django.middleware.clickjacking.XFrameOptionsMiddleware"]},
    ],
    ids=["plain", "python-middleware", "django-middleware"],
)
def test_inline_list_path_converts_a_later_object_by_attribute(api_kwargs):
    """The inline list path checks only the first element.

    A list that starts with a dict and holds an object later gets the result of
    the slow path, which converts the object by attribute.
    """
    api = BoltAPI(**api_kwargs)

    @api.get("/trivial")
    async def trivial() -> list[Row]:
        return [{"id": 1}, RowObject(2)]

    @api.get("/trivial-query")
    async def trivial_query(start: int = 1) -> list[Row]:
        return [{"id": start}, RowObject(start + 1)]

    @api.get("/awaits")
    async def awaits() -> list[Row]:
        await asyncio.sleep(0)
        return [{"id": 1}, RowObject(2)]

    @api.get("/awaits-query")
    async def awaits_query(start: int = 1) -> list[Row]:
        await asyncio.sleep(0)
        return [{"id": start}, RowObject(start + 1)]

    @api.get("/sync")
    def sync_rows() -> list[Row]:
        return [{"id": 1}, RowObject(2)]

    @api.get("/sync-query")
    def sync_rows_query(start: int = 1) -> list[Row]:
        return [{"id": start}, RowObject(start + 1)]

    with TestClient(api) as client:
        for path in ("/trivial", "/trivial-query", "/awaits", "/awaits-query", "/sync", "/sync-query"):
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.text)
            assert resp.json() == ROWS_JSON, path
        # The handler gets its query parameter also when the list takes the slow path.
        for path in ("/trivial-query", "/awaits-query", "/sync-query"):
            resp = client.get(path, params={"start": 4})
            assert resp.status_code == 200, (path, resp.text)
            assert resp.json() == [{"id": 4, "note": "default"}, {"id": 5, "note": "default"}], path


def test_inline_list_path_reports_an_invalid_element(settings):
    """An invalid element still gives the validation error of that element."""
    # The 500 shows the validation errors only when DEBUG is on. This test reads them.
    settings.DEBUG = True
    api = BoltAPI()

    @api.get("/rows")
    async def rows() -> list[Row]:
        return [{"id": 1}, {"id": "x"}]

    with TestClient(api, raise_server_exceptions=False) as client:
        resp = client.get("/rows")
        assert resp.status_code == 500
        assert resp.json()["extra"]["validation_errors"] == [
            {
                "loc": ["response", 1, "id"],
                "msg": "Response validation failed for list[Row]: Input should be a valid integer, "
                "unable to parse string as an integer",
                "type": "int_parsing",
            }
        ]


def test_list_route_that_returns_one_struct_gives_a_response_validation_error():
    """A list route checks the shape of the result, not only the type of the Struct."""
    api = BoltAPI()

    @api.get("/rows")
    async def rows() -> list[Row]:
        return Row(id=1)

    with TestClient(api, raise_server_exceptions=False) as client:
        resp = client.get("/rows")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "Response validation error"


class RowModel(BaseModel):
    id: int


def test_response_validation_error_locates_the_error_in_the_response(settings):
    """A response validation error has the root "response", not the root of the request body."""
    # The 500 shows the validation errors only when DEBUG is on. This test reads them.
    settings.DEBUG = True
    api = BoltAPI()

    @api.get("/list")
    async def rows(request) -> list[Row]:
        return [{"id": 1}, {"id": "x"}]

    @api.get("/struct")
    def row() -> Row:
        return {"id": "x"}

    @api.get("/pydantic")
    async def pydantic_row() -> RowModel:
        return {"id": "x"}

    expected = {"/list": ["response", 1, "id"], "/struct": ["response", "id"], "/pydantic": ["response", "id"]}
    with TestClient(api, raise_server_exceptions=False) as client:
        for path, loc in expected.items():
            resp = client.get(path)
            assert resp.status_code == 500, path
            errors = resp.json()["extra"]["validation_errors"]
            assert [error["loc"] for error in errors] == [loc], (path, errors)
