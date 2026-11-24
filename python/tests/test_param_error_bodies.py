"""The 422 body for a parameter that fails validation in Python.

Rust coerces scalar values. Python validates a Pydantic model parameter, a
sequence parameter, and a parameter with ``msgspec.Meta`` constraints. Each
error names the source in ``loc`` as a scalar parameter does: ``query``,
``header``, ``cookie`` or ``body``.
"""

from __future__ import annotations

from typing import Annotated

import msgspec
import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator

from freeman import BoltAPI, UploadFile
from freeman.middleware import ErrorHandlerMiddleware
from freeman.params import Cookie, File, Form, Header, Path, Query
from freeman.testing import TestClient


class Filters(BaseModel):
    page: int
    ids: list[int] = []
    size: Annotated[int, Field(ge=1)] = 10
    tag: Annotated[str, Field(min_length=2)] = Field(alias="t", default="ok")


class Headers(BaseModel):
    x_api_key: str
    x_count: Annotated[int, Field(ge=1)] = 1
    label: Annotated[str, Field(min_length=2)] = Field(alias="x_label", default="ok")


# Function scope: each test registers its routes, so a mutation tool sees
# each test reach the request-time closures of the extractors.
@pytest.fixture
def client():
    api = BoltAPI()

    @api.get("/query-struct")
    async def query_struct(filters: Annotated[Filters, Query()]):
        return {"page": filters.page, "ids": filters.ids}

    @api.get("/cookie-struct")
    async def cookie_struct(filters: Annotated[Filters, Cookie()]):
        return {"page": filters.page}

    @api.post("/form-struct")
    async def form_struct(filters: Annotated[Filters, Form()]):
        return {"page": filters.page}

    @api.get("/header-struct")
    async def header_struct(headers: Annotated[Headers, Header()]):
        return {"key": headers.x_api_key}

    @api.get("/sequence")
    async def sequence(ids: Annotated[list[int], Query()]):
        return {"ids": ids}

    @api.get("/constrained")
    async def constrained(n: Annotated[int, msgspec.Meta(ge=1), Query()]):
        return {"n": n}

    with TestClient(api) as client:
        yield client


def _missing(source: str, name: str) -> dict:
    return {"type": "missing", "loc": [source, name], "msg": "Field required"}


def _at_least_one(source: str, name: str) -> dict:
    return {"type": "greater_than_equal", "loc": [source, name], "msg": "Input should be greater than or equal to 1"}


def _too_short(source: str, name: str) -> dict:
    return {"type": "string_too_short", "loc": [source, name], "msg": "String should have at least 2 characters"}


@pytest.mark.parametrize(
    ("path", "kwargs", "source"),
    [
        ("/query-struct", {}, "query"),
        ("/cookie-struct", {}, "cookie"),
        ("/form-struct", {"data": {"size": "5"}}, "body"),
    ],
)
def test_missing_struct_field_names_the_source(client, path, kwargs, source):
    method = client.post if path == "/form-struct" else client.get
    response = method(path, **kwargs)

    assert response.status_code == 422
    assert response.json()["detail"] == [_missing(source, "page")]


class Renamed(BaseModel):
    page_number: int = Field(alias="pageNumber")


def test_missing_renamed_field_uses_its_wire_name():
    api = BoltAPI()

    @api.get("/renamed")
    async def renamed(filters: Annotated[Renamed, Query()]):
        return {"page": filters.page_number}

    with TestClient(api) as client:
        response = client.get("/renamed")

    assert response.status_code == 422
    assert response.json()["detail"] == [_missing("query", "pageNumber")]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = 1


class ClosedUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    cover: UploadFile | None = None


def test_a_struct_that_forbids_unknown_fields_ignores_the_keys_of_other_parameters():
    api = BoltAPI()

    @api.get("/query")
    async def query(filters: Annotated[Closed, Query()], limit: Annotated[int, Query(alias="max")] = 0):
        return {"page": filters.page, "limit": limit}

    @api.get("/cookie")
    async def cookie(filters: Annotated[Closed, Cookie()], token: Annotated[str, Cookie()] = ""):
        return {"page": filters.page, "token": token}

    @api.post("/form")
    async def form(
        filters: Annotated[Closed, Form()],
        note: Annotated[str, Form()] = "",
        doc: Annotated[UploadFile | None, File()] = None,
    ):
        return {"page": filters.page, "note": note, "doc": doc.filename if doc else None}

    @api.post("/upload")
    async def upload(doc: Annotated[UploadFile, File()], data: Annotated[ClosedUpload, Form()]):
        return {"doc": doc.filename, "title": data.title, "cover": data.cover.filename if data.cover else None}

    with TestClient(api) as client:
        assert client.get("/query?page=2&max=5").json() == {"page": 2, "limit": 5}
        assert client.get("/cookie", headers={"Cookie": "page=2; token=t"}).json() == {"page": 2, "token": "t"}
        response = client.post("/form", data={"page": "2", "note": "n"}, files={"doc": ("a.txt", b"a")})
        assert response.json() == {"page": 2, "note": "n", "doc": "a.txt"}
        files = {"doc": ("a.txt", b"a"), "cover": ("b.png", b"b")}
        response = client.post("/upload", data={"title": "t"}, files=files)
        assert response.json() == {"doc": "a.txt", "title": "t", "cover": "b.png"}

        # A key that no parameter declares is still an error. The error names the source.
        unknown = client.get("/query?page=2&max=5&other=1")
        assert unknown.status_code == 422
        assert unknown.json()["detail"] == [
            {"type": "extra_forbidden", "loc": ["query", "other"], "msg": "Extra inputs are not permitted"}
        ]
        unknown = client.post("/form", data={"page": "2", "other": "1"})
        assert unknown.status_code == 422
        assert unknown.json()["detail"][0]["loc"] == ["body", "other"]


def test_missing_header_struct_field_names_the_header(client):
    response = client.get("/header-struct")

    assert response.status_code == 422
    assert response.json()["detail"] == [_missing("header", "x-api-key")]


def test_invalid_header_struct_fields_name_the_headers(client):
    response = client.get("/header-struct", headers={"X-Api-Key": "k", "X-Count": "0", "X-Label": "x"})

    assert response.status_code == 422
    assert response.json()["detail"] == [_at_least_one("header", "x-count"), _too_short("header", "x-label")]


def test_header_struct_reports_a_missing_and_an_invalid_field_together(client):
    response = client.get("/header-struct", headers={"X-Count": "0"})

    assert response.status_code == 422
    assert response.json()["detail"] == [_missing("header", "x-api-key"), _at_least_one("header", "x-count")]


class AuthHeaders(BaseModel):
    token: str = Field(alias="X-Api-Key")


def test_missing_renamed_header_field_names_the_header():
    api = BoltAPI()

    @api.get("/auth")
    async def auth(headers: Annotated[AuthHeaders, Header()]):
        return {"token": headers.token}

    with TestClient(api) as client:
        assert client.get("/auth", headers={"X-Api-Key": "k"}).json() == {"token": "k"}
        response = client.get("/auth")

    assert response.status_code == 422
    assert response.json()["detail"] == [_missing("header", "x-api-key")]


def test_an_optional_struct_parameter_takes_its_default_when_the_request_has_none_of_its_keys():
    api = BoltAPI()

    @api.get("/query")
    async def query(filters: Annotated[Filters | None, Query()] = None):
        return {"page": filters.page if filters else None}

    @api.get("/cookie")
    async def cookie(filters: Annotated[Filters | None, Cookie()]):
        return {"page": filters.page if filters else None}

    @api.post("/form")
    async def form(filters: Annotated[Filters | None, Form()] = None):
        return {"page": filters.page if filters else None}

    @api.get("/header")
    async def header(headers: Annotated[Headers | None, Header()] = None):
        return {"key": headers.x_api_key if headers else None}

    with TestClient(api) as client:
        assert client.get("/query").json() == {"page": None}
        assert client.get("/query?other=1").json() == {"page": None}
        assert client.get("/query?page=3").json() == {"page": 3}
        assert client.get("/cookie").json() == {"page": None}
        assert client.get("/cookie", headers={"Cookie": "page=3"}).json() == {"page": 3}
        assert client.post("/form", data={"other": "1"}).json() == {"page": None}
        assert client.post("/form", data={"page": "3"}).json() == {"page": 3}
        assert client.get("/header").json() == {"key": None}
        assert client.get("/header", headers={"X-Api-Key": "k"}).json() == {"key": "k"}

        # With one of its keys in the request, the struct validates as a required one does.
        response = client.get("/query?size=5")
        assert response.status_code == 422
        assert response.json()["detail"] == [_missing("query", "page")]
        response = client.get("/header", headers={"X-Count": "2"})
        assert response.status_code == 422
        assert response.json()["detail"] == [_missing("header", "x-api-key")]


@pytest.mark.parametrize(
    ("path", "send", "source"),
    [
        ("/query-struct", lambda c, p: c.get(p, params={"page": "1", "size": "0", "t": "x"}), "query"),
        ("/form-struct", lambda c, p: c.post(p, data={"page": "1", "size": "0", "t": "x"}), "body"),
    ],
)
def test_invalid_struct_fields_each_get_one_error(client, path, send, source):
    response = send(client, path)

    assert response.status_code == 422
    assert response.json()["detail"] == [_at_least_one(source, "size"), _too_short(source, "t")]


def test_valid_sequence_field_gets_no_error_when_another_field_fails(client):
    # The sequence values are strings on the wire. Only `size` is wrong.
    response = client.get("/query-struct?page=1&ids=1&ids=2&size=0")

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [["query", "size"]]


def test_invalid_sequence_item_error_points_at_the_item(client):
    response = client.get("/sequence?ids=1&ids=x")

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "type": "validation_error",
            "loc": ["query", "ids"],
            "msg": "Expected `int`, got `str` - at `$[1]`",
            "input": ["1", "x"],
        }
    ]


def test_constrained_scalar_error_has_no_holder_path(client):
    response = client.get("/constrained?n=0")

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "validation_error", "loc": ["query", "n"], "msg": "Expected `int` >= 1", "input": 0}
    ]


class Range(BaseModel):
    x_low: int
    x_high: int

    @model_validator(mode="after")
    def check_order(self):
        if self.x_low > self.x_high:
            raise ValueError("x_low must be <= x_high")
        return self


def test_a_header_struct_check_across_fields_keeps_its_message():
    # No field fails alone, so the error list of the fields is empty. The error of the struct must stay.
    api = BoltAPI()

    @api.get("/range")
    async def range_route(bounds: Annotated[Range, Header()]):
        return {"low": bounds.x_low}

    with TestClient(api) as client:
        response = client.get("/range", headers={"X-Low": "5", "X-High": "1"})

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "value_error", "loc": ["header"], "msg": "Value error, x_low must be <= x_high"}
    ]


@pytest.mark.parametrize(
    ("send", "status"),
    [
        (lambda c: c.get("/header-struct"), 422),
        (lambda c: c.get("/query-struct"), 422),
        (lambda c: c.post("/body-struct", json={"page": "x"}), 422),
        (lambda c: c.get("/crash"), 500),
    ],
    ids=["header-struct", "query-struct", "body-struct", "server-error"],
)
def test_error_handler_middleware_keeps_a_validation_error_a_422(send, status):
    # The middleware turns other exceptions into a 500. A validation error is the fault of the client.
    api = BoltAPI(middleware=[ErrorHandlerMiddleware])

    @api.get("/header-struct")
    async def header_struct(headers: Annotated[Headers, Header()]):
        return {"key": headers.x_api_key}

    @api.get("/query-struct")
    async def query_struct(filters: Annotated[Filters, Query()]):
        return {"page": filters.page}

    @api.post("/body-struct")
    async def body_struct(filters: Filters):
        return {"page": filters.page}

    @api.get("/crash")
    async def crash():
        raise RuntimeError("server fault")

    with TestClient(api) as client:
        response = send(client)

    assert response.status_code == status


def test_a_path_constraint_error_names_the_path_parameter():
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def item(item_id: Annotated[int, msgspec.Meta(ge=5), Path()]):
        return {"id": item_id}

    with TestClient(api) as client:
        response = client.get("/items/2")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "item_id"]


def test_a_path_alias_that_the_route_does_not_have_is_a_422_that_names_it():
    """The route has no `{nope}` part, so the value is missing. A constraint does not change the answer."""
    api = BoltAPI()

    @api.get("/plain/{item_id}")
    async def plain(value: Annotated[int, Path(alias="nope")]):
        return {"value": value}

    @api.get("/constrained/{item_id}")
    async def constrained(value: Annotated[int, msgspec.Meta(ge=1), Path(alias="nope")]):
        return {"value": value}

    with TestClient(api) as client:
        for url in ("/plain/3", "/constrained/3"):
            response = client.get(url)
            assert response.status_code == 422, url
            assert response.json()["detail"] == "Missing required path parameter: nope"
