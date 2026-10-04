from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ArtifactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    artifact_type: Literal["note", "comparison_table", "draft"] = "note"
    body_text: str = Field(default="", max_length=200000)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("成果名称不能为空")
        return value


class ArtifactFromRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    artifact_type: Literal["note", "comparison_table", "draft"] = "note"
    run_id: UUID

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("成果名称不能为空")
        return value


class ArtifactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body_text: str | None = Field(default=None, max_length=200000)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("成果名称不能为空")
        return value

    @model_validator(mode="after")
    def changes_present(self):
        if "title" not in self.model_fields_set and "body_text" not in self.model_fields_set:
            raise ValueError("请至少修改名称或正文")
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("成果名称不能为空")
        if "body_text" in self.model_fields_set and self.body_text is None:
            raise ValueError("成果正文不能为空")
        return self


class ArtifactCandidateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID


class ArtifactAdopt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    version_id: UUID


class ArtifactVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    artifact_id: UUID
    version_number: int
    parent_version_id: UUID | None
    origin: str
    status: str
    body_text: str
    structured_content: dict | list | None
    content_sha256: str
    source_run_id: UUID | None
    evidence_snapshot: list[dict]
    adopted_at: datetime | None
    created_at: datetime


class ArtifactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    topic_id: UUID
    title: str
    artifact_type: str
    current_version_id: UUID | None
    revision: int
    created_at: datetime
    updated_at: datetime
    current_version: ArtifactVersionRead | None = None


class ArtifactList(BaseModel):
    items: list[ArtifactRead]


class ArtifactVersionList(BaseModel):
    items: list[ArtifactVersionRead]


class ArtifactCompare(BaseModel):
    base: ArtifactVersionRead
    compare: ArtifactVersionRead
    unified_diff: str
