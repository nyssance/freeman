"""Route middleware reaches every route, also through class views and mounts.

A class view method carries ``@rate_limit``,
``@middleware`` and ``@skip_middleware`` on the method. The handler that
Bolt registers for them must carry the same marks. A mounted API keeps its
own global middleware at each depth of mounting, and its route cache must not
keep the APIs that mount it alive.
"""

from __future__ import annotations

import gc
import weakref

from freeman import BoltAPI
from freeman.middleware import middleware, rate_limit, skip_middleware
from freeman.testing import TestClient
from freeman.views import APIView


def _header_middleware(header: str) -> type:
    class HeaderMiddleware:
        def __init__(self, get_response):
            self.get_response = get_response

        async def __call__(self, request):
            response = await self.get_response(request)
            response.headers[header] = "1"
            return response

    HeaderMiddleware.__name__ = HeaderMiddleware.__qualname__ = f"{header.replace('-', '')}Middleware"
    return HeaderMiddleware


GlobalMiddleware = _header_middleware("X-Global")


async def route_header(request, call_next):
    response = await call_next(request)
    response.headers["X-Route"] = "1"
    return response


def test_rate_limit_on_a_class_view_method_limits_the_route():
    api = BoltAPI()

    @api.view("/limited", methods=["GET"])
    class Limited(APIView):
        @rate_limit(rps=1, burst=1)
        async def get(self, request):
            return {"ok": True}

    with TestClient(api) as client:
        statuses = [client.get("/limited").status_code for _ in range(3)]

    assert statuses[0] == 200
    assert 429 in statuses[1:], statuses


def test_route_middleware_on_a_class_view_method_runs():
    api = BoltAPI()

    @api.view("/wrapped", methods=["GET"])
    class Wrapped(APIView):
        @middleware(route_header)
        async def get(self, request):
            return {"ok": True}

    with TestClient(api) as client:
        response = client.get("/wrapped")

    assert response.status_code == 200
    assert response.headers.get("X-Route") == "1"


def test_skip_middleware_on_a_class_view_method_skips_the_global_middleware():
    api = BoltAPI(middleware=[GlobalMiddleware])

    @api.view("/skipped", methods=["GET", "POST"])
    class Skipped(APIView):
        @skip_middleware("XGlobalMiddleware")
        async def get(self, request):
            return {"ok": True}

        async def post(self, request):
            return {"ok": True}

    with TestClient(api) as client:
        skipped = client.get("/skipped")
        kept = client.post("/skipped", json={})

    assert skipped.status_code == 200
    assert "X-Global" not in skipped.headers
    assert kept.headers.get("X-Global") == "1"


def test_global_middleware_of_a_mounted_api_runs_through_two_mounts():
    grandchild = BoltAPI(middleware=[GlobalMiddleware])

    @grandchild.get("/x")
    async def handler():
        return {}

    child = BoltAPI()
    child.mount("/g", grandchild)
    parent = BoltAPI()
    parent.mount("/c", child)

    with TestClient(child) as client:
        assert client.get("/g/x").headers.get("X-Global") == "1"
    with TestClient(parent) as client:
        assert client.get("/c/g/x").headers.get("X-Global") == "1"


def test_a_mounted_api_does_not_keep_the_apis_that_mount_it_alive():
    child = BoltAPI(middleware=[GlobalMiddleware])

    @child.get("/x")
    async def handler():
        return {}

    parents = []
    for _ in range(3):
        parent = BoltAPI()
        parent.mount("/c", child)
        with TestClient(parent) as client:
            assert client.get("/c/x").headers.get("X-Global") == "1"
        parents.append(weakref.ref(parent))
    del parent, client
    gc.collect()

    assert [ref() for ref in parents] == [None, None, None]
