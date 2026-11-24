from __future__ import annotations

from typing import Annotated

from django.db import IntegrityError
from pydantic import BaseModel

from freeman import BoltAPI
from freeman.exceptions import HTTPException
from freeman.pagination import CursorPagination, LimitOffsetPagination, PageNumberPagination, paginate
from freeman.params import Depends
from freeman.views import APIView

from .models import User

api = BoltAPI(prefix="/users")


# ============================================================================
# Schemas (Pydantic models)
# ============================================================================


class UserFull(BaseModel):
    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    is_active: bool


class UserMini(BaseModel):
    id: int
    username: str


class UserListOut(BaseModel):
    id: int
    username: str
    email: str


class UserDetailOut(BaseModel):
    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    is_active: bool


class UserMiniOut(BaseModel):
    id: int
    username: str


class UserCreateIn(BaseModel):
    username: str
    email: str
    first_name: str = ""
    last_name: str = ""


class UserUpdateIn(BaseModel):
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    is_active: bool | None = None


# ============================================================================
# Function-Based Views (Original, for benchmarking)
# ============================================================================


@api.get("/")
async def users_root():
    return {"ok": True}


@api.get("/full10")
async def list_full_10() -> list[UserFull]:
    # Optimized: only fetch needed fields instead of all()
    return User.objects.only("id", "username", "email", "first_name", "last_name", "is_active")[:10]


@api.get("/sync-full10")
def list_full_10_sync() -> list[UserFull]:
    # Optimized: only fetch needed fields instead of all()
    return User.objects.only("id", "username", "email", "first_name", "last_name", "is_active")[:10]


@api.get("/mini10")
async def list_mini_10() -> list[UserMini]:
    # Already optimized: only() fetches just id and username
    return User.objects.only("id", "username")[:10]


@api.get("/seed")
async def seed_users(count: int = 1000) -> dict:
    """Create test users for benchmarking."""
    # Delete existing users first
    await User.objects.all().adelete()

    # Create users in bulk for performance
    users_to_create = [
        User(
            username=f"user{i}",
            email=f"user{i}@example.com",
            first_name=f"First{i}",
            last_name=f"Last{i}",
            is_active=True,
        )
        for i in range(count)
    ]

    created_users = await User.objects.abulk_create(users_to_create)
    return {"created": len(created_users), "count": count}


@api.post("/delete")
async def delete_all_users() -> dict:
    """Delete all users (for cleanup after benchmarking)."""
    count, _ = await User.objects.all().adelete()
    return {"deleted": count}


# ============================================================================


# Custom pagination class for examples
class SmallPagePagination(PageNumberPagination):
    """Custom pagination with smaller page size."""

    page_size = 10
    max_page_size = 50
    page_size_query_param = "page_size"  # Allow client to customize page size


# ----------------------------------------------------------------------------
# 1. Pagination of model instances
#    - Without an item type, each model field is in the response
# ----------------------------------------------------------------------------


@api.get("/paginated")
@paginate(SmallPagePagination)
async def list_users_paginated(request):
    """
    List users with page number pagination.

    Query params:
        - page: Page number (default: 1)
        - page_size: Items per page (default: 10, max: 50)

    Example: GET /users/paginated?page=2&page_size=20

    Response:
        {
            "items": [{"id": 1, "username": "john", "email": "john@example.com"}, ...],
            "total": 100,
            "page": 2,
            "page_size": 20,
            "total_pages": 5,
            "has_next": true,
            "has_previous": true,
            "next_page": 3,
            "previous_page": 1
        }
    """
    return User.objects.all()


# ----------------------------------------------------------------------------
# 2. Pagination with return type annotation (alternative syntax)
# ----------------------------------------------------------------------------


@api.get("/paginated-typed")
@paginate(SmallPagePagination)
async def list_users_typed(request) -> list[UserListOut]:
    """
    Same as above but using return type annotation instead of response_model.

    Both approaches work identically - choose based on preference.
    """
    return User.objects.all()


# ----------------------------------------------------------------------------
# 3. LimitOffset Pagination with an item type
# ----------------------------------------------------------------------------


@api.get("/paginated-offset", response_model=list[UserListOut])
@paginate(LimitOffsetPagination)
async def list_users_offset(request):
    """
    List users with limit-offset pagination.

    Query params:
        - limit: Number of items (default: 100, max: 1000)
        - offset: Starting position (default: 0)

    Example: GET /users/paginated-offset?limit=20&offset=40

    Response:
        {
            "items": [...],
            "total": 100,
            "limit": 20,
            "offset": 40,
            "has_next": true,
            "has_previous": true
        }
    """
    return User.objects.all()


# ----------------------------------------------------------------------------
# 4. Cursor Pagination with an item type
# ----------------------------------------------------------------------------


class UserCursorPagination(CursorPagination):
    """Cursor pagination ordered by creation date."""

    page_size = 20
    ordering = "-id"  # Most recent first


@api.get("/paginated-cursor", response_model=list[UserListOut])
@paginate(UserCursorPagination)
async def list_users_cursor(request):
    """
    List users with cursor-based pagination.

    Cursor pagination is ideal for infinite scroll or real-time feeds
    because it's stable even when new items are added.

    Query params:
        - cursor: Opaque cursor string (optional, omit for first page)
        - page_size: Items per page (default: 20)

    Example: GET /users/paginated-cursor?cursor=eyJ2IjoxMDB9

    Response:
        {
            "items": [...],
            "page_size": 20,
            "has_next": true,
            "has_previous": true,
            "next_cursor": "eyJ2Ijo4MH0=",
            "previous_cursor": null
        }
    """
    return User.objects.all()


# ----------------------------------------------------------------------------
# 5. Pagination with a smaller item type
#    - Fields are extracted based on the model field declarations
# ----------------------------------------------------------------------------


@api.get("/paginated-msgspec", response_model=list[UserMini])
@paginate(SmallPagePagination)
async def list_users_msgspec(request):
    """
    The item type decides which fields go out.

    Only id and username are included (as declared in UserMini).
    """
    return User.objects.all()


# ----------------------------------------------------------------------------
# 6. Pagination without an item type (fallback)
#    - When no response_model is specified, all model fields are dumped
#    - Not recommended for production (may expose sensitive fields)
# ----------------------------------------------------------------------------


@api.get("/paginated-legacy")
@paginate(SmallPagePagination)
async def list_users_legacy(request):
    """
    Pagination without an item type - all model fields are included.

    WARNING: This may expose sensitive fields. Always use an item type
    in production to control which fields are returned.
    """
    return User.objects.only("id", "username")


# ----------------------------------------------------------------------------
# 7. Class-Based View (APIView) with Pagination
# ----------------------------------------------------------------------------


async def _get_user(pk: int) -> User:
    try:
        return await User.objects.aget(pk=pk)
    except User.DoesNotExist as e:
        raise HTTPException(status_code=404, detail="Not found") from e


@api.view("/api-paginated")
class UserPaginatedList(APIView):
    """GET /users/api-paginated lists the users, one page at a time."""

    @paginate(SmallPagePagination)
    async def get(self) -> list[UserListOut]:
        return User.objects.all()


@api.view("/api-paginated/{pk}")
class UserPaginatedDetail(APIView):
    """GET /users/api-paginated/{pk} returns one user."""

    async def get(self, pk: int) -> UserDetailOut:
        return await _get_user(pk)


# ----------------------------------------------------------------------------
# 8. CRUD Function Routes with Pagination
# ----------------------------------------------------------------------------
#
# GET /users/model-paginated lists one page of users (page, page_size).
# POST creates a user. GET, PUT, PATCH and DELETE /users/model-paginated/{pk}
# read, change and delete one user.


@api.get("/model-paginated")
@paginate(SmallPagePagination)
async def list_model_paginated() -> list[UserMiniOut]:
    return User.objects.all()


@api.post("/model-paginated", status_code=201)
async def create_model_paginated(data: UserCreateIn) -> UserDetailOut:
    try:
        return await User.objects.acreate(**data.model_dump())
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="The object conflicts with an existing object") from e


@api.get("/model-paginated/{pk}")
async def retrieve_model_paginated(pk: int) -> UserDetailOut:
    return await _get_user(pk)


async def _update_user(pk: int, data: UserUpdateIn) -> User:
    user = await _get_user(pk)
    for name, value in data.model_dump(exclude_unset=True).items():
        setattr(user, name, value)
    await user.asave()
    return user


@api.put("/model-paginated/{pk}")
async def update_model_paginated(pk: int, data: UserUpdateIn) -> UserDetailOut:
    return await _update_user(pk, data)


@api.patch("/model-paginated/{pk}")
async def partial_update_model_paginated(pk: int, data: UserUpdateIn) -> UserDetailOut:
    return await _update_user(pk, data)


@api.delete("/model-paginated/{pk}", status_code=204)
async def destroy_model_paginated(pk: int) -> None:
    user = await _get_user(pk)
    await user.adelete()


# ============================================================================
# Filter Backend via Depends (class-callable dep reading request.query)
# ============================================================================
#
# Demonstrates that a Depends() target can read request.query directly even
# when the handler itself never references `request`. Static analysis at
# registration walks the dep tree, so Rust still parses the query string.
#
# Try:
#   GET /users/filtered
#   GET /users/filtered?is_active=false
#   GET /users/filtered?is_active=true&username=user1


class QueryFilterBackend:
    """Minimal filter backend: whitelists query params and applies them as
    exact-match `filter(**kwargs)` on the queryset.
    """

    def __init__(self, fields: tuple[str, ...]) -> None:
        self.fields = fields

    def __call__(self, request) -> dict:
        raw = request.query
        filters: dict = {}
        for name in self.fields:
            if name not in raw:
                continue
            value = raw[name]
            lowered = value.lower()
            if lowered == "true":
                value = True
            elif lowered == "false":
                value = False
            filters[name] = value
        return filters


@api.get("/filtered")
async def list_users_filtered(
    filters: Annotated[dict, Depends(QueryFilterBackend(fields=("username", "is_active", "email")))],
) -> list[UserMini]:
    """List users with filters derived from ?username=... &is_active=... &email=..."""
    return User.objects.filter(**filters).only("id", "username")[:20]
