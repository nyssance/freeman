"""Typed lookups in a generated OpenAPI schema, for tests.

Each lookup asserts the part the test relies on, so a missing part fails
with a clear message and the result has a narrow type.
"""

from __future__ import annotations

from freeman.openapi.spec import OpenAPI, OpenAPIResponse, Operation, Parameter, Reference, RequestBody, Schema


def operation(schema: OpenAPI, path: str, method: str = "get") -> Operation:
    """The operation for ``method`` on ``path``."""
    assert schema.paths is not None, "the schema has no paths"
    found = getattr(schema.paths[path], method)
    assert isinstance(found, Operation), f"no {method.upper()} operation on {path}"
    return found


def parameters(op: Operation) -> list[Parameter]:
    """The parameters of ``op``. Each one is inline, not a reference."""
    assert op.parameters is not None, "the operation has no parameters"
    inline = [p for p in op.parameters if isinstance(p, Parameter)]
    assert len(inline) == len(op.parameters), "a parameter is a reference"
    return inline


def request_body(op: Operation) -> RequestBody:
    """The inline request body of ``op``."""
    body = op.request_body
    assert isinstance(body, RequestBody), f"the request body is {body!r}"
    return body


def responses(op: Operation) -> dict[str, OpenAPIResponse]:
    """The responses of ``op`` by status code. Each one is inline, not a reference."""
    assert op.responses is not None, "the operation has no responses"
    inline = {code: r for code, r in op.responses.items() if isinstance(r, OpenAPIResponse)}
    assert len(inline) == len(op.responses), "a response is a reference"
    return inline


def schema(value: Schema | Reference | None) -> Schema:
    """``value`` as an inline schema, not a reference."""
    assert isinstance(value, Schema), f"the schema is {value!r}"
    return value
