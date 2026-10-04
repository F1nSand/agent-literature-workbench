from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_type: Literal["preference", "research_conclusion"]
    content: str = Field(min_length=1, max_length=4000)
    source_run_id: UUID | None = None
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=20)

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("内容不能为空")
        return value

    @model_validator(mode="after")
    def source_rules(self):
        if self.memory_type == "preference" and (self.source_run_id is not None or self.evidence_ids):
            raise ValueError("偏好无需来源文献")
        if self.memory_type == "research_conclusion" and (self.source_run_id is None or not self.evidence_ids):
            raise ValueError("研究结论需要已完成研读及其依据")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("依据不能重复")
        return self


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    action: Literal["edit", "confirm", "reject", "archive"]
    content: str | None = Field(default=None, min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("内容不能为空")
        return value

    @model_validator(mode="after")
    def content_matches_action(self):
        if (self.action == "edit") != (self.content is not None):
            raise ValueError("编辑需要提供内容，其他操作不能修改内容")
        return self


class MemoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    topic_id: UUID
    memory_type: str
    status: str
    content: str
    revision: int
    source_run_id: UUID | None
    evidence_snapshot: list[dict]
    confirmed_at: datetime | None
    invalidated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MemoryList(BaseModel):
    items: list[MemoryRead]
