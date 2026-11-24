"""Pagination with a Pydantic item type.

The cases follow the Struct item cases of ``test_pagination.py``. A page of a
Pydantic item type has the envelope ``Page[Item]``: Pydantic validates and
encodes it, and a QuerySet page reads only the columns of the item model.
"""

import re
from types import SimpleNamespace

import pytest
from django.db import models
from openapi_spec_validator import validate
from pydantic import BaseModel, Field

from freeman import BoltAPI, CursorPagination, LimitOffsetPagination, PageNumberPagination, paginate
from freeman.openapi import OpenAPIConfig
from freeman.openapi.schema_generator import SchemaGenerator
from freeman.pagination import Page, extract_pagination_item_type
from freeman.testing import TestClient

from .test_models import Article


class ArticleOut(BaseModel):
    id: int
    title: str
    author: str
    is_published: bool


class ArticleWithDefault(BaseModel):
    id: int
    is_published: bool
    word_count: int = 0


class ArticleWithExtra(BaseModel):
    id: int
    title: str
    not_on_the_model: str


class RecordingQuerySet(models.QuerySet):
    """Record the SQL of each load."""

    loads: list[str] = []

    def _fetch_all(self):
        if self._result_cache is None:
            RecordingQuerySet.loads.append(str(self.query))
        super()._fetch_all()


def _articles():
    return RecordingQuerySet(model=Article).order_by("id")


def _columns(sql: str) -> set[str]:
    return set(re.findall(r'"freeman_article"\."(\w+)"', sql.split(" FROM ")[0]))


@pytest.fixture
def articles(db):
    for i in range(1, 26):
        Article.objects.create(
            title=f"Article {i}", content=f"Content {i}", author=f"Author {i % 5}", is_published=i % 2 == 0
        )
    RecordingQuerySet.loads.clear()


class Small(PageNumberPagination):
    page_size = 10


class SmallLimit(LimitOffsetPagination):
    default_limit = 10


class SmallCursor(CursorPagination):
    page_size = 10
    ordering = "id"


@pytest.fixture
def client(articles):
    api = BoltAPI()

    @api.get("/pages")
    @paginate(Small)
    async def pages(request, author: str | None = None) -> list[ArticleOut]:
        queryset = _articles()
        return queryset if author is None else queryset.filter(author=author)

    @api.get("/pages-sync")
    @paginate(Small)
    def pages_sync(request) -> list[ArticleOut]:
        return _articles()

    @api.get("/limit")
    @paginate(SmallLimit)
    async def limit(request) -> list[ArticleOut]:
        return _articles()

    @api.get("/cursor")
    @paginate(SmallCursor)
    async def cursor(request) -> list[ArticleOut]:
        return _articles()

    @api.get("/cursor-sync")
    @paginate(SmallCursor)
    def cursor_sync(request) -> list[ArticleOut]:
        return _articles()

    with TestClient(api) as test_client:
        yield test_client


def test_a_page_of_pydantic_items_was_a_500_and_is_now_the_envelope():
    """Before: ``PaginatedResponse[Model]`` named a Pydantic model, and Pydantic could not build a schema over the Struct."""
    api = BoltAPI()

    @api.get("/rows")
    @paginate(PageNumberPagination)
    async def rows(request) -> list[ArticleOut]:
        return [{"id": 1, "title": "a", "author": "x", "is_published": True}]

    with TestClient(api) as client:
        response = client.get("/rows")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [{"id": 1, "title": "a", "author": "x", "is_published": True}],
        "total": 1,
        "has_next": False,
        "has_previous": False,
        "page": 1,
        "page_size": 100,
        "total_pages": 1,
    }


@pytest.mark.django_db(transaction=True)
class TestPageNumber:
    @pytest.mark.parametrize("path", ["/pages", "/pages-sync"])
    def test_the_first_page(self, client, path):
        response = client.get(path)
        assert response.status_code == 200, response.text
        data = response.json()
        assert [item["title"] for item in data["items"]] == [f"Article {i}" for i in range(1, 11)]
        assert set(data["items"][0]) == {"id", "title", "author", "is_published"}
        assert {key: data[key] for key in data if key != "items"} == {
            "total": 25,
            "has_next": True,
            "has_previous": False,
            "page": 1,
            "page_size": 10,
            "total_pages": 3,
            "next_page": 2,
        }

    def test_the_middle_and_the_last_page(self, client):
        middle = client.get("/pages?page=2").json()
        assert (middle["previous_page"], middle["next_page"], middle["has_previous"]) == (1, 3, True)
        last = client.get("/pages?page=3").json()
        assert len(last["items"]) == 5
        assert last["has_next"] is False
        assert "next_page" not in last

    def test_a_custom_page_size_and_a_filter_count_the_filtered_rows(self, client):
        data = client.get("/pages?author=Author 1&page_size=2").json()
        assert data["total"] == 5
        assert data["page_size"] == 2
        assert [item["author"] for item in data["items"]] == ["Author 1", "Author 1"]

    def test_an_empty_page(self, client):
        data = client.get("/pages?author=nobody").json()
        assert (data["items"], data["total"], data["has_next"]) == ([], 0, False)

    def test_the_page_reads_only_the_columns_of_the_item_model(self, client):
        RecordingQuerySet.loads.clear()
        assert client.get("/pages?page=2").status_code == 200
        (sql,) = RecordingQuerySet.loads
        assert _columns(sql) == {"id", "title", "author", "is_published"}
        assert "LIMIT 10" in sql
        assert "OFFSET 10" in sql


@pytest.mark.django_db(transaction=True)
class TestLimitOffset:
    def test_limit_and_offset(self, client):
        data = client.get("/limit?limit=5&offset=20").json()
        assert [item["id"] for item in data["items"]] == [
            item["id"] for item in client.get("/pages?page=3").json()["items"]
        ]
        assert {key: data[key] for key in data if key != "items"} == {
            "total": 25,
            "has_next": False,
            "has_previous": True,
            "limit": 5,
            "offset": 20,
        }

    def test_the_page_reads_only_the_columns_of_the_item_model(self, client):
        RecordingQuerySet.loads.clear()
        client.get("/limit?limit=3")
        assert _columns(RecordingQuerySet.loads[-1]) == {"id", "title", "author", "is_published"}


@pytest.mark.django_db(transaction=True)
class TestCursor:
    @pytest.mark.parametrize("path", ["/cursor", "/cursor-sync"])
    def test_the_pages_follow_the_cursor(self, client, path):
        first = client.get(path).json()
        assert len(first["items"]) == 10
        assert first["has_next"] is True
        assert first["has_previous"] is False
        assert "total" in first
        second = client.get(path, params={"cursor": first["next_cursor"]}).json()
        third = client.get(path, params={"cursor": second["next_cursor"]}).json()
        ids = [item["id"] for page in (first, second, third) for item in page["items"]]
        assert ids == sorted(ids)
        assert len(set(ids)) == 25
        assert third["has_next"] is False
        assert "next_cursor" not in third


def test_page_items_validate_as_a_plain_list_response():
    """A page has the items, the defaults and the error of the same route with no @paginate."""
    api = BoltAPI()

    def mixed():
        return [
            ArticleOut(id=1, title="model", author="a", is_published=True),
            {"id": 7, "title": "dict", "author": "b", "is_published": False},
        ]

    def objects():
        return [SimpleNamespace(id=1, is_published=True), SimpleNamespace(id=2, is_published=False, word_count=9)]

    @api.get("/plain/mixed")
    async def plain_mixed() -> list[ArticleOut]:
        return mixed()

    @api.get("/paged/mixed")
    @paginate(PageNumberPagination)
    async def paged_mixed(request) -> list[ArticleOut]:
        return mixed()

    @api.get("/plain/default")
    async def plain_default() -> list[ArticleWithDefault]:
        return objects()

    @api.get("/paged/default")
    @paginate(PageNumberPagination)
    async def paged_default(request) -> list[ArticleWithDefault]:
        return objects()

    @api.get("/plain/missing")
    async def plain_missing() -> list[ArticleWithExtra]:
        return objects()

    @api.get("/paged/missing")
    @paginate(PageNumberPagination)
    async def paged_missing(request) -> list[ArticleWithExtra]:
        return objects()

    with TestClient(api, raise_server_exceptions=False) as client:
        for name in ("mixed", "default"):
            plain, paged = client.get(f"/plain/{name}"), client.get(f"/paged/{name}")
            assert (plain.status_code, paged.status_code) == (200, 200), name
            assert paged.json()["items"] == plain.json(), name
        assert client.get("/paged/default").json()["items"][0] == {"id": 1, "is_published": True, "word_count": 0}
        plain, paged = client.get("/plain/missing"), client.get("/paged/missing")
        assert (plain.status_code, paged.status_code) == (500, 500)
        assert paged.json() == plain.json()


@pytest.mark.django_db(transaction=True)
def test_a_queryset_page_of_a_model_with_a_field_that_is_not_a_column_is_a_500(articles):
    api = BoltAPI()

    @api.get("/missing")
    @paginate(PageNumberPagination)
    async def missing(request) -> list[ArticleWithExtra]:
        return _articles()

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.get("/missing")

    assert response.status_code == 500
    # The model has a field that is no column, so the page reads the instances.
    assert _columns(RecordingQuerySet.loads[-1]) >= {"id", "title", "content", "created_at"}


def test_the_item_type_of_a_page():
    assert extract_pagination_item_type(Page[ArticleOut]) is ArticleOut
    assert extract_pagination_item_type(list[ArticleOut]) is ArticleOut


def test_the_openapi_envelope_names_the_item_component():
    api = BoltAPI()

    class Described(BaseModel):
        id: int
        title: str = Field(description="The title")

    @api.get("/rows")
    @paginate(LimitOffsetPagination)
    async def rows(request) -> list[Described]:
        return []

    document = SchemaGenerator(api, OpenAPIConfig(title="Pages", version="1")).generate().to_schema()
    validate(document)
    schema = document["paths"]["/rows"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    envelope = document["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]
    assert envelope["properties"]["items"] == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/Described"},
        "title": "Items",
    }
    assert envelope["required"] == ["items", "total", "has_next", "has_previous"]
    # An optional field that is None is not in the JSON, so the output schema has no default.
    assert envelope["properties"]["limit"] == {"type": ["integer", "null"], "title": "Limit"}
    assert "limit" not in envelope["required"]
    assert document["components"]["schemas"]["Described"]["properties"]["title"]["description"] == "The title"
