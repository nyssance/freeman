"""App under test for the connection after a request whose body Bolt does not read.

Rust answers a 401 or a 429 before it reads the body. ``/limited`` lets one
request through each second, so a second request at once gets the 429.
"""

from __future__ import annotations

import msgspec

from freeman import BoltAPI
from freeman.auth import IsAuthenticated, JWTAuthentication
from freeman.middleware import rate_limit

api = BoltAPI()

SECRET = "unread-body-server-secret-32-bytes!"


class Blob(msgspec.Struct):
    data: str


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.post("/protected", auth=[JWTAuthentication(secret=SECRET)], guards=[IsAuthenticated()])
async def protected(blob: Blob) -> dict:
    return {"len": len(blob.data)}


@api.post("/limited")
@rate_limit(rps=1, burst=1)
async def limited(blob: Blob) -> dict:
    return {"len": len(blob.data)}


@api.post("/no-body")
async def no_body() -> dict:
    return {"ok": True}
