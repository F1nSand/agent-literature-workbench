from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.documents.schemas import DocumentSummary


class TopicCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    goal: str = Field(default="", max_length=10000)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("专题名称不能为空")
        return value


class TopicUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    goal: str | None = Field(default=None, max_length=10000)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("专题名称不能为空")
        return value

    @model_validator(mode="after")
    def require_at_least_one_change(self):
        if not self.model_fields_set:
            raise ValueError("请至少提供一个要修改的字段")
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("专题名称不能为空")
        if "goal" in self.model_fields_set and self.goal is None:
            raise ValueError("研究目标不能为空")
        return self


class TopicSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    goal: str
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
    document_count: int
    conversation_count: int


class TopicList(BaseModel):
    items: list[TopicSummary]


class TopicDocumentItem(BaseModel):
    linked_at: datetime
    document: DocumentSummary


class TopicDocumentList(BaseModel):
    items: list[TopicDocumentItem]


class ConversationCreate(BaseModel):
    title: str = Field(default="新会话", min_length=1, max_length=500)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("会话名称不能为空")
        return value


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    topic_id: UUID | None
    title: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


class ConversationList(BaseModel):
    items: list[ConversationRead]


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=500)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("会话名称不能为空")
        return value


class AssistantScopeRequest(BaseModel):
    mode: Literal["library", "selected"]
    document_ids: list[UUID] | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.mode == "selected":
            if not self.document_ids or len(set(self.document_ids)) != len(self.document_ids):
                raise ValueError("指定文献范围不能为空或重复")
        elif self.document_ids is not None:
            raise ValueError("全库范围不能附加文献列表")
        return self


class PageContext(BaseModel):
    page_type: Literal["home", "library", "reader", "note", "assistant", "settings"]
    document_id: UUID | None = None
    version_id: UUID | None = None
    locator: dict | None = None
    selection: str | None = Field(default=None, max_length=4000)
    note_id: UUID | None = None
    note_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_bounded_context(self):
        if (self.document_id is None) != (self.version_id is None):
            raise ValueError("文献和版本必须同时提供")
        if self.page_type == "reader" and self.document_id is None:
            raise ValueError("阅读器上下文需要文献和版本")
        if (self.note_id is None) != (self.note_revision is None):
            raise ValueError("笔记和版本号必须同时提供")
        if self.page_type == "note" and self.note_id is None:
            raise ValueError("笔记上下文需要笔记及版本号")
        if self.locator is not None and len(str(self.locator)) > 2000:
            raise ValueError("文献定位信息过长")
        if self.selection is not None:
            self.selection = self.selection[:4000]
        return self


class RunCreate(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    scope_mode: Literal["topic", "selected", "library"] = "topic"
    document_ids: list[UUID] | None = Field(default=None, max_length=500)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    scope: AssistantScopeRequest | None = None
    page_context: PageContext | None = None

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("问题不能为空")
        return value

    @model_validator(mode="after")
    def scope_must_be_explicit(self):
        if self.scope is not None:
            if self.document_ids is not None:
                raise ValueError("新范围不能与旧版文献范围字段同时提供")
            return self
        if self.scope_mode == "selected":
            if not self.document_ids:
                raise ValueError("指定文献范围不能为空")
            if len(set(self.document_ids)) != len(self.document_ids):
                raise ValueError("指定文献不能重复")
        elif self.document_ids is not None:
            raise ValueError("此范围模式不能附加指定文献")
        return self


class UIActionAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "failed"]
    error: str | None = Field(default=None, max_length=200)
    selected_document_id: UUID | None = None
    selected_version_id: UUID | None = None

    @model_validator(mode="after")
    def selected_document_pair(self):
        if (self.selected_document_id is None) != (self.selected_version_id is None):
            raise ValueError("选择的文献及版本必须同时提供")
        if self.status == "failed" and self.selected_document_id is not None:
            raise ValueError("失败确认不能包含已选文献")
        return self


class UIActionAckRead(BaseModel):
    action_id: str
    status: Literal["succeeded", "failed"]
    acknowledged: bool


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    status: str
    scope_mode: str
    scope_snapshot: dict
    scope_hash: str
    page_context: dict | None = None
    budget_snapshot: dict
    attempt_count: int
    tool_calls_used: int
    model_steps_used: int
    input_tokens: int | None
    output_tokens: int | None
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    sequence_number: int
    role: str
    content: str
    metadata: dict = Field(validation_alias="metadata_json")
    created_at: datetime


class ConversationMessages(BaseModel):
    conversation_id: UUID
    messages: list[RunMessageRead]


class RunEvidenceRead(BaseModel):
    id: UUID
    document_id: UUID
    version_id: UUID
    page_number: int
    locator: dict | None = None
    location_label: str | None = None
    content_unit_id: UUID | None
    content_type: str
    source: str
    is_derived: bool
    quote_eligible: bool
    content_sha256: str
    excerpt: str | None
    is_validated: bool
    is_invalidated: bool


class RunEventRead(BaseModel):
    sequence_number: int
    event_type: str
    payload: dict
    created_at: datetime


class UIActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action_id: str
    action_type: str
    payload: dict
    status: Literal["pending", "succeeded", "failed"]
    error: str | None
    acknowledged_at: datetime | None
    created_at: datetime


class ConversationRunSummary(BaseModel):
    id: UUID
    status: str
    scope_mode: str
    created_at: datetime
    question: str
    answer: str | None = None


class ConversationRunList(BaseModel):
    items: list[ConversationRunSummary]


class RunDetail(RunRead):
    messages: list[RunMessageRead]
    evidence: list[RunEvidenceRead]
    events: list[RunEventRead]
    ui_actions: list[UIActionRead] = Field(default_factory=list)
