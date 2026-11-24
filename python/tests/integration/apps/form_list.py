"""App under test for form ``list[T]`` field integration.

Exercises the FormValue Single/Multi grouping and Python-side scalar-to-list
wrapping for both urlencoded and multipart bodies.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel

from freeman import BoltAPI
from freeman.params import Form

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


class TagsForm(BaseModel):
    name: str
    tags: list[str] = []
    counts: list[int] = []


@api.post("/form-list")
async def handle_form_list(data: Annotated[TagsForm, Form()]):
    return {"name": data.name, "tags": data.tags, "counts": data.counts}
