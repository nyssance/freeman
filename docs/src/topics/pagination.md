---
icon: lucide/list
---

# Pagination

Freeman provides three pagination styles for handling large datasets efficiently.

## PageNumber Pagination

Classic page-based pagination:

```python
from freeman import BoltAPI, PageNumberPagination, paginate

api = BoltAPI()

class ArticlePagination(PageNumberPagination):
    page_size = 20
    max_page_size = 100  # Upper limit for the query value and for page_size
    page_size_query_param = "page_size"  # Allow client to customize

@api.get("/articles")
@paginate(ArticlePagination)
async def list_articles(request) -> list[ArticleOut]:
    return Article.objects.all()
```

Response:

```json
{
  "items": [...],
  "total": 150,
  "has_next": true,
  "has_previous": false,
  "page": 1,
  "page_size": 20,
  "total_pages": 8,
  "next_page": 2
}
```

The response leaves out a field that has no value. The first page has no `previous_page`. The last page has no `next_page`.

Query: `/articles?page=2&page_size=10`

A page number after the last page gives the last page. With no rows, each page number gives page 1.

## LimitOffset Pagination

Flexible offset-based pagination:

```python
from freeman import LimitOffsetPagination, paginate

@api.get("/articles", response_model=list[ArticleOut])
@paginate(LimitOffsetPagination)
async def list_articles(request):
    return Article.objects.all()
```

Query: `/articles?limit=10&offset=20`

Response includes `items`, `total`, `has_next`, `has_previous`, `limit`, `offset`. An offset at or after the last row gives no items.

## Cursor Pagination

Efficient pagination for large datasets and real-time feeds:

```python
from freeman import CursorPagination, paginate

class ArticlePagination(CursorPagination):
    page_size = 20
    ordering = "-created_at"  # Required: field to paginate by

@api.get("/articles")
@paginate(ArticlePagination)
async def list_articles(request) -> list[ArticleOut]:
    return Article.objects.all()
```

Response includes `page_size`, `next_cursor`, `has_next`, `has_previous`. A cursor goes forward only: the response has no cursor for the page before. `has_previous` is true when the request has a valid cursor. To go back, keep the cursors that the client used.

Query: `/articles?cursor=eyJ2IjoxMDB9`

The cursor is the `next_cursor` of the page before. It uses the URL-safe base64 alphabet, so put it into the query string as it is. A cursor that Freeman cannot read gives the first page. This includes a cursor with a value of the wrong type for the `ordering` field or the primary key.

The `ordering` field does not have to be unique. Freeman also orders by the primary key, so rows with equal values keep their place across pages. The cursor holds the value and the primary key of the last row. For an index, add the primary key after the ordering field, for example `models.Index(fields=["-created_at", "-id"])`.

A `values()` queryset must include the ordering field and the primary key, for example `Article.objects.values("id", "created_at", "title")`. Without one of them, the paginator cannot make the next cursor and raises `ValueError`.

The `ordering` field can be a ForeignKey. Then the cursor holds the key, not the related object. If the related model has `Meta.ordering`, Django orders a ForeignKey name by that ordering and not by the key. Thus the paginator raises `ValueError` for such a name. Use the key name, for example `ordering = "author_id"`.

The `ordering` field must not be null. A null value cannot make a cursor, and SQL compares no value with null. When the last row of a page has a null value, the paginator raises `ValueError`. Order by a field that is never null, or exclude the null values, for example `Article.objects.exclude(published_at=None)`.

The handler must return a QuerySet. The cursor is a filter on the database, and a list has no such filter. For a list, the paginator raises `TypeError`. Use `PageNumberPagination` or `LimitOffsetPagination` for a list.

## Specifying the item type

Two equivalent ways to specify the item type:

```python
# Option 1: Return type annotation (recommended)
@api.get("/articles")
@paginate(ArticlePagination)
async def list_articles(request) -> list[ArticleOut]:
    return Article.objects.all()

# Option 2: response_model parameter
@api.get("/articles", response_model=list[ArticleOut])
@paginate(ArticlePagination)
async def list_articles(request):
    return Article.objects.all()
```

Both approaches work identically. The item type filters the fields: only its declared fields are in the response. A Pydantic item type reads a dict by key and each other object by attribute. An item that does not fit gives a response validation error (status 500). See [Models](models.md).

## Class-based view with Pagination

```python
from freeman.views import APIView

@api.view("/articles")
class ArticleList(APIView):
    @paginate(ArticlePagination)
    async def get(self) -> list[ArticleOut]:
        return Article.objects.all()
```

## Custom response envelope

The default envelope is `Page[T]`. It has `items`, `total`,
`has_next`, `has_previous` and the fields of the pagination scheme. You can use
a different shape, for example the DRF shape `count/next/previous/results`. Set
`response_class` to a generic Pydantic model. Then override
`build_response()`. The response body and the OpenAPI schema
(`DRFPage_ArticleOut_`) use the new shape:

```python
from pydantic import BaseModel

class DRFPage[T](BaseModel):
    count: int
    next: str | None
    previous: str | None
    results: list[T]

class DRFPagination(PageNumberPagination):
    page_size = 20
    response_class = DRFPage

    def build_response(self, items, *, total, request, page, page_size, has_next, has_previous, **info):
        base = f"{request.path}?page_size={page_size}&page="
        return DRFPage(
            count=total,
            next=f"{base}{page + 1}" if has_next else None,
            previous=f"{base}{page - 1}" if has_previous else None,
            results=items,
        )
```

`build_response()` gets the serialized `items`, the `total`, the `request` and
the `has_next` and `has_previous` flags. It also gets the fields of the scheme
as keyword arguments:

- Page number: `page`, `page_size`, `total_pages`, `next_page`, `previous_page`
- Limit/offset: `limit`, `offset`
- Cursor: `page_size`, `next_cursor`

## Performance Considerations

### Avoiding N+1 Queries

A Pydantic item type with only column fields reads the page with `values()`.
A nested model reads a relation of each row. Load the relations in the QuerySet,
with `select_related` for ForeignKey and OneToOne fields and `prefetch_related`
for many-valued relations. Then a page costs a small, fixed number of queries:

```python
@api.get("/articles")
@paginate(ArticlePagination)
async def list_articles(request) -> list[ArticleOut]:
    return Article.objects.select_related("author").prefetch_related("tags")
```

### Cursor vs PageNumber for Large Datasets

For tables with millions of rows, prefer `CursorPagination` over `PageNumberPagination`. Cursor pagination uses indexed columns for efficient seeking, while page number pagination requires counting total rows.
