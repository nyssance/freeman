"""
Django ORM Integration Tests for Class-Based Views.

An APIView method reads real rows with the Django async ORM.
"""

import pytest

from freeman import BoltAPI
from freeman.testing import TestClient
from freeman.views import APIView
from tests.test_models import Article

# --- Fixtures ---


@pytest.fixture
def api():
    """Create a fresh BoltAPI instance for each test."""
    return BoltAPI()


@pytest.fixture
def sample_articles(db):
    """Create sample articles in the database."""
    articles = []
    for i in range(1, 4):
        article = Article.objects.create(
            title=f"Article {i}", content=f"Content {i}", author="Test Author", is_published=(i % 2 == 0)
        )
        articles.append(article)
    return articles


# --- Schemas ---


# --- Edge Cases ---


@pytest.mark.django_db(transaction=True)
def test_async_queryset_iteration(api, sample_articles):
    """Test that async queryset iteration works correctly."""

    @api.view("/articles")
    class ArticleListView(APIView):
        async def get(self, request) -> list:
            articles = []
            # Test async iteration of a queryset
            async for article in Article.objects.all():
                articles.append({"id": article.id, "title": article.title})
            return articles

    with TestClient(api) as client:
        response = client.get("/articles")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 3
        assert all("id" in article and "title" in article for article in data)
