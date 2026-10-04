from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MemoryItem(Base):
    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint("memory_type IN ('preference', 'research_conclusion', 'process_summary')", name="ck_memory_items_type"),
        CheckConstraint("status IN ('active', 'pending', 'confirmed', 'rejected', 'invalidated', 'archived')", name="ck_memory_items_status"),
        CheckConstraint("revision > 0", name="ck_memory_items_revision_positive"),
        Index("ix_memory_items_topic_status_type", "topic_id", "status", "memory_type"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("topics.id", ondelete="RESTRICT"), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    source_run_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    evidence_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
