from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_notes_revision_positive"),
        ForeignKeyConstraint(["id", "current_version_id"], ["note_versions.note_id", "note_versions.id"], name="fk_notes_current_version_owner", use_alter=True),
        UniqueConstraint("idempotency_key", name="uq_notes_idempotency"),
        UniqueConstraint("legacy_artifact_id", name="uq_notes_legacy_artifact"),
        Index("ix_notes_archived_updated", "archived_at", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    current_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    legacy_artifact_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("artifacts.id", name="fk_notes_legacy_artifact", ondelete="RESTRICT"), nullable=True)
    legacy_artifact_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class NoteVersion(Base):
    __tablename__ = "note_versions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="ck_note_versions_number_positive"),
        CheckConstraint("origin IN ('user', 'generated')", name="ck_note_versions_origin"),
        CheckConstraint("status IN ('current', 'candidate', 'superseded')", name="ck_note_versions_status"),
        CheckConstraint("base_revision IS NULL OR base_revision > 0", name="ck_note_versions_base_revision_positive"),
        UniqueConstraint("note_id", "version_number", name="uq_note_versions_number"),
        UniqueConstraint("note_id", "id", name="uq_note_versions_owner_id"),
        UniqueConstraint("note_id", "idempotency_key", name="uq_note_versions_idempotency"),
        UniqueConstraint("legacy_artifact_version_id", name="uq_note_versions_legacy_artifact_version"),
        ForeignKeyConstraint(["note_id", "parent_version_id"], ["note_versions.note_id", "note_versions.id"], name="fk_note_versions_parent_owner"),
        Index("ix_note_versions_note_created", "note_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    note_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("notes.id", ondelete="RESTRICT"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    base_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate", server_default="candidate")
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    plain_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    source_run_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    legacy_artifact_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("artifact_versions.id", name="fk_note_versions_legacy_artifact_version", ondelete="RESTRICT"), nullable=True)
    legacy_content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    legacy_evidence_snapshot: Mapped[list | dict | None] = mapped_column(JSONB, nullable=True)
    legacy_structured_content: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
