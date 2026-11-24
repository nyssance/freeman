---
icon: lucide/alert-triangle
---

# Exceptions Reference

All exceptions are importable from `freeman.exceptions`.

```python
from freeman.exceptions import (
    HTTPException,
    RequestValidationError,
    ResponseValidationError,
    BadRequest,
    NotFound,
    # ... etc
)
```

## HTTP Exceptions

### HTTPException

Base class for all HTTP error responses.

<!-- fragment -->
```python
HTTPException(
    status_code: int = 500,
    detail: str | dict | list = "",
    headers: dict[str, str] = None,
    extra: dict | list = None,
)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `status_code` | `int` | HTTP status code |
| `detail` | `str` | Error message (defaults to HTTP phrase) |
| `headers` | `dict` | Headers to include in response. A number value, such as `{"Retry-After": 30}`, becomes a `str`. A value of another type raises `TypeError`. |
| `extra` | `dict \| list` | Additional data in response |

A `str` in the first position is the detail. Thus `NotFound("User not found")` is the same as `NotFound(detail="User not found")`, and `HTTPException("Storage is down")` is a 500.

A status with no standard phrase, such as 499, gets the detail `Client Error` (4xx) or `Server Error` (5xx).

**Response format:**

```json
{
    "detail": "Error message",
    "extra": { }
}
```

## Client Errors (4xx)

| Exception | Status | Description |
|-----------|--------|-------------|
| `BadRequest` | 400 | Invalid request syntax or parameters |
| `Unauthorized` | 401 | Authentication required or failed |
| `Forbidden` | 403 | Insufficient permissions |
| `NotFound` | 404 | Resource not found |
| `MethodNotAllowed` | 405 | HTTP method not supported |
| `NotAcceptable` | 406 | Cannot produce requested format |
| `Conflict` | 409 | Request conflicts with current state |
| `Gone` | 410 | Resource permanently deleted |
| `UnprocessableEntity` | 422 | Semantic validation error |
| `TooManyRequests` | 429 | Rate limit exceeded |

**Usage:**

```python
from freeman.exceptions import NotFound, BadRequest

@api.get("/users/{user_id}")
async def get_user(user_id: int):
    if user_id < 1:
        raise BadRequest(detail="Invalid user ID")

    user = await User.objects.filter(id=user_id).afirst()
    if not user:
        raise NotFound(detail="User not found")

    return {"id": user.id}
```

## Server Errors (5xx)

| Exception | Status | Description |
|-----------|--------|-------------|
| `InternalServerError` | 500 | Unexpected server error |
| `NotImplemented` | 501 | Feature not implemented |
| `BadGateway` | 502 | Invalid upstream response |
| `ServiceUnavailable` | 503 | Server temporarily unavailable |
| `GatewayTimeout` | 504 | Upstream server timeout |

## Validation Errors

### RequestValidationError

Raised when request data fails validation. Returns **422 Unprocessable Entity**.

<!-- fragment -->
```python
RequestValidationError(
    errors: list[dict],
    body: Any = None,
)
```

**Multi-error collection:** a Pydantic body model reports every error of the body, also those of its `@field_validator` and `@model_validator` methods, in one 422. `e.errors()` returns the list of error dicts.

**Response format:**

```json
{
    "detail": [
        {
            "loc": ["body", "email"],
            "msg": "Invalid email",
            "type": "value_error"
        },
        {
            "loc": ["body", "password"],
            "msg": "Password too short",
            "type": "value_error"
        }
    ]
}
```

### ResponseValidationError

Raised when handler return value fails validation. Returns **500 Internal Server Error**. With `DEBUG=False`, the response body does not show the validation errors. Freeman writes them to the server log.

<!-- fragment -->
```python
ResponseValidationError(
    errors: list[dict],
    body: Any = None,
)
```

## FileResponse errors

`FileResponse` raises these errors when it gets the path of the file. `File` raises `ResponseFileNotFound` when it reads the file. Only these errors give a 404 or a 403, and the 404 and 403 responses do not show the path. A `FileNotFoundError` or a `PermissionError` of other code gives a 500. To answer 404 or 403 from handler code, raise `NotFound` or `Forbidden`.

| Exception | Subclass of | Status | When raised |
|-----------|-------------|--------|-------------|
| `ResponseFileNotFound` | `FileNotFoundError` | 404 | The path names no regular file: it does not exist, it is a directory, or it is not valid |
| `ResponseFileNotAllowed` | `PermissionError` | 403 | The path is outside `BOLT_ALLOWED_FILE_PATHS`, also when it names no file or the server cannot read it |

## Validation Error Types

When validation fails, Freeman returns structured errors. Pydantic validates each model, so an error of a body or a parameter model has the type of Pydantic. A scalar parameter gets the same type, see [Errors of scalar parameters](../topics/error-handling.md#errors-of-scalar-parameters).

| Type | Description | Example Message |
|------|-------------|-----------------|
| `missing` | Required field not provided, at any depth | `Field required` |
| `int_parsing`, `string_type`, ... | Type mismatch | `Input should be a valid integer, unable to parse string as an integer` |
| `greater_than_equal`, `string_too_long`, ... | Constraint violation | `Input should be greater than or equal to 1` |
| `json_invalid` | Malformed JSON | `Invalid JSON at line 2, column 5` |
| `value_error` | Custom `@field_validator` or `@model_validator` failure | `Invalid email format` |
| `validation_error` | Another error of a scalar parameter | ``Expected `object`, got `array` `` |

### Error Location (`loc`)

The `loc` field indicates where the error occurred:

| Location | Format | Example |
|----------|--------|---------|
| Request body | `["body", "field"]` | `["body", "email"]` |
| Nested field | `["body", "user", "name"]` | `["body", "address", "city"]` |
| Array item | `["body", 0, "field"]` | `["body", 0, "id"]` |
| Query param | `["query", "param"]` | `["query", "page"]` |
| Path param | `["path", "param"]` | `["path", "user_id"]` |
| Header | `["header", "name"]` | `["header", "authorization"]` |

The `loc` of a `missing` error ends with the name of the missing field, also in a nested model or in an array item. The index of an array item in `loc` is an integer.

### Example Validation Errors

**Missing required field:**

```json
{
    "detail": [
        {
            "loc": ["body", "price"],
            "msg": "Field required",
            "type": "missing"
        }
    ]
}
```

**Missing field in a nested model:**

For `User(name: str, address: Address)` and `Address(city: str, zip: str)`, the body `{"name": "Ada", "address": {"city": "Paris"}}` gives:

```json
{
    "detail": [
        {
            "loc": ["body", "address", "zip"],
            "msg": "Field required",
            "type": "missing"
        }
    ]
}
```

For a list of `Address` items, `{"items": [{"city": "Paris"}]}` gives `"loc": ["body", "items", 0, "zip"]`.

**Type mismatch:**

```json
{
    "detail": [
        {
            "loc": ["body", "age"],
            "msg": "Input should be a valid integer, unable to parse string as an integer",
            "type": "int_parsing"
        }
    ]
}
```

**Invalid JSON:**

```json
{
    "detail": [
        {
            "loc": ["body", 2, 15],
            "msg": "Invalid JSON at line 2, column 15: unexpected character",
            "type": "json_invalid"
        }
    ]
}
```

**Multiple errors:**

```json
{
    "detail": [
        {
            "loc": ["body", "email"],
            "msg": "Input should be a valid string",
            "type": "string_type"
        },
        {
            "loc": ["body", "age"],
            "msg": "Field required",
            "type": "missing"
        }
    ]
}
```

## Adding Custom Headers

```python
raise Unauthorized(
    detail="Token expired",
    headers={"WWW-Authenticate": "Bearer"}
)
```

## Adding Extra Data

```python
raise BadRequest(
    detail="Validation failed",
    extra={
        "field": "email",
        "value": "invalid@",
        "suggestion": "Enter a valid email address"
    }
)
```

**Response:**

```json
{
    "detail": "Validation failed",
    "extra": {
        "field": "email",
        "value": "invalid@",
        "suggestion": "Enter a valid email address"
    }
}
```

## Status Code Summary

| Code | Exception | When Raised |
|------|-----------|-------------|
| 400 | `BadRequest` | Explicit raise only |
| 401 | `Unauthorized` | Authentication failed |
| 403 | `Forbidden` | Guard/permission denied |
| 404 | `NotFound` | Resource not found, Django's `Http404`, `ResponseFileNotFound` from `FileResponse` |
| 405 | `MethodNotAllowed` | Wrong HTTP method |
| 406 | `NotAcceptable` | Cannot produce requested format |
| 409 | `Conflict` | State conflict |
| 410 | `Gone` | Resource deleted |
| 422 | `UnprocessableEntity` | **Validation errors**, missing required params (automatic) |
| 429 | `TooManyRequests` | Rate limit exceeded |
| 500 | `InternalServerError` | Unhandled exceptions, response validation |
| 501 | `NotImplemented` | Feature not implemented |
| 502 | `BadGateway` | Upstream error |
| 503 | `ServiceUnavailable` | Server unavailable |
| 504 | `GatewayTimeout` | Upstream timeout |
