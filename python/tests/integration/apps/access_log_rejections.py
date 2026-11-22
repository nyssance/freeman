"""Access-log app: routes whose requests Rust answers before the handler runs."""

from __future__ import annotations

from freeman import BoltAPI
from freeman.auth import APIKeyAuthentication, IsAuthenticated
from freeman.middleware import rate_limit

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/open")
async def open_route():
    return {"ok": True}


@api.get("/private", auth=[APIKeyAuthentication(api_keys={"secret"}, header="x-api-key")], guards=[IsAuthenticated()])
async def private():
    return {"ok": True}


@api.get("/limited")
@rate_limit(rps=1, burst=1, key="ip")
async def limited():
    return {"ok": True}


@api.get("/items/{item_id}")
async def item(item_id: int):
    return {"item_id": item_id}
