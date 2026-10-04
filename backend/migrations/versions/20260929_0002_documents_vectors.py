"""Add version-scoped document pages, content units, and cloud usage records.

Revision ID: 20260929_0002
Revises: 20260929_0001
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision = "20260929_0002"
down_revision = "20260929_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("storage_key", sa.String(length=80), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("page_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('queued', 'processing', 'ready', 'failed')", name="ck_documents_status"),
        sa.CheckConstraint("page_count >= 0", name="ck_documents_page_count"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256", name="uq_documents_sha256"),
        sa.UniqueConstraint("storage_key", name="uq_documents_storage_key"),
    )
    op.create_index("ix_documents_status_created", "documents", ["status", "created_at"])

    op.create_table(
        "document_pages",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_char_count", sa.Integer(), nullable=False),
        sa.Column("ocr_used", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("extraction_method", sa.String(length=24), server_default="pdf_text", nullable=False),
        sa.CheckConstraint("page_number > 0", name="ck_document_pages_number"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("document_id", "page_number"),
    )
    op.create_index("ix_document_pages_document", "document_pages", ["document_id", "page_number"])

    op.create_table(
        "content_units",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=24), server_default="text", nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(2560), nullable=True),
        sa.Column("embedding_model", sa.String(length=160), nullable=True),
        sa.Column("token_estimate", sa.Integer(), server_default="0", nullable=False),
        sa.Column("extra", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_content_units_ordinal"),
        sa.ForeignKeyConstraint(
            ["document_id", "page_number"],
            ["document_pages.document_id", "document_pages.page_number"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "page_number", "ordinal", name="uq_content_units_page_ordinal"),
    )
    op.create_index("ix_content_units_document_page", "content_units", ["document_id", "page_number"])
    op.execute(
        "CREATE INDEX ix_content_units_embedding_halfvec_hnsw "
        "ON content_units USING hnsw ((embedding::halfvec(2560)) halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "provider_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_cny", sa.Numeric(12, 6), nullable=True),
        sa.Column("rate_input_cny_per_million", sa.Float(), nullable=True),
        sa.Column("rate_output_cny_per_million", sa.Float(), nullable=True),
        sa.Column("rate_source", sa.String(length=500), nullable=True),
        sa.Column("provider_trace_id", sa.String(length=120), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('succeeded', 'failed', 'unknown')", name="ck_provider_calls_status"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["background_tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provider_calls_document_time", "provider_calls", ["document_id", "created_at"])
    op.create_index("ix_provider_calls_task", "provider_calls", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_provider_calls_task", table_name="provider_calls")
    op.drop_index("ix_provider_calls_document_time", table_name="provider_calls")
    op.drop_table("provider_calls")
    op.drop_index("ix_content_units_embedding_halfvec_hnsw", table_name="content_units")
    op.drop_index("ix_content_units_document_page", table_name="content_units")
    op.drop_table("content_units")
    op.drop_index("ix_document_pages_document", table_name="document_pages")
    op.drop_table("document_pages")
    op.drop_index("ix_documents_status_created", table_name="documents")
    op.drop_table("documents")
