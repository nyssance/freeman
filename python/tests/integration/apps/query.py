"""App under test for the HTTP QUERY method."""

from __future__ import annotations

from pydantic import BaseModel

from freeman import BoltAPI

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


class SearchQuery(BaseModel):
    term: str
    limit: int = 10


@api.query("/search")
async def search(query: SearchQuery) -> dict:
    return {"term": query.term, "limit": query.limit}
