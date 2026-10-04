from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("artifact_type IN ('note', 'comparison_table', 'draft')", name="ck_artifacts_type"),
        CheckConstraint("revision > 0", name="ck_artifacts_revision_positive"),
        ForeignKeyConstraint(
            ["id", "current_version_id"],
            ["artifact_versions.artifact_id", "artifact_versions.id"],
            name="fk_artifacts_current_version_owner",
            use_alter=True,
        ),
        Index("ix_artifacts_topic_updated", "topic_id", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    topic_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("topics.id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="ck_artifact_versions_number_positive"),
        CheckConstraint("origin IN ('user', 'generated')", name="ck_artifact_versions_origin"),
        CheckConstraint("status IN ('current', 'candidate', 'superseded')", name="ck_artifact_versions_status"),
        UniqueConstraint("artifact_id", "version_number", name="uq_artifact_versions_number"),
        UniqueConstraint("artifact_id", "id", name="uq_artifact_versions_owner_id"),
        ForeignKeyConstraint(
            ["artifact_id", "parent_version_id"],
            ["artifact_versions.artifact_id", "artifact_versions.id"],
            name="fk_artifact_versions_parent_owner",
        ),
        Index("ix_artifact_versions_artifact_created", "artifact_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("artifacts.id", ondelete="RESTRICT"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate", server_default="candidate")
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    structured_content: Mapped[dict | list | None] = mapped_column(JSONB, nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_run_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    evidence_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
