"""The ``blocking`` route option: a sync handler runs on the executor, or inline with ``blocking=False``."""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable

import pytest

from freeman import BoltAPI, Router
from freeman._handler_attrs import set_handler_attr
from freeman.testing import TestClient
from freeman.typing import HandlerMetadata
from freeman.views import APIView


def _thread_name() -> str:
    return threading.current_thread().name


def _factory(work: Callable[[], str]) -> Callable[..., dict[str, str]]:
    """Build a handler as a layer above the engine does."""

    def handler(*args: object, **kwargs: object) -> dict[str, str]:
        return {"thread": work()}

    # As in railguns: the signature tells the engine what to bind.
    request = inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD)
    set_handler_attr(handler, "__signature__", inspect.Signature([request]))
    return handler


def _meta(api: BoltAPI, method: str = "GET") -> HandlerMetadata:
    return next(api._handler_meta[handler_id] for m, _path, handler_id, _fn in api._routes if m == method)


def test_a_sync_handler_runs_on_the_executor_by_default():
    api = BoltAPI()
    api.get("/default")(_factory(_thread_name))

    with TestClient(api) as client:
        thread = client.get("/default").json()["thread"]

    assert _meta(api)["is_blocking"] is True
    assert thread.startswith("bolt_default")


def test_blocking_true_is_the_default():
    api = BoltAPI()
    api.get("/blocking", blocking=True)(_factory(_thread_name))

    with TestClient(api) as client:
        thread = client.get("/blocking").json()["thread"]

    assert _meta(api)["is_blocking"] is True
    assert thread.startswith("bolt_default")


def test_blocking_false_runs_a_sync_handler_inline():
    api = BoltAPI()
    api.get("/inline", blocking=False)(_factory(_thread_name))

    with TestClient(api) as client:
        thread = client.get("/inline").json()["thread"]

    assert _meta(api)["is_blocking"] is False
    assert not thread.startswith("bolt_default")


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "head", "options", "query"])
def test_each_method_decorator_takes_blocking(method):
    api = BoltAPI()
    getattr(api, method)("/x", blocking=True)(_factory(_thread_name))

    assert _meta(api, method.upper())["is_blocking"] is True


def test_blocking_true_on_an_async_handler_raises_at_registration():
    api = BoltAPI()

    async def handler() -> dict:
        return {}

    with pytest.raises(TypeError, match="An async handler must not block"):
        api.get("/async", blocking=True)(handler)


def test_blocking_false_on_an_async_handler_is_allowed():
    api = BoltAPI()

    @api.get("/async", blocking=False)
    async def handler() -> dict:
        return {}

    assert _meta(api)["is_blocking"] is False


def test_a_router_route_takes_blocking():
    router = Router(prefix="/r")
    router.get("/inline", blocking=False)(_factory(_thread_name))
    api = BoltAPI()
    api.include_router(router)

    with TestClient(api) as client:
        thread = client.get("/r/inline").json()["thread"]

    assert not thread.startswith("bolt_default")


def test_a_router_route_with_an_async_handler_and_blocking_true_raises():
    router = Router()

    @router.get("/async", blocking=True)
    async def handler() -> dict:
        return {}

    with pytest.raises(TypeError, match="must not block"):
        BoltAPI().include_router(router)


def test_a_view_takes_blocking():
    class Pure(APIView):
        def get(self) -> dict:
            return {"thread": _thread_name()}

    api = BoltAPI()
    api.view("/pure", blocking=False)(Pure)

    with TestClient(api) as client:
        assert not client.get("/pure").json()["thread"].startswith("bolt_default")


def test_a_view_with_an_async_method_and_blocking_true_raises():
    class Async(APIView):
        async def get(self) -> dict:
            return {}

    with pytest.raises(TypeError, match="must not block"):
        BoltAPI().view("/async", blocking=True)(Async)
