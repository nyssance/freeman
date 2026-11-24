"""Regression tests for Serializer output of routes."""

from __future__ import annotations

import pytest

from freeman import BoltAPI
from freeman.serializers import Serializer, computed_field, field
from freeman.testing import TestClient
from tests.test_models import Article, Author, BlogPost


class BaseArticle(Serializer):
    id: int
    title: str


class ArticleOut(BaseArticle):
    content: str

    @computed_field
    def title_upper(self) -> str:
        return self.title.upper()


class SecretArticle(Serializer):
    id: int
    title: str
    content: str = field(write_only=True)


class AuthorMini(Serializer):
    id: int
    name: str


class PostOut(Serializer):
    id: int
    title: str
    author: AuthorMini

    @computed_field
    def title_len(self) -> int:
        return len(self.title)


class AuthorPostIds(Serializer):
    id: int
    posts: list[int]


@pytest.fixture
def article():
    return Article.objects.create(title="hello", content="the body", author="A1")


@pytest.mark.django_db(transaction=True)
def test_direct_reverse_relation_field_uses_async_fallback():
    api = BoltAPI()
    author = Author.objects.create(name="Ada", email="ada@x.io")
    post = BlogPost.objects.create(title="Post", content="body", author=author)

    @api.get("/authors", response_model=list[AuthorPostIds])
    async def authors():
        return Author.objects.all()

    with TestClient(api) as client:
        r = client.get("/authors")
        assert r.status_code == 200, r.text
        assert r.json() == [{"id": author.id, "posts": [post.id]}]


@pytest.mark.django_db(transaction=True)
def test_serializer_instances_with_write_only_fields_are_dumped(article):
    api = BoltAPI()

    @api.get("/one/{pk}", response_model=SecretArticle)
    async def one(pk: int):
        return SecretArticle.from_model(await Article.objects.aget(id=pk))

    @api.get("/many", response_model=list[SecretArticle])
    async def many():
        return [SecretArticle.from_model(a) async for a in Article.objects.all()]

    with TestClient(api) as client:
        r = client.get(f"/one/{article.id}")
        assert r.status_code == 200, r.text
        assert r.json() == {"id": article.id, "title": "hello"}

        r = client.get("/many")
        assert r.status_code == 200, r.text
        assert r.json() == [{"id": article.id, "title": "hello"}]


@pytest.mark.django_db(transaction=True)
def test_serializer_response_still_renders_with_validation_disabled(article):
    api = BoltAPI()

    @api.get("/one/{pk}", response_model=ArticleOut, validate_response=False)
    async def one(pk: int):
        return await Article.objects.aget(id=pk)

    with TestClient(api) as client:
        r = client.get(f"/one/{article.id}")
        assert r.status_code == 200, r.text
        assert r.json() == {"id": article.id, "title": "hello", "content": "the body", "title_upper": "HELLO"}
