"""The ``blocking`` route option: an explicit choice of the thread of a sync handler."""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable
from typing import ClassVar

import pytest

from freeman import BoltAPI, Router
from freeman._handler_attrs import set_handler_attr
from freeman.testing import TestClient
from freeman.typing import HandlerMetadata
from freeman.views import APIView
from tests.test_models import Article


def _thread_name() -> str:
    return threading.current_thread().name


def _factory(work: Callable[[], str]) -> Callable[..., dict[str, str]]:
    """Build a handler as a layer above the engine does: its source shows no ORM call."""

    def handler(*args: object, **kwargs: object) -> dict[str, str]:
        return {"thread": work()}

    # As in railguns: the signature tells the engine what to bind.
    request = inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD)
    set_handler_attr(handler, "__signature__", inspect.Signature([request]))
    return handler


def _meta(api: BoltAPI, method: str = "GET") -> HandlerMetadata:
    return next(api._handler_meta[handler_id] for m, _path, handler_id, _fn in api._routes if m == method)


def test_a_factory_built_handler_runs_inline_by_default():
    """The analysis finds no blocking call in a closure, so the handler runs inline."""
    api = BoltAPI()
    api.get("/inline")(_factory(_thread_name))

    with TestClient(api) as client:
        thread = client.get("/inline").json()["thread"]

    assert _meta(api)["is_blocking"] is False
    assert not thread.startswith("bolt_default")


def test_blocking_true_runs_a_factory_built_handler_on_the_executor():
    api = BoltAPI()
    api.get("/blocking", blocking=True)(_factory(_thread_name))

    with TestClient(api) as client:
        response = client.get("/blocking")

    assert response.status_code == 200
    assert response.json()["thread"].startswith("bolt_default")
    assert response.json()["thread"] != threading.main_thread().name


class _Cache:
    """Not a model. It has ``objects`` and ``save``, so the source analysis takes it for the ORM."""

    objects: ClassVar[dict[str, int]] = {}

    @classmethod
    def save(cls) -> int:
        return len(cls.objects)


def test_blocking_false_runs_a_handler_inline_that_the_analysis_marks_blocking():
    def count() -> dict[str, object]:
        return {"count": _Cache.save(), "size": len(_Cache.objects), "thread": threading.current_thread().name}

    analysed, explicit = BoltAPI(), BoltAPI()
    analysed.get("/count")(count)
    explicit.get("/count", blocking=False)(count)

    with TestClient(explicit) as client:
        response = client.get("/count")

    assert _meta(analysed)["is_blocking"] is True
    assert _meta(explicit)["is_blocking"] is False
    assert response.json()["count"] == 0
    assert not response.json()["thread"].startswith("bolt_default")


@pytest.mark.django_db(transaction=True)
def test_the_default_keeps_the_analysis():
    api = BoltAPI()

    @api.get("/count")
    def count() -> dict[str, object]:
        return {"count": Article.objects.count(), "thread": threading.current_thread().name}

    with TestClient(api) as client:
        response = client.get("/count")

    assert _meta(api)["is_blocking"] is True
    assert response.json()["thread"].startswith("bolt_default")


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
    router.get("/blocking", blocking=True)(_factory(_thread_name))
    api = BoltAPI()
    api.include_router(router)

    with TestClient(api) as client:
        thread = client.get("/r/blocking").json()["thread"]

    assert thread.startswith("bolt_default")


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
    api.view("/pure", blocking=True)(Pure)

    with TestClient(api) as client:
        assert client.get("/pure").json()["thread"].startswith("bolt_default")


def test_a_view_with_an_async_method_and_blocking_true_raises():
    class Async(APIView):
        async def get(self) -> dict:
            return {}

    with pytest.raises(TypeError, match="must not block"):
        BoltAPI().view("/async", blocking=True)(Async)
