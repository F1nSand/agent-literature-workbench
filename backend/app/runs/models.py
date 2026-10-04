from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted', 'awaiting_user')", name="ck_runs_status"),
        CheckConstraint("scope_mode IN ('topic', 'selected', 'library')", name="ck_runs_scope_mode"),
        CheckConstraint("attempt_count >= 0 AND tool_calls_used >= 0 AND model_steps_used >= 0", name="ck_runs_counters_nonnegative"),
        UniqueConstraint("conversation_id", "idempotency_key", name="uq_runs_conversation_idempotency"),
        Index("ix_runs_conversation_created", "conversation_id", "created_at"),
        Index("ix_runs_status_updated", "status", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", server_default="queued")
    scope_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    page_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    budget_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tool_calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    model_steps_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("sequence_number > 0", name="ck_messages_sequence_positive"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        UniqueConstraint("run_id", "sequence_number", name="uq_messages_run_sequence"),
        Index("ix_messages_run_created", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        CheckConstraint("page_number > 0", name="ck_evidence_page_positive"),
        Index("ix_evidence_run_message", "run_id", "message_id"),
        Index("ix_evidence_document_version_page", "document_id", "version_id", "page_number"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False)
    version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False)
    content_unit_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("content_units.id", ondelete="SET NULL"), nullable=True)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    is_derived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    quote_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ToolCall(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint("sequence_number > 0", name="ck_tool_calls_sequence_positive"),
        CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_tool_calls_status"),
        UniqueConstraint("run_id", "sequence_number", name="uq_tool_calls_run_sequence"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_tool_calls_run_idempotency"),
        Index("ix_tool_calls_run_status", "run_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        CheckConstraint("sequence_number > 0", name="ck_run_events_sequence_positive"),
        UniqueConstraint("run_id", "sequence_number", name="uq_run_events_run_sequence"),
        Index("ix_run_events_run_created", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UIAction(Base):
    __tablename__ = "run_ui_actions"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="ck_run_ui_actions_status"),
        CheckConstraint("action_type IN ('open_document', 'show_document_choices', 'show_note_candidate')", name="ck_run_ui_actions_type"),
        UniqueConstraint("run_id", "action_id", name="uq_run_ui_actions_identity"),
        Index("ix_run_ui_actions_run_created", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    action_id: Mapped[str] = mapped_column(String(32), nullable=False)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    acknowledgement: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
