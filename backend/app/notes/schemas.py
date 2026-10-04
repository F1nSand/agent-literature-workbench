from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.notes.content import validate_content


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=500)
    content: dict[str, Any] = Field(default_factory=lambda: {"type": "doc", "content": [{"type": "paragraph"}]})
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("content")
    @classmethod
    def safe_content(cls, value: dict) -> dict:
        return validate_content(value)


class NoteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=500)
    content: dict[str, Any] | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value.strip()

    @field_validator("content")
    @classmethod
    def safe_content(cls, value: dict | None) -> dict | None:
        return validate_content(value) if value is not None else value

    @model_validator(mode="after")
    def has_change(self):
        if not ({"title", "content"} & self.model_fields_set):
            raise ValueError("请至少修改标题或正文")
        if "content" in self.model_fields_set and self.content is None:
            raise ValueError("笔记正文不能为空")
        return self


class CandidateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    content: dict[str, Any]
    source_run_id: UUID | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("content")
    @classmethod
    def safe_content(cls, value: dict) -> dict:
        return validate_content(value)


class NoteAdopt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    version_id: UUID


class NoteVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    note_id: UUID
    version_number: int
    base_revision: int | None
    parent_version_id: UUID | None
    origin: str
    status: str
    content: dict[str, Any]
    plain_text: str
    source_run_id: UUID | None
    adopted_at: datetime | None
    created_at: datetime


class NoteRead(BaseModel):
    id: UUID
    title: str
    revision: int
    current_version_id: UUID
    content: dict[str, Any]
    plain_text: str
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class NoteList(BaseModel):
    items: list[NoteRead]
    next_cursor: str | None = None


class NoteVersionList(BaseModel):
    items: list[NoteVersionRead]


class NotePreview(BaseModel):
    note_id: UUID
    base_revision: int
    candidate: NoteVersionRead

