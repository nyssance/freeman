---
icon: lucide/shapes
---

# Models

Freeman uses [Pydantic](https://docs.pydantic.dev/) models for the data of a route: the request body, the result, parameter models and form models, and the items of a page.

## Request body

A parameter whose type is a Pydantic model is the JSON body. Pydantic validates the raw bytes. A body that does not fit gives one 422 with every error:

```python
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from freeman import BoltAPI

api = BoltAPI()


class CreateUser(BaseModel):
    username: Annotated[str, Field(min_length=3, max_length=30)]
    email: str
    age: Annotated[int, Field(ge=0)] = 0

    @field_validator("username")
    @classmethod
    def no_admin(cls, value: str) -> str:
        if value == "admin":
            raise ValueError("This name is reserved")
        return value


@api.post("/users")
async def create_user(user: CreateUser) -> dict:
    return {"username": user.username}
```

Each error has the `loc` `["body", <field>, ...]`, the `msg` of Pydantic and its `type`.

`Body(embed=True)` reads the model at the key of the parameter name: `{"user": {...}}`. See [Request Handling](requests.md).

## Result

The return annotation, or `response_model`, gives the shape of the response. The model reads a dict by key, and a Django model instance or another object by attribute. Only the fields of the model go out:

```python
from pydantic import BaseModel

from freeman import BoltAPI
from myapp.models import User

api = BoltAPI()


class UserOut(BaseModel):
    id: int
    username: str


@api.get("/users/{user_id}")
async def get_user(user_id: int) -> UserOut:
    return await User.objects.aget(id=user_id)


@api.get("/users")
async def list_users() -> list[UserOut]:
    return User.objects.all()
```

A QuerySet for `list[Model]` reads only the columns of the model, with `values()`, when each field is a plain column. A result that does not fit the model is a response validation error (status 500).

## Common cases

| Need | Pydantic |
| --- | --- |
| A field constraint | `Annotated[int, Field(ge=1)]`, `Annotated[str, Field(max_length=50)]` |
| A validator | `@field_validator("name")` with `@classmethod`, or `@model_validator` |
| A field that input cannot set | A separate input model without the field, for example `UserIn` and `UserOut` |
| A computed field | `@computed_field` on a property |
| A nested model | A field whose type is another model, for example `author: AuthorOut` or `tags: list[TagOut]` |
| A field with another wire name | `Field(alias="userName")` |

A nested model that reads a relation of a Django model needs the related rows loaded first, with `select_related()` or `prefetch_related()`.

## Parameter models

A Pydantic model can group query, header or cookie parameters. Pydantic validates the model from the text of its fields:

```python
from typing import Annotated

from pydantic import BaseModel, Field

from freeman import BoltAPI
from freeman.param_functions import Header, Query

api = BoltAPI()


class Filters(BaseModel):
    status: str | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 10


class ClientHeaders(BaseModel):
    x_request_id: str | None = None


@api.get("/items")
async def list_items(
    filters: Annotated[Filters, Query()],
    headers: Annotated[ClientHeaders, Header()],
) -> dict:
    return {"status": filters.status, "limit": filters.limit, "request_id": headers.x_request_id}
```

A header model reads the field `x_request_id` from the header `x-request-id`.

## Form models

`Annotated[Model, Form()]` validates the model from the form fields. An `UploadFile` field takes a file part:

```python
from typing import Annotated

from pydantic import BaseModel

from freeman import BoltAPI, UploadFile
from freeman.param_functions import Form

api = BoltAPI()


class Profile(BaseModel):
    name: str
    avatar: UploadFile | None = None


@api.post("/profile")
async def update_profile(data: Annotated[Profile, Form()]) -> dict:
    return {"name": data.name, "has_avatar": data.avatar is not None}
```

## Paginated items

With `@paginate`, the item type of `list[Model]` converts each page. The response is the envelope `Page[Model]`. See [Pagination](pagination.md):

```python
from pydantic import BaseModel

from freeman import BoltAPI
from freeman.pagination import PageNumberPagination, paginate
from myapp.models import Article

api = BoltAPI()


class ArticleOut(BaseModel):
    id: int
    title: str


@api.get("/articles")
@paginate(PageNumberPagination)
async def list_articles() -> list[ArticleOut]:
    return Article.objects.all()
```
