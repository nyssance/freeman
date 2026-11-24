"""Pydantic models as query, header and cookie parameters.

The cases follow ``test_typed_header_cookie_params.py`` and the struct model
cases of ``test_param_rich_types.py``. Pydantic converts the text of each
field and reports every error of the model in one 422.
"""

import uuid
from datetime import datetime
from typing import Annotated

import pytest
from openapi_spec_validator import validate
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from freeman import BoltAPI
from freeman.openapi import OpenAPIConfig
from freeman.openapi.schema_generator import SchemaGenerator
from freeman.params import Cookie, Header, Query
from freeman.testing import TestClient


class TypedHeaders(BaseModel):
    x_count: int
    x_ratio: float = 1.0


class AllHeaders(BaseModel):
    x_count: int
    x_limit: int = 10
    x_none: int | None = None
    x_debug: bool = False
    item_count: int = Field(0, alias="X-Item-Count")
    x_request_id: uuid.UUID | None = None
    x_since: datetime | None = None
    x_name: str = ""


class TypedCookies(BaseModel):
    count: int
    flag: bool = False


class AllCookies(BaseModel):
    count: int = 0
    page: int = 1
    dark_mode: bool = False
    item_count: int = Field(0, alias="item-count")
    session: str = ""


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = 1
    size: Annotated[int, Field(ge=1, le=100)] = 20
    tags: list[str] = []
    ids: set[int] | None = None
    search: str = Field(alias="q")


def _detail(response) -> str:
    return str(response.json()["detail"])


@pytest.fixture
def client():
    api = BoltAPI()

    @api.get("/header/struct")
    async def header_struct(headers: Annotated[TypedHeaders, Header()]):
        return {"count": headers.x_count, "ratio": headers.x_ratio}

    @api.get("/header/sync")
    def header_sync(headers: Annotated[TypedHeaders, Header()]):
        return {"count": headers.x_count, "ratio": headers.x_ratio}

    @api.get("/header/all")
    async def header_all(headers: Annotated[AllHeaders, Header()]):
        return headers.model_dump(mode="json")

    @api.get("/header/request")
    async def header_request(request, headers: Annotated[TypedHeaders, Header()]):
        return {"count": headers.x_count, "meta": request.META.get("HTTP_X_COUNT")}

    @api.get("/header/optional")
    async def header_optional(headers: Annotated[TypedHeaders | None, Header()] = None):
        return {"none": True} if headers is None else headers.model_dump()

    @api.get("/cookie/struct")
    async def cookie_struct(cookies: Annotated[TypedCookies, Cookie()]):
        return {"count": cookies.count, "flag": cookies.flag}

    @api.get("/cookie/all")
    async def cookie_all(cookies: Annotated[AllCookies, Cookie()]):
        return cookies.model_dump()

    @api.get("/query")
    async def query(filters: Annotated[Filters, Query()], extra: int = 0):
        return {
            "page": filters.page,
            "size": filters.size,
            "tags": filters.tags,
            "ids": None if filters.ids is None else sorted(filters.ids),
            "search": filters.search,
            "extra": extra,
        }

    @api.get("/query/sync")
    def query_sync(filters: Annotated[Filters, Query()]):
        return {"page": filters.page, "search": filters.search}

    with TestClient(api) as test_client:
        yield test_client


class TestHeaderModel:
    def test_a_header_model_converts_its_fields(self, client):
        response = client.get("/header/struct", headers={"X-Count": "4", "X-Ratio": "0.5"})
        assert response.status_code == 200, response.text
        assert response.json() == {"count": 4, "ratio": 0.5}
        assert client.get("/header/sync", headers={"X-Count": "9"}).json() == {"count": 9, "ratio": 1.0}

    def test_a_bad_header_is_a_422_that_names_the_header(self, client):
        response = client.get("/header/struct", headers={"X-Count": "four"})
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == [
            {
                "loc": ["header", "x-count"],
                "msg": "Input should be a valid integer, unable to parse string as an integer",
                "type": "int_parsing",
            }
        ]
        assert client.get("/header/sync", headers={"X-Count": "x"}).status_code == 422

    def test_a_missing_required_header_is_a_422(self, client):
        response = client.get("/header/struct")
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == [{"loc": ["header", "x-count"], "msg": "Field required", "type": "missing"}]

    def test_each_field_type_converts_and_defaults_apply(self, client):
        value = uuid.uuid4()
        response = client.get(
            "/header/all",
            headers={
                "X-Count": "5",
                "X-Limit": "25",
                "X-None": "7",
                "X-Debug": "yes",
                "X-Item-Count": "3",
                "X-Request-Id": str(value),
                "X-Since": "2024-01-02T03:04:05",
                "X-Name": "123",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "x_count": 5,
            "x_limit": 25,
            "x_none": 7,
            "x_debug": True,
            "item_count": 3,
            "x_request_id": str(value),
            "x_since": "2024-01-02T03:04:05",
            "x_name": "123",
        }
        defaults = client.get("/header/all", headers={"X-Count": "1"}).json()
        assert defaults == {
            "x_count": 1,
            "x_limit": 10,
            "x_none": None,
            "x_debug": False,
            "item_count": 0,
            "x_request_id": None,
            "x_since": None,
            "x_name": "",
        }

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("true", True), ("1", True), ("yes", True), ("false", False), ("0", False), ("off", False)],
    )
    def test_a_bool_header(self, client, raw, expected):
        response = client.get("/header/all", headers={"X-Count": "1", "X-Debug": raw})
        assert response.status_code == 200, response.text
        assert response.json()["x_debug"] is expected

    @pytest.mark.parametrize(
        ("header", "value"),
        [
            ("X-Limit", "many"),
            ("X-Debug", "maybe"),
            ("X-Item-Count", "three"),
            ("X-Request-Id", "nope"),
            ("X-Since", "yesterday"),
        ],
    )
    def test_a_bad_value_names_its_header(self, client, header, value):
        response = client.get("/header/all", headers={"X-Count": "1", header: value})
        assert response.status_code == 422, response.text
        assert [error["loc"] for error in response.json()["detail"]] == [["header", header.lower()]]

    def test_the_request_meta_keeps_the_header(self, client):
        assert client.get("/header/request", headers={"X-Count": "5"}).json() == {"count": 5, "meta": "5"}

    def test_an_optional_model_with_none_of_its_headers_is_the_default(self, client):
        assert client.get("/header/optional").json() == {"none": True}
        assert client.get("/header/optional", headers={"X-Count": "2"}).json() == {"x_count": 2, "x_ratio": 1.0}
        assert client.get("/header/optional", headers={"X-Ratio": "2"}).status_code == 422


class TestCookieModel:
    def test_a_cookie_model_converts_its_fields(self, client):
        response = client.get("/cookie/struct", headers={"Cookie": "count=2; flag=yes"})
        assert response.status_code == 200, response.text
        assert response.json() == {"count": 2, "flag": True}

    def test_a_bad_cookie_is_a_422_that_names_the_cookie(self, client):
        response = client.get("/cookie/struct", headers={"Cookie": "count=abc"})
        assert response.status_code == 422, response.text
        assert [error["loc"] for error in response.json()["detail"]] == [["cookie", "count"]]

    def test_each_field_type_converts_and_defaults_apply(self, client):
        response = client.get(
            "/cookie/all", headers={"Cookie": "count=5; page=3; dark_mode=true; item-count=8; session=42"}
        )
        assert response.json() == {"count": 5, "page": 3, "dark_mode": True, "item_count": 8, "session": "42"}
        assert client.get("/cookie/all").json() == {
            "count": 0,
            "page": 1,
            "dark_mode": False,
            "item_count": 0,
            "session": "",
        }

    @pytest.mark.parametrize("cookie", ["page=last", "dark_mode=maybe", "item-count=eight"])
    def test_a_bad_value_names_its_cookie(self, client, cookie):
        response = client.get("/cookie/all", headers={"Cookie": cookie})
        assert response.status_code == 422, response.text
        assert [error["loc"] for error in response.json()["detail"]] == [["cookie", cookie.split("=")[0]]]


class TestQueryModel:
    def test_a_query_model_converts_its_fields_and_collects_repeated_keys(self, client):
        response = client.get("/query?q=hi&page=2&size=5&tags=a&tags=b&ids=3&ids=1&ids=3&extra=7")
        assert response.status_code == 200, response.text
        assert response.json() == {"page": 2, "size": 5, "tags": ["a", "b"], "ids": [1, 3], "search": "hi", "extra": 7}
        assert client.get("/query?q=hi&tags=solo").json()["tags"] == ["solo"]
        assert client.get("/query/sync?q=x&page=4").json() == {"page": 4, "search": "x"}

    def test_every_error_of_the_model_is_in_one_422(self, client):
        response = client.get("/query?page=x&size=500&ids=1&ids=y")
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == [
            {
                "loc": ["query", "page"],
                "msg": "Input should be a valid integer, unable to parse string as an integer",
                "type": "int_parsing",
            },
            {"loc": ["query", "size"], "msg": "Input should be less than or equal to 100", "type": "less_than_equal"},
            {
                "loc": ["query", "ids", 1],
                "msg": "Input should be a valid integer, unable to parse string as an integer",
                "type": "int_parsing",
            },
            {"loc": ["query", "q"], "msg": "Field required", "type": "missing"},
        ]

    def test_extra_forbid_refuses_an_unknown_key_but_not_the_key_of_another_parameter(self, client):
        assert client.get("/query?q=a&extra=1").status_code == 200
        response = client.get("/query?q=a&color=red")
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == [
            {"loc": ["query", "color"], "msg": "Extra inputs are not permitted", "type": "extra_forbidden"}
        ]

    def test_the_alias_is_the_wire_name(self, client):
        response = client.get("/query?search=a")
        assert response.status_code == 422
        locs = [error["loc"] for error in response.json()["detail"]]
        assert ["query", "q"] in locs
        assert ["query", "search"] in locs  # extra="forbid": the field name is not a key


def _show(value):
    if isinstance(value, (set, frozenset)):
        return {"value": sorted(value), "type": type(value).__name__}
    if isinstance(value, list):
        return {"value": value, "type": "list", "item_types": sorted({type(item).__name__ for item in value})}
    return {"value": value, "type": type(value).__name__}


class _RenamedTags(BaseModel):
    tags: list[str] = Field(default_factory=list, alias="tag")


def test_a_renamed_sequence_field_leaves_its_python_name_as_one_value():
    api = BoltAPI()

    @api.get("/query")
    async def query(filters: Annotated[_RenamedTags, Query()], tags: str = "none"):
        return {"filters": filters.tags, "tags": tags}

    with TestClient(api) as client:
        response = client.get("/query?tag=a&tag=b&tags=solo")

    assert response.status_code == 200, response.text
    assert response.json() == {"filters": ["a", "b"], "tags": "solo"}


class _TagHeaders(BaseModel):
    x_name: str = "none"
    x_tags: list[str] = []
    x_ids: set[int] | None = None


def test_a_sequence_field_of_a_header_model_gives_one_item():
    api = BoltAPI()

    @api.get("/model")
    async def model(headers: Annotated[_TagHeaders, Header()]):
        return {"name": headers.x_name, "tags": headers.x_tags, "ids": _show(headers.x_ids)}

    with TestClient(api) as client:
        response = client.get("/model", headers={"X-Name": "bo", "X-Tags": "a, b", "X-Ids": "4"})
        assert response.status_code == 200, response.text
        # One item, as for a sequence header parameter. Bolt does not split it at a comma.
        assert response.json() == {"name": "bo", "tags": ["a, b"], "ids": {"value": [4], "type": "set"}}
        assert client.get("/model").json() == {"name": "none", "tags": [], "ids": {"value": None, "type": "NoneType"}}

        bad = client.get("/model", headers={"X-Ids": "x"})
        assert bad.status_code == 422
        assert bad.json()["detail"][0]["loc"] == ["header", "x-ids", 0]


class _LimitedTags(BaseModel):
    tags: Annotated[list[str], Field(max_length=1)] = []
    ids: Annotated[set[int], Field(max_length=1)] | None = None


@pytest.mark.parametrize(
    ("marker", "headers"),
    [(Header, {"Tags": "a", "Ids": "4"}), (Cookie, {"Cookie": "tags=a; ids=4"})],
    ids=["header", "cookie"],
)
def test_a_constrained_sequence_field_of_a_header_or_cookie_model_gives_one_item(marker, headers):
    api = BoltAPI()

    @api.get("/model")
    async def model(values: Annotated[_LimitedTags, marker()]):
        return {"tags": values.tags, "ids": _show(values.ids)}

    with TestClient(api) as client:
        response = client.get("/model", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"tags": ["a"], "ids": {"value": [4], "type": "set"}}


def test_a_bare_list_field_of_a_cookie_model_takes_the_value():
    api = BoltAPI()

    class Prefs(BaseModel):
        themes: list = []

    @api.get("/prefs")
    async def prefs(prefs: Annotated[Prefs, Cookie()]):
        return _show(prefs.themes)

    with TestClient(api) as client:
        response = client.get("/prefs", headers={"Cookie": "themes=dark"})

    assert response.status_code == 200, response.text
    assert response.json() == {"value": ["dark"], "type": "list", "item_types": ["str"]}


def test_a_one_value_source_refuses_a_default_of_two_items():
    api = BoltAPI()

    class Two(BaseModel):
        x_tags: list[str] = ["a", "b"]

    async def handler(headers: Annotated[Two, Header()]):
        return {}

    with pytest.raises(TypeError, match="gets at most one item"):
        api.get("/two")(handler)


# --- bytes --------------------------------------------------------------------------


class BytesModel(BaseModel):
    b: bytes
    items: list[bytes] = []
    short: Annotated[bytes, Field(max_length=3)] = b""
    maybe: bytes | None = None
    n: int = 0


class BytesHeaders(BaseModel):
    x_b: bytes
    x_n: int = 0


def _utf8(text):
    return {"type": "bytes", "hex": text.encode().hex()}


def _show_bytes(value):
    if isinstance(value, bytes):
        return {"type": "bytes", "hex": value.hex()}
    if value is None:
        return {"type": "NoneType"}
    return {"type": type(value).__name__, "items": [_show_bytes(item) for item in value]}


@pytest.fixture
def bytes_client():
    api = BoltAPI()

    def show(model):
        return {
            "b": _show_bytes(model.b),
            "items": _show_bytes(model.items),
            "short": _show_bytes(model.short),
            "maybe": _show_bytes(model.maybe),
            "n": model.n,
        }

    @api.get("/query")
    async def query(model: Annotated[BytesModel, Query()]):
        return show(model)

    @api.get("/cookie")
    async def cookie(model: Annotated[BytesModel, Cookie()]):
        return show(model)

    @api.get("/header")
    async def header(model: Annotated[BytesHeaders, Header()]):
        return {"x_b": _show_bytes(model.x_b), "x_n": model.x_n}

    with TestClient(api) as client:
        yield client


def test_a_bytes_field_gets_the_utf8_bytes_of_the_text(bytes_client):
    """A bytes field gets the UTF-8 bytes of the text, with no base64 decoding."""
    response = bytes_client.get("/query?b=YWJj&items=x&items=YWJj&short=%C3%A9a&maybe=abc&n=7")
    assert response.json() == {
        "b": _utf8("YWJj"),
        "items": {"type": "list", "items": [_utf8("x"), _utf8("YWJj")]},
        "short": _utf8("éa"),
        "maybe": _utf8("abc"),
        "n": 7,
    }
    one = {"b": _utf8("a b!"), "items": {"type": "list", "items": []}, "short": {"type": "bytes", "hex": ""}}
    assert bytes_client.get("/query", params={"b": "a b!"}).json() == one | {"maybe": {"type": "NoneType"}, "n": 0}
    cookie = bytes_client.get("/cookie", headers={"cookie": "b=YWJj; n=2"}).json()
    assert cookie == one | {"b": _utf8("YWJj"), "maybe": {"type": "NoneType"}, "n": 2}
    assert bytes_client.get("/header", headers={"x-b": "YWJj", "x-n": "3"}).json() == {"x_b": _utf8("YWJj"), "x_n": 3}


def test_a_bytes_field_checks_its_constraints_on_the_bytes(bytes_client):
    response = bytes_client.get("/query?b=abc&short=YWJj")

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"loc": ["query", "short"], "msg": "Data should have at most 3 bytes", "type": "bytes_too_long"}
    ]


# --- Registration ---------------------------------------------------------------------


def test_a_field_with_alias_choices_fails_at_registration():
    api = BoltAPI()

    class Choices(BaseModel):
        q: str = Field(validation_alias=AliasChoices("q", "query"))

    async def handler(filters: Annotated[Choices, Query()]):
        return {}

    with pytest.raises(TypeError, match="give the field a str alias"):
        api.get("/choices")(handler)


def test_a_model_key_and_a_typed_parameter_of_that_key_fail_at_registration():
    api = BoltAPI()

    class Paging(BaseModel):
        page: int = 1

    async def handler(paging: Annotated[Paging, Query()], page: int = 1):
        return {}

    with pytest.raises(TypeError, match="read the same query key 'page' with different types"):
        api.get("/paging")(handler)


# --- OpenAPI ---------------------------------------------------------------------------


class Documented(BaseModel):
    page: Annotated[int, Field(ge=1, description="The page number")] = 1
    tags: list[str] = []
    size: int | None = None
    search: str = Field(alias="q")


class DocumentedHeaders(BaseModel):
    x_tags: list[str] = []
    x_trace: str


def test_each_field_of_a_model_is_one_openapi_parameter():
    api = BoltAPI()

    @api.get("/items")
    async def items(
        filters: Annotated[Documented, Query()],
        headers: Annotated[DocumentedHeaders, Header()],
        cookies: Annotated[TypedCookies, Cookie()],
    ):
        return {}

    document = SchemaGenerator(api, OpenAPIConfig(title="Params", version="1")).generate().to_schema()
    validate(document)
    parameters = {
        (parameter["in"], parameter["name"]): parameter
        for parameter in document["paths"]["/items"]["get"]["parameters"]
    }

    assert list(parameters) == [
        ("query", "page"),
        ("query", "tags"),
        ("query", "size"),
        ("query", "q"),
        ("header", "x-tags"),
        ("header", "x-trace"),
        ("cookie", "count"),
        ("cookie", "flag"),
    ]
    page = parameters["query", "page"]
    assert page["description"] == "The page number"
    assert page["required"] is False
    assert page["schema"] == {"type": "integer", "minimum": 1, "default": 1}
    assert parameters["query", "tags"]["schema"] == {"type": "array", "items": {"type": "string"}, "default": []}
    # A query value cannot be null. The parameter is not required, and its schema is that of the type.
    assert parameters["query", "size"]["schema"] == {"type": "integer"}
    assert parameters["query", "size"]["required"] is False
    assert parameters["query", "q"]["required"] is True
    # A header gives one value, so its array holds one item at most.
    assert parameters["header", "x-tags"]["schema"] == {
        "type": "array",
        "items": {"type": "string"},
        "default": [],
        "maxItems": 1,
    }
    assert parameters["header", "x-trace"]["required"] is True
    assert parameters["cookie", "flag"]["schema"] == {"type": "boolean", "default": False}
    # A parameter model is not a component.
    assert "Documented" not in document.get("components", {}).get("schemas", {})
