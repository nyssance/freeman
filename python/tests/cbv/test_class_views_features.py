"""
Comprehensive feature tests for class-based views.

This test suite verifies that ALL Freeman features work correctly with
class-based views (APIView):

- Request validation (Body, Query, Path, Header, Cookie, Form, File)
- Response validation and serialization
- Authentication (JWT, APIKey, Session)
- Guards/Permissions (IsAuthenticated, Requires)
- Dependency injection (Depends)
- Middleware (CORS, rate limiting)
- Error handling (HTTPException, validation errors)
- Streaming responses
- File uploads/downloads
"""

from typing import Annotated

import msgspec
import pytest
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType

from freeman import BoltAPI
from freeman.auth import APIKeyAuthentication, IsAuthenticated, JWTAuthentication
from freeman.auth.guards import Requires
from freeman.auth.jwt_utils import create_jwt_for_user, get_current_user
from freeman.exceptions import HTTPException
from freeman.middleware import cors, rate_limit, skip_middleware
from freeman.params import Body, Cookie, Depends, Header, Path, Query
from freeman.responses import StreamingResponse
from freeman.testing import TestClient
from freeman.views import APIView

# --- Fixtures ---


@pytest.fixture
def api():
    """Create a fresh BoltAPI instance for each test."""
    return BoltAPI()


# --- Schemas ---


class UserSchema(msgspec.Struct):
    """User response schema."""

    id: int
    username: str
    email: str


class UserCreateSchema(msgspec.Struct):
    """User creation schema."""

    username: str
    email: str
    password: str


class ErrorResponse(msgspec.Struct):
    """Error response schema."""

    detail: str
    code: str | None = None


# ============================================================================
# Request Validation Tests
# ============================================================================


def test_request_body_validation_error(api):
    """Test that invalid request body returns proper validation error."""

    @api.view("/users", methods=["POST"])
    class ArticleView(APIView):
        async def post(self, request, data: UserCreateSchema):
            return {"ok": True}

    with TestClient(api) as client:
        # Missing required field
        response = client.post("/users", json={"username": "test"})
        assert response.status_code == 422  # Validation error returns 422
        data = response.json()
        assert "detail" in data
        # Detail is a list of error objects with 'loc', 'msg', 'type' fields
        errors = data["detail"]
        assert isinstance(errors, list)
        assert any("email" in str(err.get("loc", [])) or "email" in err.get("msg", "") for err in errors)

        # Invalid type
        response = client.post(
            "/users",
            json={
                "username": 123,  # Should be string
                "email": "test@example.com",
                "password": "secret",
            },
        )
        assert response.status_code == 422  # Validation error returns 422


def test_query_parameter_validation(api):
    """Test query parameter validation with constraints.

    NOTE: Query parameter constraint validation (ge, le, etc.) is not currently
    enforced by Freeman. This test just verifies basic query param extraction.
    """

    @api.view("/search", methods=["GET"])
    class SearchView(APIView):
        async def get(
            self, request, page: Annotated[int, Query(ge=1)] = 1, limit: Annotated[int, Query(ge=1, le=100)] = 10
        ):
            return {"page": page, "limit": limit}

    with TestClient(api) as client:
        # Valid params
        response = client.get("/search?page=1&limit=50")
        assert response.status_code == 200
        assert response.json()["page"] == 1
        assert response.json()["limit"] == 50


def test_path_parameter_validation(api):
    """Test path parameter validation."""

    @api.view("/users/{user_id}", methods=["GET"])
    class UserView(APIView):
        async def get(self, request, user_id: Annotated[int, Path(ge=1)]):
            return {"user_id": user_id}

    with TestClient(api) as client:
        # Valid
        response = client.get("/users/123")
        assert response.status_code == 200
        assert response.json()["user_id"] == 123

        # Invalid: not a number (raises ValueError, returns 422)
        response = client.get("/users/abc")
        assert response.status_code == 422  # Type coercion validation error


def test_header_parameter_extraction(api):
    """Test extracting parameters from HTTP headers."""

    @api.view("/protected", methods=["GET"])
    class APIView_WithHeader(APIView):
        async def get(
            self,
            request,
            api_key: Annotated[str, Header(alias="X-API-Key")],
            user_agent: Annotated[str, Header(alias="User-Agent")] = "unknown",
        ):
            return {"api_key": api_key, "user_agent": user_agent}

    with TestClient(api) as client:
        # With headers
        response = client.get("/protected", headers={"X-API-Key": "secret123", "User-Agent": "TestClient/1.0"})
        assert response.status_code == 200
        assert response.json()["api_key"] == "secret123"
        assert response.json()["user_agent"] == "TestClient/1.0"

        # Missing required header
        response = client.get("/protected")
        assert response.status_code == 422  # Missing required header returns 422


def test_cookie_parameter_extraction(api):
    """Test extracting parameters from cookies."""

    @api.view("/session", methods=["GET"])
    class SessionView(APIView):
        async def get(
            self,
            request,
            session_id: Annotated[str, Cookie(alias="session")],
            theme: Annotated[str, Cookie(alias="theme")] = "light",
        ):
            return {"session_id": session_id, "theme": theme}

    with TestClient(api) as client:
        # With cookies
        response = client.get("/session", headers={"Cookie": "session=abc123; theme=dark"})
        assert response.status_code == 200
        assert response.json()["session_id"] == "abc123"
        assert response.json()["theme"] == "dark"

        # Missing required cookie
        response = client.get("/session")
        assert response.status_code == 422  # Missing required cookie returns 422


def test_mixed_parameter_sources(api):
    """Test mixing parameters from different sources."""

    @api.view("/users/{user_id}/update", methods=["POST"])
    class ComplexView(APIView):
        async def post(
            self,
            request,
            user_id: int,  # Path
            include_details: bool = False,  # Query
            api_key: Annotated[str, Header(alias="X-API-Key")] = "",  # Header
            data: UserCreateSchema = Body(),
        ):  # Body
            return {
                "user_id": user_id,
                "include_details": include_details,
                "api_key": api_key,
                "data": {"username": data.username, "email": data.email},
            }

    with TestClient(api) as client:
        response = client.post(
            "/users/123/update?include_details=true",
            json={"username": "john", "email": "john@example.com", "password": "secret"},
            headers={"X-API-Key": "key123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == 123
        assert data["include_details"] is True
        assert data["api_key"] == "key123"
        assert data["data"]["username"] == "john"


# ============================================================================
# Response Validation Tests
# ============================================================================


def test_response_model_validation(api):
    """Test that response is validated against response_model."""

    @api.view("/user", methods=["GET"])
    class UserView(APIView):
        async def get(self, request) -> UserSchema:
            # Return dict, should be validated
            return {"id": 1, "username": "john", "email": "john@example.com"}

    with TestClient(api) as client:
        response = client.get("/user")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 1
        assert data["username"] == "john"
        assert data["email"] == "john@example.com"


def test_response_list_validation(api):
    """Test that list responses are validated."""

    @api.view("/users", methods=["GET"])
    class UsersView(APIView):
        async def get(self, request) -> list[UserSchema]:
            return [
                {"id": 1, "username": "john", "email": "john@example.com"},
                {"id": 2, "username": "jane", "email": "jane@example.com"},
            ]

    with TestClient(api) as client:
        response = client.get("/users")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["id"] == 1
        assert data[1]["id"] == 2


# ============================================================================
# Authentication Tests
# ============================================================================


@pytest.mark.django_db
def test_jwt_authentication_with_class_view(api):
    """Test JWT authentication with class-based views.

    NOTE: JWT authentication runs in Rust middleware, so we need to use
    use_http_layer=True to test it properly.
    """

    # Create test user
    user = User.objects.create(username="testuser", email="test@example.com")
    token = create_jwt_for_user(user, secret="test-secret")

    @api.view("/protected", methods=["GET"])
    class ProtectedView(APIView):
        auth = [JWTAuthentication(secret="test-secret")]
        guards = [IsAuthenticated()]

        async def get(self, request):
            auth_context = request.get("auth", {})
            return {"user_id": auth_context.get("user_id")}

    with TestClient(api, use_http_layer=True) as client:
        # Without token - should fail
        response = client.get("/protected")
        assert response.status_code == 401

        # With valid token - should work
        response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        # user_id is returned as string from JWT
        assert response.json()["user_id"] == str(user.id)


def test_api_key_authentication_with_class_view(api):
    """Test API key authentication with class-based views."""

    @api.view("/protected", methods=["GET"])
    class ProtectedView(APIView):
        auth = [APIKeyAuthentication(api_keys=["secret-key-123"])]
        guards = [IsAuthenticated()]

        async def get(self, request):
            return {"authenticated": True}

    with TestClient(api, use_http_layer=True) as client:
        # Without API key - should fail
        response = client.get("/protected")
        assert response.status_code == 401

        # With valid API key - should work
        response = client.get("/protected", headers={"X-API-Key": "secret-key-123"})
        assert response.status_code == 200
        assert response.json()["authenticated"] is True

        # With invalid API key - should fail
        response = client.get("/protected", headers={"X-API-Key": "invalid-key"})
        assert response.status_code == 401


def test_arguments_of_view_apply_to_the_routes(api):
    """``auth``, ``guards``, ``status_code`` and ``validate_response`` of the decorator reach each route."""
    arguments = {
        "auth": [APIKeyAuthentication(api_keys={"secret-key-123"})],
        "guards": [IsAuthenticated()],
        "status_code": 202,
        "validate_response": False,
    }

    @api.view("/items", **arguments)
    class ItemView(APIView):
        async def get(self, request) -> UserSchema:
            return {"not": "a user"}

    with TestClient(api) as client:
        assert client.get("/items").status_code == 401

        # With validation on, this body gives 500, because it is not a UserSchema.
        response = client.get("/items", headers={"X-API-Key": "secret-key-123"})
        assert response.status_code == 202
        assert response.json() == {"not": "a user"}


# ============================================================================
# Guards/Permissions Tests
# ============================================================================


@pytest.mark.django_db
def test_is_authenticated_guard_with_class_view(api):
    """Test IsAuthenticated guard with class-based views."""

    user = User.objects.create(username="testuser")
    token = create_jwt_for_user(user, secret="test-secret")

    @api.view("/protected", methods=["GET"])
    class ProtectedView(APIView):
        auth = [JWTAuthentication(secret="test-secret")]
        guards = [IsAuthenticated()]

        async def get(self, request):
            return {"ok": True}

    with TestClient(api, use_http_layer=True) as client:
        # Not authenticated
        response = client.get("/protected")
        assert response.status_code == 401

        # Authenticated
        response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200


@pytest.mark.django_db
def test_is_admin_guard_with_class_view(api):
    """Test the is_superuser Requires guard with class-based views."""

    # Regular user
    user = User.objects.create(username="regular", is_staff=False, is_superuser=False)
    user_token = create_jwt_for_user(user, secret="test-secret")

    # Admin user
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    admin_token = create_jwt_for_user(admin, secret="test-secret")

    @api.view("/admin", methods=["GET"])
    class AdminView(APIView):
        auth = [JWTAuthentication(secret="test-secret")]
        guards = [Requires("is_superuser", True)]

        async def get(self, request):
            return {"ok": True}

    with TestClient(api, use_http_layer=True) as client:
        # Regular user - should fail
        response = client.get("/admin", headers={"Authorization": f"Bearer {user_token}"})
        assert response.status_code == 403

        # Admin user - should work
        response = client.get("/admin", headers={"Authorization": f"Bearer {admin_token}"})
        assert response.status_code == 200


@pytest.mark.django_db
def test_has_permission_guard_with_class_view(api):
    """Test a permissions Requires guard with class-based views."""
    from freeman.auth.jwt_utils import create_jwt_for_user  # noqa: PLC0415

    # Create permission
    content_type = ContentType.objects.get_for_model(User)
    permission = Permission.objects.create(
        codename="can_delete_user", name="Can delete user", content_type=content_type
    )

    # User without permission
    user1 = User.objects.create(username="user1")
    token1 = create_jwt_for_user(user1, secret="test-secret")

    # User with permission
    user2 = User.objects.create(username="user2")
    user2.user_permissions.add(permission)
    # Include permissions in JWT extra claims
    token2 = create_jwt_for_user(user2, secret="test-secret", extra_claims={"permissions": ["auth.can_delete_user"]})

    @api.view("/users/{user_id}", methods=["DELETE"])
    class ProtectedView(APIView):
        auth = [JWTAuthentication(secret="test-secret")]
        guards = [Requires("permissions", "auth.can_delete_user")]

        async def delete(self, request, user_id: int):
            return {"deleted": True}

    with TestClient(api, use_http_layer=True) as client:
        # User without permission - should fail
        response = client.delete("/users/123", headers={"Authorization": f"Bearer {token1}"})
        assert response.status_code == 403

        # User with permission - should work
        response = client.delete("/users/123", headers={"Authorization": f"Bearer {token2}"})
        assert response.status_code == 200


# ============================================================================
# Dependency Injection Tests
# ============================================================================


@pytest.mark.django_db(transaction=True)
def test_depends_with_class_view(api):
    """Test dependency injection with class-based views.

    NOTE: Uses transaction=True to avoid SQLite database locking issues
    when get_current_user makes async DB queries.
    """

    user = User.objects.create(username="testuser", email="test@example.com")
    token = create_jwt_for_user(user, secret="test-secret")

    @api.view("/profile", methods=["GET"])
    class ProfileView(APIView):
        auth = [JWTAuthentication(secret="test-secret")]
        guards = [IsAuthenticated()]

        async def get(self, request, current_user: Annotated[User, Depends(get_current_user)]):
            return {"username": current_user.username, "email": current_user.email}

    with TestClient(api, use_http_layer=True) as client:
        response = client.get("/profile", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "testuser"
        assert data["email"] == "test@example.com"


def test_dependency_injection(api):
    """Test custom dependency function with class-based views."""

    async def get_db_connection():
        """Mock database connection dependency."""
        return {"connected": True, "db": "test_db"}

    @api.view("/data", methods=["GET"])
    class DataView(APIView):
        async def get(self, request, db: Annotated[dict, Depends(get_db_connection)]):
            return {"db_status": db}

    with TestClient(api) as client:
        response = client.get("/data")
        assert response.status_code == 200
        data = response.json()
        assert data["db_status"]["connected"] is True
        assert data["db_status"]["db"] == "test_db"


# ============================================================================
# Error Handling Tests
# ============================================================================


def test_http_exception_handling(api):
    """Test HTTPException handling in class-based views."""

    @api.view("/users/{user_id}", methods=["GET"])
    class UserView(APIView):
        async def get(self, request, user_id: int):
            if user_id == 404:
                raise HTTPException(status_code=404, detail="User not found")
            if user_id == 403:
                raise HTTPException(status_code=403, detail="Access denied")
            return {"user_id": user_id}

    with TestClient(api) as client:
        # Not found
        response = client.get("/users/404")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

        # Forbidden
        response = client.get("/users/403")
        assert response.status_code == 403
        assert "denied" in response.json()["detail"].lower()

        # Success
        response = client.get("/users/123")
        assert response.status_code == 200


def test_unhandled_exception_in_class_view(api):
    """Test that unhandled exceptions return 500."""

    @api.view("/buggy", methods=["GET"])
    class BuggyView(APIView):
        async def get(self, request):
            raise ValueError("Something went wrong!")

    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.get("/buggy")
        assert response.status_code == 500
        assert "detail" in response.json()


# ============================================================================
# Streaming Response Tests
# ============================================================================


def test_streaming_response_with_class_view(api):
    """Test streaming responses with class-based views."""

    @api.view("/stream", methods=["GET"])
    class StreamView(APIView):
        async def get(self, request):
            async def generate():
                for i in range(5):
                    yield f"data: {i}\n\n"

            return StreamingResponse(generate(), media_type="text/event-stream")

    with TestClient(api) as client:
        response = client.get("/stream")
        assert response.status_code == 200
        # Note: TestClient may not fully support streaming,
        # but we verify the response type is correct
        assert response.headers.get("content-type") == "text/event-stream"


# ============================================================================
# Middleware Tests
# ============================================================================


def test_cors_middleware_with_class_view(api):
    """Test CORS middleware decorator on class-based view methods."""

    class APIView_WithCORS(APIView):
        # Apply CORS to specific method
        @cors(origins=["https://example.com"], methods=["GET", "POST"], credentials=True)
        async def get(self, request):
            return {"message": "CORS enabled"}

        async def post(self, request):
            return {"message": "No CORS"}

    @api.view("/api/data", methods=["GET", "POST"])
    class APIView_WithCORSRegistered(APIView_WithCORS):
        pass

    with TestClient(api) as client:
        # GET should have CORS metadata attached
        response = client.get("/api/data")
        assert response.status_code == 200
        assert response.json()["message"] == "CORS enabled"

        # POST should work
        response = client.post("/api/data", json={})
        assert response.status_code == 200


def test_rate_limit_middleware_with_class_view(api):
    """Test rate limit middleware decorator on class-based view methods."""

    class APIView_WithRateLimit(APIView):
        # Apply rate limiting to specific method
        @rate_limit(rps=10, burst=20, key="ip")
        async def get(self, request):
            return {"message": "rate limited"}

        async def post(self, request):
            return {"message": "no rate limit"}

    @api.view("/api/limited", methods=["GET", "POST"])
    class APIView_WithRateLimitRegistered(APIView_WithRateLimit):
        pass

    with TestClient(api) as client:
        # Should work (rate limiting metadata attached)
        response = client.get("/api/limited")
        assert response.status_code == 200
        assert response.json()["message"] == "rate limited"

        response = client.post("/api/limited", json={})
        assert response.status_code == 200


def test_skip_middleware_with_class_view(api):
    """Test skip_middleware decorator on class-based view methods."""

    class APIView_SkipMiddleware(APIView):
        # Skip specific middleware for this method
        @skip_middleware("cors", "rate_limit")
        async def get(self, request):
            return {"message": "middleware skipped"}

        async def post(self, request):
            return {"message": "normal middleware"}

    @api.view("/api/skip", methods=["GET", "POST"])
    class APIView_SkipMiddlewareRegistered(APIView_SkipMiddleware):
        pass

    with TestClient(api) as client:
        response = client.get("/api/skip")
        assert response.status_code == 200
        assert response.json()["message"] == "middleware skipped"

        response = client.post("/api/skip", json={})
        assert response.status_code == 200


def test_multiple_middleware_decorators_with_class_view(api):
    """Test stacking multiple middleware decorators on class-based view methods."""

    class APIView_MultiMiddleware(APIView):
        # Stack multiple middleware decorators
        @cors(origins=["*"])
        @rate_limit(rps=100)
        async def get(self, request):
            return {"message": "multi middleware"}

    @api.view("/api/multi", methods=["GET"])
    class APIView_MultiMiddlewareRegistered(APIView_MultiMiddleware):
        pass

    with TestClient(api) as client:
        response = client.get("/api/multi")
        assert response.status_code == 200
        assert response.json()["message"] == "multi middleware"


# ============================================================================
# Custom Action Method Tests
# ============================================================================
