"""A msgspec.Struct is not a model type of a route.

Pydantic alone validates a body, a result, a parameter model and a form
model. A Struct in one of these places is a TypeError at registration.
"""

from typing import Annotated

import msgspec
import pytest
from pydantic import BaseModel

from freeman import BoltAPI, PageNumberPagination, paginate
from freeman.error_handlers import error_schema
from freeman.param_functions import Form, Query
from freeman.testing import TestClient


class ItemStruct(msgspec.Struct):
    id: int


class ItemModel(BaseModel):
    id: int


class StructPagination(PageNumberPagination):
    response_class = ItemStruct


MESSAGE = (
    r"uses the msgspec\.Struct ItemStruct\. Freeman validates models with Pydantic only\. Use a pydantic\.BaseModel"
)


def _body(api):
    @api.post("/items")
    async def create(item: ItemStruct):
        return {}


def _nested_body(api):
    @api.post("/items")
    async def create(items: list[ItemStruct]):
        return {}


def _result(api):
    @api.get("/items")
    async def read() -> ItemStruct:
        return ItemStruct(id=1)


def _nested_result(api):
    @api.get("/items")
    async def read() -> list[ItemStruct] | None:
        return []


def _response_model(api):
    @api.get("/items", response_model=ItemStruct)
    async def read():
        return {}


def _response_model_of_a_status(api):
    @api.get("/items", response_model={200: ItemModel, 404: ItemStruct})
    async def read():
        return {}


def _query_model(api):
    @api.get("/items")
    async def read(params: Annotated[ItemStruct, Query()]):
        return {}


def _form_model(api):
    @api.post("/items")
    async def create(form: Annotated[ItemStruct, Form()]):
        return {}


def _pagination_envelope(api):
    @api.get("/items")
    @paginate(StructPagination)
    async def read(request) -> list[ItemModel]:
        return []


def _error_schema(api):
    @error_schema(ItemStruct)
    def formatter(error, request):
        return {}


@pytest.mark.parametrize(
    "register",
    [
        _body,
        _nested_body,
        _result,
        _nested_result,
        _response_model,
        _response_model_of_a_status,
        _query_model,
        _form_model,
        _pagination_envelope,
        _error_schema,
    ],
)
def test_a_struct_as_a_model_is_a_type_error_at_registration(register):
    with pytest.raises(TypeError, match=MESSAGE):
        register(BoltAPI())


def test_an_unannotated_handler_can_return_a_struct():
    """A route with no response type sends a Struct as msgspec encodes it, and documents no schema."""
    api = BoltAPI()

    @api.get("/items")
    async def read():
        return ItemStruct(id=1)

    api._register_openapi_routes()
    with TestClient(api) as client:
        assert client.get("/items").json() == {"id": 1}
        schemas = client.get("/docs/openapi.json").json().get("components", {}).get("schemas", {})
    assert "ItemStruct" not in schemas
