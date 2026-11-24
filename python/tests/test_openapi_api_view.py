"""OpenAPI schema generation for APIView routes and URL path parameters."""

from __future__ import annotations

from freeman import BoltAPI
from freeman.openapi import OpenAPIConfig
from freeman.openapi.schema_generator import SchemaGenerator, _extract_path_param_names
from freeman.views import APIView


def _generate(api: BoltAPI):
    config = OpenAPIConfig(title="t", version="1.0.0")
    return SchemaGenerator(api, config).generate()


def test_extract_path_param_names_basic():
    assert _extract_path_param_names("/articles/{pk}") == ["pk"]
    assert _extract_path_param_names("/a/{x}/b/{y}") == ["x", "y"]
    assert _extract_path_param_names("/no/params") == []


def test_path_param_that_the_method_does_not_bind_appears_in_spec():
    """A method that reads {pk} from self.request.params still documents it."""
    api = BoltAPI()

    @api.view("/articles/{pk}")
    class ArticleView(APIView):
        async def get(self) -> dict:
            return {"pk": self.request.params["pk"]}

        async def delete(self) -> None:
            return None

    detail = _generate(api).paths["/articles/{pk}"]

    for op in (detail.get, detail.delete):
        param_names = {p.name for p in (op.parameters or []) if p.param_in == "path"}
        assert "pk" in param_names, f"pk missing from {op.operation_id}"
    assert detail.get.request_body is None
    assert detail.delete.request_body is None


def test_api_view_operation_takes_its_id_and_its_text_from_the_method():
    """The operation id holds the class and the method. The docstring gives the summary and the description."""
    api = BoltAPI()

    @api.view("/hello")
    class HelloView(APIView):
        async def get(self, request) -> dict:
            """Say hello.

            Answer each caller with one greeting.
            """
            return {"message": "Hello"}

    operation = _generate(api).paths["/hello"].get

    assert operation.operation_id == "get_HelloView.get"
    assert (operation.summary, operation.description) == ("Say hello.", "Answer each caller with one greeting.")


def test_path_param_fallback_does_not_duplicate_typed_param():
    """If a handler explicitly declares a typed path param, the fallback must
    not add a duplicate untyped one."""
    api = BoltAPI()

    @api.get("/items/{item_id}")
    async def get_item(item_id: int) -> dict:
        return {"item_id": item_id}

    schema = _generate(api)
    op = schema.paths["/items/{item_id}"].get
    path_params = [p for p in (op.parameters or []) if p.param_in == "path"]
    assert len(path_params) == 1
    assert path_params[0].name == "item_id"
    # The typed annotation drives the schema, not the string fallback
    assert path_params[0].schema.type == "integer"
