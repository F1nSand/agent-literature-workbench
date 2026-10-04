"""Add index generations, embedding/OCR caches, and lexical search fields.

Revision ID: 20260929_0004
Revises: 20260929_0003
Create Date: 2026-09-29
"""

import hashlib
import json
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision = "20260929_0004"
down_revision = "20260929_0003"
branch_labels = None
depends_on = None


DEFAULT_MODEL = "Qwen/Qwen3-Embedding-4B"
DIMENSIONS = 2560
CHUNKER_VERSION = "page-local-v1"


def _profile_hash(model: str) -> str:
    profile = {
        "provider": "siliconflow",
        "model": model,
        "dimensions": DIMENSIONS,
        "distance": "cosine-v1",
        "chunker": CHUNKER_VERSION,
    }
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode("utf-8")).hexdigest()


def _cache_key_sql() -> str:
    return (
        "'embedding:content:siliconflow:' || :model || ':d2560:cosine-v1:page-local-v1:' "
        "|| content_units.text_sha256"
    )


def upgrade() -> None:
    bind = op.get_bind()
    model_count = bind.execute(
        sa.text("SELECT count(DISTINCT embedding_model) FROM content_units WHERE embedding IS NOT NULL AND embedding_model IS NOT NULL")
    ).scalar_one()
    if model_count > 1:
        raise RuntimeError("existing content uses multiple embedding models; migrate each profile separately")
    model = bind.execute(
        sa.text("SELECT max(embedding_model) FROM content_units WHERE embedding IS NOT NULL AND embedding_model IS NOT NULL")
    ).scalar_one_or_none() or DEFAULT_MODEL
    generation_id = uuid4()
    profile_hash = _profile_hash(model)

    op.create_table(
        "index_generations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("dimensions = 2560", name="ck_index_generations_dimensions"),
        sa.CheckConstraint("status IN ('building', 'active', 'retired', 'failed')", name="ck_index_generations_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_index_generations_status_created", "index_generations", ["status", "created_at"])
    op.create_index(
        "uq_index_generations_one_active",
        "index_generations",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "embedding_cache",
        sa.Column("cache_key", sa.String(length=512), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(DIMENSIONS), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("cache_key"),
    )
    op.create_table(
        "content_unit_embeddings",
        sa.Column("content_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cache_key", sa.String(length=512), nullable=False),
        sa.Column("embedding", Vector(DIMENSIONS), nullable=False),
        sa.ForeignKeyConstraint(["content_unit_id"], ["content_units.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["generation_id"], ["index_generations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cache_key"], ["embedding_cache.cache_key"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("content_unit_id", "generation_id"),
    )
    op.create_index("ix_content_unit_embeddings_generation", "content_unit_embeddings", ["generation_id"])
    op.execute(
        "CREATE INDEX ix_content_unit_embeddings_halfvec_hnsw "
        "ON content_unit_embeddings USING hnsw ((embedding::halfvec(2560)) halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
    op.create_table(
        "ocr_cache",
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("cache_key"),
    )

    op.add_column("content_units", sa.Column("search_text", sa.Text(), server_default="", nullable=False))
    op.execute("UPDATE content_units SET search_text = text")
    op.create_check_constraint(
        "ck_content_units_type", "content_units", "content_type IN ('text', 'chart', 'table', 'formula')"
    )
    op.execute(
        "CREATE INDEX ix_content_units_search_text_gin "
        "ON content_units USING gin (to_tsvector('simple', search_text))"
    )

    op.execute(
        sa.text(
            "INSERT INTO index_generations (id, provider, model, dimensions, config_hash, status, activated_at) "
            "VALUES (:id, 'siliconflow', :model, 2560, :profile_hash, 'active', now())"
        ).bindparams(id=generation_id, model=model, profile_hash=profile_hash)
    )
    key_expression = _cache_key_sql()
    op.execute(
        sa.text(
            "INSERT INTO embedding_cache (cache_key, provider, model, dimensions, config_hash, embedding) "
            f"SELECT DISTINCT ON (content_units.text_sha256) {key_expression}, 'siliconflow', :model, 2560, :profile_hash, content_units.embedding "
            "FROM content_units WHERE content_units.embedding IS NOT NULL "
            "ORDER BY content_units.text_sha256, content_units.id"
        ).bindparams(model=model, profile_hash=profile_hash)
    )
    op.execute(
        sa.text(
            "INSERT INTO content_unit_embeddings (content_unit_id, generation_id, cache_key, embedding) "
            f"SELECT content_units.id, :generation_id, {key_expression}, content_units.embedding "
            "FROM content_units WHERE content_units.embedding IS NOT NULL"
        ).bindparams(model=model, generation_id=generation_id)
    )


def downgrade() -> None:
    op.drop_index("ix_content_units_search_text_gin", table_name="content_units")
    op.drop_constraint("ck_content_units_type", "content_units", type_="check")
    op.drop_column("content_units", "search_text")
    op.drop_table("ocr_cache")
    op.drop_index("ix_content_unit_embeddings_halfvec_hnsw", table_name="content_unit_embeddings")
    op.drop_index("ix_content_unit_embeddings_generation", table_name="content_unit_embeddings")
    op.drop_table("content_unit_embeddings")
    op.drop_table("embedding_cache")
    op.drop_index("uq_index_generations_one_active", table_name="index_generations")
    op.drop_index("ix_index_generations_status_created", table_name="index_generations")
    op.drop_table("index_generations")
