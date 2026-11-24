"""Pydantic models as the body and as the result of a handler."""

import datetime as dt
import enum
import re
import typing
from decimal import Decimal
from typing import Annotated
from uuid import UUID

import msgspec
import pytest
from asgiref.sync import sync_to_async
from django.core.exceptions import EmptyResultSet
from django.db import connection, models
from django.db.models import Count
from django.db.models.signals import post_init
from openapi_spec_validator import validate
from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field, computed_field, model_validator

from freeman import BoltAPI
from freeman._pydantic import compile_queryset_reader
from freeman.exceptions import ResponseValidationError
from freeman.openapi import OpenAPIConfig
from freeman.openapi.schema_generator import SchemaGenerator
from freeman.params import Body
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


def test_an_optional_plain_field_is_a_type_list_and_an_optional_model_keeps_its_union(api):
    """Some client generators read ``"type": [T, "null"]`` only, and leave an ``anyOf`` with null out."""
    components = SchemaGenerator(api, api._openapi_config).generate().to_schema()["components"]["schemas"]
    signature = components["Author"]["properties"]["signature"]
    assert signature["type"] == ["string", "null"]
    assert "anyOf" not in signature

    optional = SchemaGenerator(api, api._openapi_config).generate().to_schema()["paths"]["/optional"]["get"]
    schema = optional["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"anyOf": [{"$ref": "#/components/schemas/Post"}, {"type": "null"}]}


def test_plain_nullable_leaves_other_unions_alone():
    from freeman.openapi.schema_generator import _plain_nullable  # noqa: PLC0415

    three = {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}
    assert _plain_nullable(dict(three)) == three
    constrained = {"anyOf": [{"type": "string", "maxLength": 5}, {"type": "null"}], "title": "Name"}
    assert _plain_nullable(constrained) == {"type": ["string", "null"], "maxLength": 5, "title": "Name"}
    nested = {"properties": {"tags": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}}}
    assert _plain_nullable(nested)["properties"]["tags"] == {"type": ["array", "null"], "items": {"type": "string"}}


class RecordingQuerySet(models.QuerySet):
    """Record the iterable and the SQL of each load."""

    loads: list[tuple[str, str]] = []

    def _fetch_all(self):
        if self._result_cache is None:
            try:
                sql = str(self.query)
            except EmptyResultSet:
                sql = ""
            RecordingQuerySet.loads.append((self._iterable_class.__name__, sql))
        super()._fetch_all()


class Listing(models.Model):
    title = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    created_at = models.DateTimeField()
    note = models.TextField(default="")
    attachment = models.FileField(upload_to="listings/", blank=True)
    owner_id = models.BigIntegerField(null=True)

    objects = RecordingQuerySet.as_manager()

    class Meta:
        app_label = "freeman"

    @property
    def shout(self) -> str:
        return self.title.upper()


class Item(BaseModel):
    id: int
    title: str
    price: Decimal
    created_at: dt.datetime
    owner_id: int | None

    @computed_field
    @property
    def slug(self) -> str:
        return self.title.lower().replace(" ", "-")

    @model_validator(mode="after")
    def checked(self):
        # An after validator sees the model, so it does not stop the projection.
        return self


class AliasedItem(BaseModel):
    """``heading`` reads the ``title`` attribute."""

    id: int
    heading: str = Field(validation_alias="title")


class NameOnlyItem(BaseModel):
    """The config reads only the name, so the alias plays no part."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)

    title: str = Field(alias="label")


class PropertyItem(BaseModel):
    id: int
    shout: str


class FileItem(BaseModel):
    """The attribute is a ``FieldFile``, while values() gives the name."""

    id: int
    attachment: Annotated[str, BeforeValidator(str)]


class DefaultItem(BaseModel):
    id: int
    nickname: str = "anon"


class BeforeItem(BaseModel):
    id: int
    title: str

    @model_validator(mode="before")
    @classmethod
    def raw(cls, value):
        return value


class BothItem(BaseModel):
    """The config reads the alias and the name, so the projection cannot tell which one."""

    model_config = ConfigDict(validate_by_name=True)

    id: int
    heading: str = Field(alias="title")


class ChoicesItem(BaseModel):
    id: int
    heading: str = Field(validation_alias=AliasChoices("title", "note"))


def _all():
    return Listing.objects.order_by("id")


# name -> (model, QuerySet, projected columns or None)
CASES = {
    "plain": (Item, _all, {"id", "title", "price", "created_at", "owner_id"}),
    "alias": (AliasedItem, _all, {"id", "title"}),
    "name-only": (NameOnlyItem, _all, {"title"}),
    "sliced": (Item, lambda: _all()[1:3], {"id", "title", "price", "created_at", "owner_id"}),
    "empty": (Item, lambda: _all().filter(id__lt=0), {"id", "title", "price", "created_at", "owner_id"}),
    "property": (PropertyItem, _all, None),
    "file": (FileItem, _all, None),
    "default": (DefaultItem, _all, None),
    "before": (BeforeItem, _all, None),
    "both": (BothItem, _all, None),
    "choices": (ChoicesItem, _all, None),
    "only": (Item, lambda: _all().only("id", "title", "price", "created_at", "owner_id"), None),
    "defer": (Item, lambda: _all().defer("note"), None),
    "annotated": (Item, lambda: _all().annotate(n=Count("id")), None),
    "distinct": (Item, lambda: _all().distinct(), None),
    "union": (Item, lambda: Listing.objects.filter(id__lte=2).union(Listing.objects.filter(id__gt=2)).order_by("id"), None),
    "values": (Item, lambda: _all().values(), None),
}  # fmt: skip


def _register(api, name, model, queryset):
    @api.get(f"/qs/{name}")
    def from_queryset() -> list[model]:
        return queryset()

    @api.get(f"/list/{name}")
    async def from_list() -> list[model]:
        return await sync_to_async(lambda: list(queryset()))()

    @api.get(f"/aqs/{name}")
    async def from_queryset_async() -> list[model]:
        return queryset()


@pytest.fixture
def listings(django_db_blocker):
    if Listing._meta.db_table not in connection.introspection.table_names():
        with connection.schema_editor() as editor:
            editor.create_model(Listing)
    for index, title in enumerate(["Red Chair", "Blue Desk", "Green Lamp", "Tall Shelf"]):
        Listing.objects.create(
            title=title,
            price=Decimal(f"{index + 1}9.90"),
            created_at=NOW + dt.timedelta(days=index),
            owner_id=index if index % 2 else None,
        )
    api = BoltAPI()
    for name, (model, queryset, _) in CASES.items():
        _register(api, name, model, queryset)
    RecordingQuerySet.loads.clear()
    with TestClient(api) as client:
        yield client


def _selected_columns(sql: str) -> set[str]:
    return set(re.findall(r'"freeman_listing"\."(\w+)"', sql.split(" FROM ")[0]))


def _load(client, path):
    RecordingQuerySet.loads.clear()
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.content, list(RecordingQuerySet.loads)


ALL_COLUMNS = {"id", "title", "price", "created_at", "note", "attachment", "owner_id"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("prefix", ["qs", "aqs"])
@pytest.mark.parametrize("name", list(CASES))
def test_a_queryset_result_reads_only_the_columns_of_the_model_when_it_can(listings, name, prefix):
    _, _, columns = CASES[name]
    body, loads = _load(listings, f"/{prefix}/{name}")
    instances, _ = _load(listings, f"/list/{name}")

    # The JSON is the same as from a list of the instances.
    assert body == instances
    iterable, sql = loads[-1]
    if columns is None:
        assert iterable != "ValuesIterable" or name == "values"
        if name not in ("values", "annotated", "only", "defer", "union"):
            assert _selected_columns(sql) >= ALL_COLUMNS
    else:
        assert iterable == "ValuesIterable"
        if name != "empty":
            assert _selected_columns(sql) == columns, sql


@pytest.mark.django_db(transaction=True)
def test_the_projection_reads_the_rows_of_the_list(listings):
    assert len(listings.get("/qs/plain").json()) == 4
    assert listings.get("/qs/empty").json() == []
    sliced = listings.get("/aqs/sliced").json()
    assert [item["title"] for item in sliced] == ["Blue Desk", "Green Lamp"]
    assert sliced[0] == {
        "id": sliced[0]["id"], "title": "Blue Desk", "price": "29.90", "created_at": "2026-11-23T08:30:15Z",
        "owner_id": sliced[0]["owner_id"], "slug": "blue-desk",
    }  # fmt: skip
    first = listings.get("/qs/alias").json()[0]
    assert first == {"id": first["id"], "heading": "Red Chair"}
    assert listings.get("/aqs/name-only").json()[0] == {"title": "Red Chair"}


@pytest.mark.django_db(transaction=True)
def test_a_post_init_receiver_keeps_the_instances(listings):
    def touch(sender, instance, **kwargs):
        instance.title = instance.title.upper()

    post_init.connect(touch, sender=Listing)
    try:
        body, loads = _load(listings, "/qs/plain")
        instances, _ = _load(listings, "/list/plain")
    finally:
        post_init.disconnect(touch, sender=Listing)
    assert body == instances
    assert b"RED CHAIR" in body
    assert loads[-1][0] == "ModelIterable"


@pytest.mark.django_db(transaction=True)
def test_an_evaluated_queryset_is_not_loaded_again(listings):
    read, _ = compile_queryset_reader(list[Item])
    queryset = _all()
    rows = list(queryset)
    RecordingQuerySet.loads.clear()
    assert read(queryset) == rows
    assert RecordingQuerySet.loads == []
    assert type(read(_all())[0]) is dict


def test_only_a_list_of_a_model_gets_a_reader():
    assert compile_queryset_reader(Item) is None
    assert compile_queryset_reader(list[int]) is None
    assert compile_queryset_reader(typing.List) is None  # noqa: UP006
    assert compile_queryset_reader(list[Item] | None) is None
    assert compile_queryset_reader(list[Item]) is not None


# --- Body(embed=True) ------------------------------------------------------------------


class Owner(BaseModel):
    id: int


@pytest.fixture(scope="module")
def embedded_api():
    api = BoltAPI()

    @api.post("/embedded")
    async def embedded(post: Annotated[CreatePost, Body(embed=True)], owner: Annotated[Owner, Body(embed=True)]):
        return {"title": post.title, "owner": owner.id}

    @api.post("/embedded/optional")
    def embedded_optional(owner: Annotated[Owner | None, Body(embed=True, alias="user")] = None):
        return {"owner": None if owner is None else owner.id}

    return api


def test_an_embedded_model_is_at_the_key_of_its_parameter(embedded_api):
    with TestClient(embedded_api) as client:
        response = client.post(
            "/embedded", json={"post": {"title": "t", "author_id": 1, "price": "1"}, "owner": {"id": 4}, "x": 1}
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"title": "t", "owner": 4}
        assert client.post("/embedded/optional", json={"user": {"id": 2}}).json() == {"owner": 2}
        assert client.post("/embedded/optional").json() == {"owner": None}
        assert client.post("/embedded/optional", json={}).json() == {"owner": None}


def test_every_error_of_an_embedded_model_is_in_one_422_under_its_key(embedded_api):
    with TestClient(embedded_api) as client:
        response = client.post("/embedded", json={"post": {"title": "", "price": "-1"}, "owner": {"id": 1}})
        missing = client.post("/embedded", json={"owner": {"id": 1}})
        not_json = client.post("/embedded", content=b"{bad", headers={"content-type": "application/json"})

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [
        ["body", "post", "title"],
        ["body", "post", "author_id"],
        ["body", "post", "price"],
    ]
    assert missing.json()["detail"] == [{"loc": ["body", "post"], "msg": "Field required", "type": "missing"}]
    assert not_json.status_code == 422
    assert not_json.json()["detail"][0]["type"] == "json_invalid"


def test_the_openapi_body_of_an_embedded_model_is_an_object_with_its_key(embedded_api):
    document = SchemaGenerator(embedded_api, OpenAPIConfig(title="Embedded", version="1")).generate().to_schema()
    validate(document)
    body = document["paths"]["/embedded"]["post"]["requestBody"]
    # Each embedded parameter has its key in the one object.
    assert body["content"]["application/json"]["schema"] == {
        "type": "object",
        "properties": {
            "post": {"$ref": "#/components/schemas/CreatePost"},
            "owner": {"$ref": "#/components/schemas/Owner"},
        },
        "required": ["post", "owner"],
    }
    assert body["description"] == "Request body for post, owner"
    optional = document["paths"]["/embedded/optional"]["post"]["requestBody"]
    assert optional["required"] is False
    assert "user" in optional["content"]["application/json"]["schema"]["properties"]


def test_the_openapi_body_lists_each_embedded_struct_and_model():
    class Tag(msgspec.Struct):
        name: str

    api = BoltAPI()

    @api.post("/mixed")
    async def mixed(
        tag: Annotated[Tag, Body(embed=True, description="The tag")],
        owner: Annotated[Owner | None, Body(embed=True)] = None,
        count: Annotated[int, Body(embed=True, example=3)] = 1,
    ):
        return {}

    document = SchemaGenerator(api, OpenAPIConfig(title="Embedded", version="1")).generate().to_schema()
    validate(document)
    body = document["paths"]["/mixed"]["post"]["requestBody"]
    schema = body["content"]["application/json"]["schema"]
    assert list(schema["properties"]) == ["tag", "owner", "count"]
    assert schema["required"] == ["tag"]
    assert body["description"] == "The tag"
    assert body["content"]["application/json"]["example"] == {"count": 3}
