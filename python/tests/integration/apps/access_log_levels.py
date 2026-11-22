"""Access-log level app: one route per status class."""

from __future__ import annotations

from freeman import BoltAPI
from freeman.exceptions import HTTPException

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.get("/ok")
async def ok():
    return {"ok": True}


@api.get("/gone")
async def gone():
    raise HTTPException(404, detail="gone")


@api.get("/down")
async def down():
    raise HTTPException(503, detail="down")


@api.get("/teapot", response_model={200: dict, 418: dict})
async def teapot():
    return 418, {"detail": "teapot"}
