"""Add independent notes and immutable note versions.

Revision ID: 20261001_0006
Revises: 20260929_0005
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261001_0006"
down_revision = "20260929_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_notes_revision_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_notes_idempotency"),
    )
    op.create_index("ix_notes_archived_updated", "notes", ["archived_at", "updated_at"])
    op.create_table(
        "note_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("note_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=True),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="candidate", nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("plain_text", sa.Text(), server_default="", nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version_number > 0", name="ck_note_versions_number_positive"),
        sa.CheckConstraint("origin IN ('user', 'generated')", name="ck_note_versions_origin"),
        sa.CheckConstraint("status IN ('current', 'candidate', 'superseded')", name="ck_note_versions_status"),
        sa.CheckConstraint("base_revision IS NULL OR base_revision > 0", name="ck_note_versions_base_revision_positive"),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["note_id", "parent_version_id"], ["note_versions.note_id", "note_versions.id"], name="fk_note_versions_parent_owner"),
        sa.ForeignKeyConstraint(["source_run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("note_id", "version_number", name="uq_note_versions_number"),
        sa.UniqueConstraint("note_id", "id", name="uq_note_versions_owner_id"),
        sa.UniqueConstraint("note_id", "idempotency_key", name="uq_note_versions_idempotency"),
    )
    op.create_index("ix_note_versions_note_created", "note_versions", ["note_id", "created_at"])
    op.create_foreign_key("fk_notes_current_version_owner", "notes", "note_versions",
                          ["id", "current_version_id"], ["note_id", "id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM notes)")).scalar_one():
        raise RuntimeError("cannot downgrade notes schema while note data exists")
    op.drop_constraint("fk_notes_current_version_owner", "notes", type_="foreignkey")
    op.drop_index("ix_note_versions_note_created", table_name="note_versions")
    op.drop_table("note_versions")
    op.drop_index("ix_notes_archived_updated", table_name="notes")
    op.drop_table("notes")
