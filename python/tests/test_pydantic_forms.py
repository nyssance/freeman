"""Pydantic models as form models, with UploadFile fields.

The cases follow ``test_form_list_extractors.py`` and the form struct cases
of ``test_upload_file.py``. Pydantic converts the text of each field and
reports every error of the model in one 422, with ``loc`` starting with
``body`` as for a form struct.
"""

import inspect
import uuid
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Annotated

import pytest
from django.core.exceptions import ImproperlyConfigured
from openapi_spec_validator import validate
from pydantic import BaseModel, ConfigDict, Field

from freeman import BoltAPI, UploadFile
from freeman._kwargs.extractors import _create_pydantic_form_extractor
from freeman.openapi import OpenAPIConfig
from freeman.openapi.schema_generator import SchemaGenerator
from freeman.params import File, Form
from freeman.testing import TestClient


class ProfileForm(BaseModel):
    name: str
    avatar: UploadFile


class DocumentForm(BaseModel):
    title: str
    documents: list[UploadFile]


class RegistrationForm(BaseModel):
    username: str
    email: str
    age: int
    profile_photo: UploadFile
    resume: UploadFile


class ProfileFormOptional(BaseModel):
    name: str
    avatar: UploadFile | None = None


class ProfileFormAnnotated(BaseModel):
    name: str
    avatar: Annotated[UploadFile, File(max_size=1_000_000, allowed_types=["image/*"])]


class ProfileFormAnnotatedOptional(BaseModel):
    name: str
    avatar: Annotated[UploadFile | None, File(max_size=1_000_000)] = None


class DocumentFormAnnotated(BaseModel):
    title: str
    documents: Annotated[list[UploadFile], File(max_files=5)]


class AdultRegistrationForm(BaseModel):
    username: str
    age: Annotated[int, Field(ge=18)]
    resume: UploadFile


class Shade(Enum):
    RED = "red"


class TypedFileForm(BaseModel):
    doc: UploadFile
    count: int
    ratio: float
    flag: bool
    token: uuid.UUID
    price: Decimal
    day: date
    shade: Shade
    ids: list[int]


class RenamedAvatarForm(BaseModel):
    avatar: Annotated[UploadFile, File(min_size=5)] | None = Field(None, alias="picture")


class LimitedFilesForm(BaseModel):
    avatar: Annotated[UploadFile, File(max_size=10)]
    docs: Annotated[list[UploadFile], File(max_files=2)]


class PdfDocForm(BaseModel):
    doc: Annotated[UploadFile, File(allowed_types=["application/pdf"])]


class StrictForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    tags: list[str] = []
    page: int = 1


def _png(content=b"\x89PNG"):
    return ("photo.png", content, "image/png")


# --- The extractor --------------------------------------------------------------------


def _extractor(model):
    return _create_pydantic_form_extractor(model, inspect.Parameter.empty, frozenset())


class TestScalarWrapping:
    def test_a_scalar_value_of_a_sequence_field_becomes_a_list(self):
        class Tags(BaseModel):
            name: str
            tags: list[str] = []
            counts: list[int] = []

        result = _extractor(Tags)({"name": "alice", "tags": "solo", "counts": "7"})
        assert (result.name, result.tags, result.counts) == ("alice", ["solo"], [7])

    def test_a_list_value_is_not_wrapped_again(self):
        class Tags(BaseModel):
            tags: list[str] = []
            scores: list[int] = []

        result = _extractor(Tags)({"tags": ["a", "b"], "scores": [10, 20]})
        assert (result.tags, result.scores) == (["a", "b"], [10, 20])

    def test_a_missing_field_takes_its_default_and_an_empty_list_stays(self):
        class Tags(BaseModel):
            name: str = "dave"
            tags: list[str] = Field(default_factory=lambda: ["default"])

        assert _extractor(Tags)({}).tags == ["default"]
        assert _extractor(Tags)({"tags": []}).tags == []

    def test_none_is_not_wrapped(self):
        class Tags(BaseModel):
            tags: list[str] | None = None

        assert _extractor(Tags)({"tags": None}).tags is None

    def test_a_repeated_text_field_keeps_its_last_value(self):
        class Name(BaseModel):
            name: str

        assert _extractor(Name)({"name": ["a", "b"]}).name == "b"

    def test_needs_files_map_says_whether_the_model_has_a_file(self):
        class Tags(BaseModel):
            tags: list[str] = []

        class WithFile(BaseModel):
            tags: list[str] = []
            upload: UploadFile | None = None

        assert _extractor(Tags).needs_files_map is False
        extractor = _extractor(WithFile)
        assert extractor.needs_files_map is True

        files = {"upload": {"filename": "note.txt", "content": b"hello", "content_type": "text/plain", "size": 5}}
        result = extractor({"tags": "solo"}, files)
        assert result.tags == ["solo"]
        assert isinstance(result.upload, UploadFile)
        assert result.upload.filename == "note.txt"
        # The request closes the files after the handler.
        assert files["_upload_files"] == [result.upload]


# --- Form models through the server -----------------------------------------------------


class TestFormModel:
    def test_text_fields_convert_and_a_repeated_key_fills_a_sequence(self):
        api = BoltAPI()

        @api.post("/strict")
        async def strict(data: Annotated[StrictForm, Form()]):
            return data.model_dump()

        with TestClient(api) as client:
            response = client.post("/strict", data={"name": "a", "tags": ["x", "y"], "page": "2"})
            assert response.status_code == 200, response.text
            assert response.json() == {"name": "a", "tags": ["x", "y"], "page": 2}
            assert client.post("/strict", data={"name": "a", "tags": "solo"}).json()["tags"] == ["solo"]

    def test_every_error_is_in_one_422_and_extra_forbid_refuses_an_unknown_field(self):
        api = BoltAPI()

        @api.post("/strict")
        async def strict(data: Annotated[StrictForm, Form()], note: Annotated[str, Form()] = ""):
            return data.model_dump()

        with TestClient(api) as client:
            assert client.post("/strict", data={"name": "a", "note": "kept"}).status_code == 200
            response = client.post("/strict", data={"page": "x", "color": "red"})

        assert response.status_code == 422, response.text
        assert response.json()["detail"] == [
            {"loc": ["body", "name"], "msg": "Field required", "type": "missing"},
            {
                "loc": ["body", "page"],
                "msg": "Input should be a valid integer, unable to parse string as an integer",
                "type": "int_parsing",
            },
            {"loc": ["body", "color"], "msg": "Extra inputs are not permitted", "type": "extra_forbidden"},
        ]

    def test_the_alias_is_the_field_name_of_the_form(self):
        class Renamed(BaseModel):
            tags: list[str] = Field(default_factory=list, alias="tag")

        api = BoltAPI()

        @api.post("/form")
        async def form(filters: Annotated[Renamed, Form()], tags: Annotated[str, Form()] = "none"):
            return {"filters": filters.tags, "tags": tags}

        with TestClient(api) as client:
            response = client.post("/form", data={"tag": ["a", "b"], "tags": "solo"})

        assert response.status_code == 200, response.text
        assert response.json() == {"filters": ["a", "b"], "tags": "solo"}

    def test_a_sync_handler_gets_the_model(self):
        api = BoltAPI()

        @api.post("/profile")
        def profile(data: Annotated[ProfileForm, Form()]):
            return {"name": data.name, "content": data.avatar.file.read().decode()}

        with TestClient(api) as client:
            response = client.post("/profile", data={"name": "Jo"}, files={"avatar": ("a.txt", b"hi", "text/plain")})

        assert response.status_code == 200, response.text
        assert response.json() == {"name": "Jo", "content": "hi"}


class TestFormModelWithUploadFile:
    def test_a_single_file(self):
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileForm, Form()]):
            content = await data.avatar.read()
            return {"name": data.name, "filename": data.avatar.filename, "size": len(content)}

        with TestClient(api) as client:
            response = client.post(
                "/profile", data={"name": "John Doe"}, files={"avatar": ("photo.jpg", b"image data here", "image/jpeg")}
            )

        assert response.status_code == 200, response.text
        assert response.json() == {"name": "John Doe", "filename": "photo.jpg", "size": 15}

    def test_a_list_of_files(self):
        api = BoltAPI()

        @api.post("/documents")
        async def upload_documents(data: Annotated[DocumentForm, Form()]):
            return {"title": data.title, "filenames": sorted(d.filename for d in data.documents)}

        with TestClient(api) as client:
            files = [("documents", ("doc1.pdf", b"1", "application/pdf")), ("documents", ("doc2.pdf", b"2", "text/x"))]
            response = client.post("/documents", data={"title": "Docs"}, files=files)
            one = client.post("/documents", data={"title": "One"}, files={"documents": ("a.pdf", b"a", "text/x")})

        assert response.json() == {"title": "Docs", "filenames": ["doc1.pdf", "doc2.pdf"]}
        assert one.json() == {"title": "One", "filenames": ["a.pdf"]}

    def test_text_and_file_fields(self):
        api = BoltAPI()

        @api.post("/register")
        async def register(data: Annotated[RegistrationForm, Form()]):
            return {
                "username": data.username,
                "email": data.email,
                "age": data.age,
                "photo": data.profile_photo.filename,
                "resume": data.resume.filename,
            }

        with TestClient(api) as client:
            response = client.post(
                "/register",
                data={"username": "johndoe", "email": "john@example.com", "age": "30"},
                files=[
                    ("profile_photo", ("photo.jpg", b"image", "image/jpeg")),
                    ("resume", ("resume.pdf", b"pdf", "application/pdf")),
                ],
            )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "username": "johndoe",
            "email": "john@example.com",
            "age": 30,
            "photo": "photo.jpg",
            "resume": "resume.pdf",
        }

    @pytest.mark.parametrize("model", [ProfileFormOptional, ProfileFormAnnotatedOptional])
    def test_an_optional_file(self, model):
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[model, Form()]):
            return {"name": data.name, "avatar": data.avatar.filename if data.avatar else None}

        with TestClient(api) as client:
            assert client.post("/profile", data={"name": "John"}).json() == {"name": "John", "avatar": None}
            response = client.post("/profile", data={"name": "John"}, files={"avatar": _png()})
            assert response.json() == {"name": "John", "avatar": "photo.png"}

    def test_a_missing_required_file_is_a_422(self):
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileForm, Form()]):
            return {"name": data.name}

        with TestClient(api) as client:
            response = client.post("/profile", data={"name": "John"})

        assert response.status_code == 422
        assert response.json()["detail"] == [{"loc": ["body", "avatar"], "msg": "Field required", "type": "missing"}]

    def test_a_valid_file_adds_no_error_next_to_a_bad_field(self):
        api = BoltAPI()

        @api.post("/register")
        async def register(data: Annotated[AdultRegistrationForm, Form()]):
            return {"username": data.username}

        with TestClient(api) as client:
            response = client.post(
                "/register",
                data={"username": "john", "age": "12"},
                files={"resume": ("cv.pdf", b"pdf", "application/pdf")},
            )

        assert response.status_code == 422, response.text
        assert [error["loc"] for error in response.json()["detail"]] == [["body", "age"]]

    def test_each_text_field_next_to_a_file_converts_to_its_type(self):
        api = BoltAPI()

        @api.post("/typed")
        async def typed(data: Annotated[TypedFileForm, Form()]):
            return {name: type(getattr(data, name)).__name__ for name in TypedFileForm.model_fields} | {
                "values": [data.count, data.ratio, data.flag, str(data.price), data.day.isoformat(), data.ids]
            }

        token = uuid.uuid4()
        with TestClient(api) as client:
            response = client.post(
                "/typed",
                data={
                    "count": "3",
                    "ratio": "1.5",
                    "flag": "true",
                    "token": str(token),
                    "price": "1.25",
                    "day": "2024-02-03",
                    "shade": "red",
                    "ids": ["1", "2"],
                },
                files={"doc": ("a.txt", b"x", "text/plain")},
            )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "doc": "UploadFile",
            "count": "int",
            "ratio": "float",
            "flag": "bool",
            "token": "UUID",
            "price": "Decimal",
            "day": "date",
            "shade": "Shade",
            "ids": "list",
            "values": [3, 1.5, True, "1.25", "2024-02-03", [1, 2]],
        }

    def test_an_annotated_file_field_is_an_upload_file(self):
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileFormAnnotated, Form()]):
            return {"is_upload": isinstance(data.avatar, UploadFile), "size": len(await data.avatar.read())}

        @api.post("/documents")
        async def documents(data: Annotated[DocumentFormAnnotated, Form()]):
            return {"count": len(data.documents)}

        with TestClient(api) as client:
            response = client.post("/profile", data={"name": "Jane"}, files={"avatar": _png(b"image bytes")})
            assert response.json() == {"is_upload": True, "size": 11}
            files = [("documents", ("a.pdf", b"a", "text/x")), ("documents", ("b.pdf", b"b", "text/x"))]
            assert client.post("/documents", data={"title": "D"}, files=files).json() == {"count": 2}

    def test_a_file_field_sent_as_text_is_a_422(self):
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileFormAnnotated, Form()]):
            return {"name": data.name}

        with TestClient(api) as client:
            response = client.post("/profile", data={"name": "Jane", "avatar": "not-a-file.pdf"})

        assert response.status_code == 422, response.text
        assert response.json()["detail"] == [
            {"loc": ["body", "avatar"], "msg": "Expected uploaded file, got str", "type": "file_type"}
        ]

    def test_the_files_are_closed_after_the_handler(self):
        api = BoltAPI()
        tracked: list[UploadFile] = []

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileForm, Form()]):
            tracked.append(data.avatar)
            assert not data.avatar._file.closed
            return {"name": data.name}

        with TestClient(api) as client:
            assert client.post("/profile", data={"name": "John"}, files={"avatar": _png()}).status_code == 200

        assert len(tracked) == 1
        assert tracked[0]._file.closed


class TestFileConstraintsByPartName:
    def test_a_file_field_checks_its_constraints(self):
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[ProfileFormAnnotated, Form()]):
            return {"content_type": data.avatar.content_type}

        with TestClient(api) as client:
            response = client.post(
                "/profile", data={"name": "Jane"}, files={"avatar": ("page.html", b"<script></script>", "text/html")}
            )
            assert response.status_code == 422
            assert response.json()["detail"][0]["type"] == "file_invalid_content_type"
            assert response.json()["detail"][0]["loc"] == ["body", "avatar"]
            response = client.post("/profile", data={"name": "Jane"}, files={"avatar": _png()})
            assert response.json() == {"content_type": "image/png"}

    def test_a_file_field_uses_its_alias_as_the_part_name(self):
        api = BoltAPI()

        @api.post("/profile")
        async def update_profile(data: Annotated[RenamedAvatarForm, Form()]):
            return {"size": data.avatar.size if data.avatar else None}

        with TestClient(api) as client:
            response = client.post("/profile", files={"picture": _png(b"xyz")})
            assert response.status_code == 422
            assert response.json()["detail"][0]["type"] == "file_too_small"
            assert response.json()["detail"][0]["loc"] == ["body", "picture"]
            assert client.post("/profile", files={"picture": _png(b"x" * 5)}).json() == {"size": 5}

    def test_file_fields_check_max_size_and_max_files(self):
        api = BoltAPI()

        @api.post("/upload")
        async def upload(data: Annotated[LimitedFilesForm, Form()]):
            return {"sizes": [doc.size for doc in data.docs]}

        small_docs = [("docs", ("a.txt", b"a", "text/plain")), ("docs", ("b.txt", b"b", "text/plain"))]
        with TestClient(api) as client:
            response = client.post("/upload", files=[("avatar", _png(b"x" * 11)), *small_docs])
            assert response.status_code == 422
            assert response.json()["detail"][0]["type"] == "file_too_large"
            assert response.json()["detail"][0]["loc"] == ["body", "avatar"]

            response = client.post(
                "/upload", files=[("avatar", _png(b"x")), *small_docs, ("docs", ("c.txt", b"c", "text/plain"))]
            )
            assert response.status_code == 422
            assert response.json()["detail"][0]["type"] == "file_too_many"
            assert response.json()["detail"][0]["loc"] == ["body", "docs"]

            # The max_size of avatar does not limit docs.
            big_docs = [("docs", ("a.txt", b"a" * 1000, "text/plain")), ("docs", ("b.txt", b"b" * 20, "text/plain"))]
            response = client.post("/upload", files=[("avatar", _png(b"x")), *big_docs])
            assert response.status_code == 200, response.text
            assert response.json() == {"sizes": [1000, 20]}

    def test_two_declarations_of_one_part_with_different_constraints_fail(self):
        api = BoltAPI()

        with pytest.raises(ImproperlyConfigured, match="'doc' has two File\\(\\) declarations"):

            @api.post("/upload")
            async def upload(
                data: Annotated[PdfDocForm, Form()], other: Annotated[UploadFile, File(alias="doc", max_size=1000)]
            ):
                return {}


# --- OpenAPI ---------------------------------------------------------------------------


def _request_body(api, path):
    document = SchemaGenerator(api, OpenAPIConfig(title="Forms", version="1")).generate().to_schema()
    validate(document)
    return document["paths"][path]["post"]["requestBody"]


def test_a_form_model_without_a_file_is_urlencoded_or_multipart():
    class Described(BaseModel):
        name: Annotated[str, Field(description="The name")]
        tags: list[str] = []
        age: int | None = None

    api = BoltAPI()

    @api.post("/form")
    async def form(data: Annotated[Described, Form()]):
        return {}

    body = _request_body(api, "/form")
    assert set(body["content"]) == {"multipart/form-data", "application/x-www-form-urlencoded"}
    schema = body["content"]["application/x-www-form-urlencoded"]["schema"]
    assert schema == {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The name"},
            "tags": {"type": "array", "items": {"type": "string"}, "default": []},
            # A form value cannot be null. The field is not required, and its schema is that of the type.
            "age": {"type": "integer"},
        },
        "required": ["name"],
    }
    assert body["required"] is True


def test_a_form_model_with_a_file_is_multipart_and_a_file_is_binary():
    api = BoltAPI()

    @api.post("/upload")
    async def upload(data: Annotated[LimitedFilesForm, Form()], note: Annotated[str, Form()] = ""):
        return {}

    body = _request_body(api, "/upload")
    assert list(body["content"]) == ["multipart/form-data"]
    schema = body["content"]["multipart/form-data"]["schema"]
    assert schema["properties"]["avatar"] == {"type": "string", "format": "binary"}
    assert schema["properties"]["docs"] == {"type": "array", "items": {"type": "string", "format": "binary"}}
    assert schema["required"] == ["avatar", "docs"]
    assert "note" in schema["properties"]
