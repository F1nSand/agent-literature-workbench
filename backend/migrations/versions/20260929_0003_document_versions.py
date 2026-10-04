"""Version document content without invalidating the active paper.

Revision ID: 20260929_0003
Revises: 20260929_0002
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260929_0003"
down_revision = "20260929_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("storage_key", sa.String(length=80), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("page_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("processing_config_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('queued', 'processing', 'ready', 'failed')", name="ck_document_versions_status"),
        sa.CheckConstraint("page_count >= 0", name="ck_document_versions_page_count"),
        sa.CheckConstraint("version_number > 0", name="ck_document_versions_number"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version_number", name="uq_document_versions_number"),
        sa.UniqueConstraint("sha256", name="uq_document_versions_sha256"),
        sa.UniqueConstraint("storage_key", name="uq_document_versions_storage_key"),
    )
    op.create_index("ix_document_versions_document_created", "document_versions", ["document_id", "created_at"])
    op.execute(
        "INSERT INTO document_versions "
        "(id, document_id, version_number, filename, storage_key, sha256, status, page_count, error_code, "
        "processing_config_hash, created_at, updated_at) "
        "SELECT id, id, 1, filename, storage_key, sha256, status, page_count, error_code, "
        "'legacy-m2-v1', created_at, updated_at FROM documents"
    )

    op.add_column("documents", sa.Column("active_version_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("documents", sa.Column("is_deleted", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("documents", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE documents SET active_version_id = id WHERE status = 'ready'")
    op.create_foreign_key(
        "fk_documents_active_version", "documents", "document_versions", ["active_version_id"], ["id"]
    )
    op.create_index("ix_documents_deleted_created", "documents", ["is_deleted", "created_at"])

    op.add_column("document_pages", sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute("UPDATE document_pages SET version_id = document_id")
    op.alter_column("document_pages", "version_id", nullable=False)
    op.add_column("content_units", sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute("UPDATE content_units SET version_id = document_id")
    op.alter_column("content_units", "version_id", nullable=False)
    op.drop_constraint("content_units_document_id_page_number_fkey", "content_units", type_="foreignkey")
    op.drop_constraint("uq_content_units_page_ordinal", "content_units", type_="unique")
    op.drop_constraint("document_pages_pkey", "document_pages", type_="primary")
    op.create_primary_key("document_pages_pkey", "document_pages", ["version_id", "page_number"])
    op.create_foreign_key(
        "fk_document_pages_version", "document_pages", "document_versions", ["version_id"], ["id"], ondelete="CASCADE"
    )
    op.create_unique_constraint(
        "uq_document_pages_owner", "document_pages", ["document_id", "version_id", "page_number"]
    )
    op.create_foreign_key(
        "fk_content_units_page_version",
        "content_units",
        "document_pages",
        ["document_id", "version_id", "page_number"],
        ["document_id", "version_id", "page_number"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_content_units_version_page_ordinal", "content_units", ["version_id", "page_number", "ordinal"]
    )
    op.create_index("ix_content_units_version_page", "content_units", ["version_id", "page_number"])


def downgrade() -> None:
    bind = op.get_bind()
    has_multiple_versions = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM document_versions GROUP BY document_id HAVING count(*) > 1)")
    ).scalar_one()
    if has_multiple_versions:
        raise RuntimeError("cannot downgrade document versions after additional versions have been created")

    op.drop_index("ix_content_units_version_page", table_name="content_units")
    op.drop_constraint("uq_content_units_version_page_ordinal", "content_units", type_="unique")
    op.drop_constraint("fk_content_units_page_version", "content_units", type_="foreignkey")
    op.drop_column("content_units", "version_id")
    op.drop_constraint("uq_document_pages_owner", "document_pages", type_="unique")
    op.drop_constraint("fk_document_pages_version", "document_pages", type_="foreignkey")
    op.drop_constraint("document_pages_pkey", "document_pages", type_="primary")
    op.create_primary_key("document_pages_pkey", "document_pages", ["document_id", "page_number"])
    op.drop_column("document_pages", "version_id")
    op.create_foreign_key(
        "content_units_document_id_page_number_fkey",
        "content_units",
        "document_pages",
        ["document_id", "page_number"],
        ["document_id", "page_number"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_content_units_page_ordinal", "content_units", ["document_id", "page_number", "ordinal"]
    )

    op.drop_index("ix_documents_deleted_created", table_name="documents")
    op.drop_constraint("fk_documents_active_version", "documents", type_="foreignkey")
    op.drop_column("documents", "deleted_at")
    op.drop_column("documents", "is_deleted")
    op.drop_column("documents", "active_version_id")
    op.drop_index("ix_document_versions_document_created", table_name="document_versions")
    op.drop_table("document_versions")
