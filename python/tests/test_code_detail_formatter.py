"""The ``code_detail`` error formatter: one error shape for every layer."""

from typing import Annotated

import pytest
from pydantic import BaseModel, Field

from freeman import BoltAPI
from freeman.auth import IsAuthenticated, JWTAuthentication
from freeman.error_handlers import code_detail
from freeman.exceptions import HTTPException
from freeman.openapi import OpenAPIConfig
from freeman.openapi.schema_generator import SchemaGenerator
from freeman.testing import TestClient


class Order(BaseModel):
    sku: Annotated[str, Field(min_length=1)]
    quantity: Annotated[int, Field(gt=0)]


@pytest.fixture(scope="module")
def api():
    api = BoltAPI(error_formatter=code_detail, openapi_config=OpenAPIConfig(title="Errors", version="1"))

    @api.get("/private", auth=[JWTAuthentication(secret="s" * 32)], guards=[IsAuthenticated()])
    async def private():
        return {}

    @api.get("/items/{item_id}")
    async def item(item_id: int):
        return {"item_id": item_id}

    @api.post("/orders")
    async def order(data: Order):
        if data.sku == "gone":
            raise HTTPException(status_code=409, detail={"code": "out_of_stock", "detail": "No stock left."})
        if data.sku == "plain":
            raise HTTPException(status_code=409, detail="Conflict with the current state.")
        if data.sku == "extra":
            raise HTTPException(status_code=409, detail="Try later.", extra={"retry_after": 5, "code": "ignored"})
        if data.sku == "crash":
            raise RuntimeError("the database password is hunter2")
        return {"sku": data.sku}

    return api


@pytest.fixture(scope="module")
def client(api):
    with TestClient(api, raise_server_exceptions=False) as client:
        yield client


def test_a_guard_that_rust_refuses(client):
    response = client.get("/private")
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/json"
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"code": "not_authenticated", "detail": "Authentication required"}


def test_a_path_with_no_route(client):
    response = client.get("/nowhere")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert set(response.json()) == {"code", "detail"}


def test_a_path_parameter_that_rust_refuses(client):
    response = client.get("/items/abc")
    assert response.status_code == 422
    data = response.json()
    assert data["code"] == "validation"
    assert data["fields"][0]["loc"] == ["path", "item_id"]


def test_a_pydantic_body_gives_every_field_error(client):
    response = client.post("/orders", json={"sku": "", "quantity": 0})
    assert response.status_code == 422
    data = response.json()
    assert data["code"] == "validation"
    assert [field["loc"] for field in data["fields"]] == [["body", "sku"], ["body", "quantity"]]
    assert all(set(field) == {"loc", "msg", "type"} for field in data["fields"])


def test_a_handler_names_its_own_code(client):
    response = client.post("/orders", json={"sku": "gone", "quantity": 1})
    assert response.status_code == 409
    assert response.json() == {"code": "out_of_stock", "detail": "No stock left."}


def test_an_http_exception_with_a_plain_detail_gets_the_code_of_its_status(client):
    response = client.post("/orders", json={"sku": "plain", "quantity": 1})
    assert response.status_code == 409
    assert response.json()["detail"] == "Conflict with the current state."
    assert isinstance(response.json()["code"], str)


def test_extra_adds_members_and_does_not_replace_the_code(client):
    response = client.post("/orders", json={"sku": "extra", "quantity": 1})
    data = response.json()
    assert data["retry_after"] == 5
    assert data["code"] != "ignored"


def test_an_unhandled_error_does_not_leak_its_message(client):
    response = client.post("/orders", json={"sku": "crash", "quantity": 1})
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "hunter2" not in response.text


def test_the_openapi_document_describes_the_error_body(api):
    document = SchemaGenerator(api, api._openapi_config).generate().to_schema()
    schema = document["components"]["schemas"]["CodeDetail"]
    assert set(schema["properties"]) == {"code", "detail", "fields"}
    assert schema["required"] == ["code", "detail"]
