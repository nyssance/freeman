"""Pydantic models as the body and as the result of a handler."""

import datetime as dt
import enum
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import pytest
from openapi_spec_validator import validate
from pydantic import BaseModel, Field, computed_field

from freeman import BoltAPI
from freeman.exceptions import ResponseValidationError
from freeman.openapi import OpenAPIConfig
from freeman.openapi.schema_generator import SchemaGenerator
from freeman.testing import TestClient


class Status(enum.StrEnum):
    draft = "draft"
    published = "published"


class Author(BaseModel):
    id: int
    username: str
    signature: str | None = None


class Post(BaseModel):
    id: int
    uuid: UUID
    title: str
    price: Decimal
    status: Status
    created_at: dt.datetime
    author: Author
    tags: list[str] = []

    @computed_field
    @property
    def slug(self) -> str:
        return self.title.lower().replace(" ", "-")


class CreatePost(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=100)]
    author_id: Annotated[int, Field(gt=0)]
    price: Annotated[Decimal, Field(ge=0)]
    tags: Annotated[list[Annotated[str, Field(max_length=20)]], Field(max_length=3)] = []
    status: Status = Status.draft


class Row:
    """An object with attributes, as an ORM row. It has a field that the model does not."""

    def __init__(self, **values):
        self.__dict__.update(values)


NOW = dt.datetime(2026, 11, 22, 8, 30, 15, tzinfo=dt.UTC)
AUTHOR = {"id": 9, "username": "ny", "signature": None}


def _post(**changes):
    values = {
        "id": 7, "uuid": UUID(int=1), "title": "Hello World", "price": Decimal("19.90"), "status": Status.published,
        "created_at": NOW, "author": AUTHOR, "tags": ["a"],
    }  # fmt: skip
    return values | changes


EXPECTED = {
    "id": 7, "uuid": "00000000-0000-0000-0000-000000000001", "title": "Hello World", "price": "19.90",
    "status": "published", "created_at": "2026-11-22T08:30:15Z", "author": AUTHOR, "tags": ["a"],
    "slug": "hello-world",
}  # fmt: skip


@pytest.fixture(scope="module")
def api():
    api = BoltAPI(openapi_config=OpenAPIConfig(title="Pydantic", version="1"))

    @api.get("/instance")
    async def instance() -> Post:
        return Post(**_post())

    @api.get("/dict")
    async def from_dict() -> Post:
        return _post(password="secret")

    @api.get("/object")
    def from_object() -> Post:
        return Row(**_post(author=Row(**AUTHOR, password="secret"), password="secret"))

    @api.get("/list")
    async def many() -> list[Post]:
        return [Post(**_post()), _post(id=8), Row(**_post(id=9, author=Row(**AUTHOR)))]

    @api.get("/optional")
    async def optional(missing: bool = False) -> Post | None:
        return None if missing else _post()

    @api.get("/wrong")
    async def wrong() -> Post:
        return {"id": "not a number"}

    @api.get("/unannotated")
    async def unannotated():
        return {"post": Post(**_post()), "count": 1}

    @api.post("/posts")
    async def create(data: CreatePost) -> Post:
        return _post(title=data.title, price=data.price, tags=data.tags, status=data.status)

    @api.post("/bulk")
    async def bulk(items: list[CreatePost]) -> dict[str, int]:
        return {"count": len(items)}

    @api.put("/posts/{post_id}")
    async def update(post_id: int, data: CreatePost, notify: bool = False) -> dict[str, object]:
        return {"post_id": post_id, "title": data.title, "notify": notify}

    return api


@pytest.fixture(scope="module")
def client(api):
    with TestClient(api) as client:
        yield client


@pytest.mark.parametrize("path", ["/instance", "/dict", "/object"])
def test_a_result_is_the_json_of_the_model(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.json() == EXPECTED


@pytest.mark.parametrize("path", ["/dict", "/object"])
def test_only_the_fields_of_the_model_reach_the_client(client, path):
    assert "secret" not in client.get(path).text


def test_a_list_result_takes_instances_dicts_and_objects(client):
    response = client.get("/list")
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == [7, 8, 9]
    assert response.json()[0] == EXPECTED


def test_an_optional_result_can_be_null(client):
    assert client.get("/optional").json() == EXPECTED
    response = client.get("/optional?missing=true")
    assert response.status_code == 200, response.text
    assert response.json() is None


def test_a_result_that_does_not_fit_the_model_is_a_server_error(client, api):
    with pytest.raises(ResponseValidationError) as raised:
        client.get("/wrong")
    message = str(raised.value)
    assert "id: Input should be a valid integer" in message
    assert "uuid: Field required" in message
    # The value that did not fit is not in the message.
    assert "not a number" not in message

    with TestClient(api, raise_server_exceptions=False) as quiet:
        assert quiet.get("/wrong").status_code == 500


def test_a_model_in_a_result_with_no_return_type_is_encoded(client):
    response = client.get("/unannotated")
    assert response.status_code == 200, response.text
    assert response.json() == {"post": EXPECTED, "count": 1}


def test_a_valid_body_reaches_the_handler_as_the_model(client):
    body = {"title": "New", "author_id": 3, "price": "5.50", "tags": ["x", "y"], "status": "published"}
    response = client.post("/posts", json=body)
    assert response.status_code == 200, response.text
    assert response.json() | {"slug": "new"} == EXPECTED | {
        "title": "New",
        "price": "5.50",
        "tags": ["x", "y"],
        "slug": "new",
    }


def test_every_error_of_a_body_is_in_one_response(client):
    body = {"title": "", "author_id": -1, "price": "-3", "tags": ["a"] * 5}
    response = client.post("/posts", json=body)
    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert {tuple(error["loc"]) for error in errors} == {
        ("body", "title"), ("body", "author_id"), ("body", "price"), ("body", "tags"),
    }  # fmt: skip
    assert all(set(error) == {"loc", "msg", "type"} for error in errors)


def test_a_decimal_field_takes_a_range(client):
    response = client.post("/posts", json={"title": "New", "author_id": 3, "price": "-0.01"})
    assert response.status_code == 422, response.text
    assert [error["loc"] for error in response.json()["detail"]] == [["body", "price"]]


def test_a_body_that_is_not_json_is_a_422(client):
    response = client.post("/posts", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"][0] == "body"


def test_a_list_body_reports_the_index_of_the_item(client):
    good = {"title": "a", "author_id": 1, "price": "1"}
    assert client.post("/bulk", json=[good, good]).json() == {"count": 2}
    response = client.post("/bulk", json=[good, good | {"author_id": 0}])
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", 1, "author_id"]


def test_path_and_query_parameters_work_beside_a_model_body(client):
    response = client.put("/posts/12?notify=true", json={"title": "t", "author_id": 1, "price": "1"})
    assert response.status_code == 200, response.text
    assert response.json() == {"post_id": 12, "title": "t", "notify": True}
    assert client.put("/posts/abc", json={"title": "t", "author_id": 1, "price": "1"}).status_code == 422


def test_the_openapi_document_holds_the_schemas_of_the_models(api):
    document = SchemaGenerator(api, api._openapi_config).generate().to_schema()
    validate(document)
    components = document["components"]["schemas"]
    assert {"Author", "CreatePost", "Post", "Status"} <= set(components)

    create = document["paths"]["/posts"]["post"]
    assert create["requestBody"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/CreatePost"}
    assert create["responses"]["200"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Post"}
    assert components["CreatePost"]["properties"]["title"] == {
        "maxLength": 100, "minLength": 1, "title": "Title", "type": "string",
    }  # fmt: skip
    assert components["CreatePost"]["required"] == ["title", "author_id", "price"]

    # A response holds the computed field, and it is required there.
    assert "slug" in components["Post"]["properties"]
    assert "slug" in components["Post"]["required"]

    many = document["paths"]["/list"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert many == {"type": "array", "items": {"$ref": "#/components/schemas/Post"}}

    update = document["paths"]["/posts/{post_id}"]["put"]
    assert [(p["name"], p["in"]) for p in update["parameters"]] == [("post_id", "path"), ("notify", "query")]
