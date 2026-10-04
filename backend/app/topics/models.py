from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (Index("ix_topics_archived_updated", "archived_at", "updated_at"),)

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class TopicDocument(Base):
    __tablename__ = "topic_documents"
    __table_args__ = (
        Index("ix_topic_documents_document", "document_id"),
        Index("ix_topic_documents_topic_created", "topic_id", "created_at"),
    )

    topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_topic_updated", "topic_id", "updated_at"), Index("ix_conversations_archived_updated", "archived_at", "updated_at"))

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    topic_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("topics.id", ondelete="RESTRICT"), nullable=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="新会话", server_default="新会话")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
