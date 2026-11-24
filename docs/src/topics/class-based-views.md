---
icon: lucide/boxes
---

# Class-Based Views

Freeman supports class-based views for organizing related endpoints. `APIView` groups the HTTP methods of one path. For a resource with list and detail paths, use one view per path, or function routes.

## APIView

Use `APIView` to group HTTP methods for a single resource:

```python
from freeman import BoltAPI
from freeman.views import APIView

api = BoltAPI()

@api.view("/hello")
class HelloView(APIView):
    async def get(self, request):
        return {"message": "Hello"}

    async def post(self, request, name: str):
        return {"message": f"Hello, {name}"}
```

This creates:

- `GET /hello` - Calls the `get` method
- `POST /hello` - Calls the `post` method

Freeman passes the request to the parameter with the name `request` or `req`, at each position. `self.request` is the same request. A method with no such parameter can still read `self.request`:

```python
@api.view("/whoami")
class WhoAmIView(APIView):
    async def get(self):
        return {"path": self.request.path}
```

### Available methods

Implement any of these methods:

```python
@api.view("/resource")
class ResourceView(APIView):
    async def get(self, request):
        """Handle GET requests"""
        return {"method": "GET"}

    async def post(self, request):
        """Handle POST requests"""
        return {"method": "POST"}

    async def put(self, request):
        """Handle PUT requests"""
        return {"method": "PUT"}

    async def patch(self, request):
        """Handle PATCH requests"""
        return {"method": "PATCH"}

    async def delete(self, request):
        """Handle DELETE requests"""
        return {"method": "DELETE"}

    async def query(self, request):
        """Handle QUERY requests (a read that takes a request body)"""
        return {"method": "QUERY"}
```

### Class-level configuration

Set authentication and permissions at the class level:

```python
from freeman.auth import JWTAuthentication, IsAuthenticated

@api.view("/protected")
class ProtectedView(APIView):
    auth = [JWTAuthentication()]
    guards = [IsAuthenticated()]

    async def get(self, request):
        return {"user_id": request.user.id}
```

### Path parameters

Handle path parameters in your methods:

```python
@api.view("/users/{user_id}")
class UserView(APIView):
    async def get(self, request, user_id: int):
        return {"user_id": user_id}

    async def put(self, request, user_id: int, data: UserUpdate):
        return {"user_id": user_id, "updated": True}
```

## A resource with list and detail paths

An `APIView` has one path. A resource with a collection and an item uses two views. Pydantic models give the request body and the response:

```python
from pydantic import BaseModel

from freeman import BoltAPI
from freeman.exceptions import HTTPException
from freeman.pagination import PageNumberPagination, paginate
from freeman.views import APIView
from myapp.models import Article

api = BoltAPI()


class ArticleIn(BaseModel):
    title: str
    content: str


class ArticleOut(BaseModel):
    id: int
    title: str
    content: str


async def get_article(pk: int) -> Article:
    try:
        return await Article.objects.aget(pk=pk)
    except Article.DoesNotExist as e:
        raise HTTPException(status_code=404, detail="Not found") from e


@api.view("/articles")
class ArticleList(APIView):
    @paginate(PageNumberPagination)
    async def get(self) -> list[ArticleOut]:
        return Article.objects.all()

    async def post(self, data: ArticleIn) -> ArticleOut:
        return await Article.objects.acreate(**data.model_dump())


@api.view("/articles/{pk}")
class ArticleDetail(APIView):
    async def get(self, pk: int) -> ArticleOut:
        return await get_article(pk)

    async def delete(self, pk: int) -> None:
        article = await get_article(pk)
        await article.adelete()
```

The status code of a view applies to each method. For a different status code per method, such as 201 for a create, use the `status_code` class attribute on a separate view, or a function route (`@api.post("/articles", status_code=201)`).

## Sync handlers

APIView supports synchronous handlers:

```python
@api.view("/sync-resource")
class SyncResourceView(APIView):
    def get(self, request):  # Note: not async
        return {"sync": True}
```

Sync handlers are automatically wrapped to run in a thread pool.
