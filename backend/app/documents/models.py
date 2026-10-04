from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    literal_column,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'processing', 'ready', 'failed')", name="ck_documents_status"),
        CheckConstraint("page_count >= 0", name="ck_documents_page_count"),
        Index("ix_documents_status_created", "status", "created_at"),
        Index("ix_documents_deleted_created", "is_deleted", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    authors: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doi: Mapped[str | None] = mapped_column(String(250), nullable=True)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    reading_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unread", server_default="unread")
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    active_version_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("document_versions.id", name="fk_documents_active_version", use_alter=True),
        nullable=True,
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        CheckConstraint("status IN ('queued', 'processing', 'ready', 'failed')", name="ck_document_versions_status"),
        CheckConstraint("page_count >= 0", name="ck_document_versions_page_count"),
        CheckConstraint("version_number > 0", name="ck_document_versions_number"),
        UniqueConstraint("document_id", "version_number", name="uq_document_versions_number"),
        Index("ix_document_versions_document_created", "document_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", server_default="queued")
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    processing_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["version_id"], ["document_versions.id"], ondelete="CASCADE"),
        CheckConstraint("page_number > 0", name="ck_document_pages_number"),
        Index("ix_document_pages_document", "document_id", "page_number"),
        UniqueConstraint("document_id", "version_id", "page_number", name="uq_document_pages_owner"),
    )

    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    page_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    locator: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    location_label: Mapped[str] = mapped_column(String(150), nullable=False, default="", server_default="")
    text_char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ocr_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    extraction_method: Mapped[str] = mapped_column(String(24), nullable=False, default="pdf_text", server_default="pdf_text")


class ContentUnit(Base):
    __tablename__ = "content_units"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "version_id", "page_number"],
            ["document_pages.document_id", "document_pages.version_id", "document_pages.page_number"],
            ondelete="CASCADE",
        ),
        CheckConstraint("ordinal >= 0", name="ck_content_units_ordinal"),
        CheckConstraint("content_type IN ('text', 'chart', 'table', 'formula')", name="ck_content_units_type"),
        UniqueConstraint("version_id", "page_number", "ordinal", name="uq_content_units_version_page_ordinal"),
        Index("ix_content_units_document_page", "document_id", "page_number"),
        Index("ix_content_units_version_page", "version_id", "page_number"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str] = mapped_column(String(24), nullable=False, default="text", server_default="text")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(2560), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    extra: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")


Index(
    "ix_content_units_embedding_halfvec_hnsw",
    literal_column("embedding::halfvec(2560)").label("embedding"),
    postgresql_using="hnsw",
    postgresql_with={"m": 16, "ef_construction": 64},
    postgresql_ops={"embedding": "halfvec_cosine_ops"},
    _table=ContentUnit.__table__,
)
Index(
    "ix_content_units_search_text_gin",
    func.to_tsvector(literal_column("'simple'"), ContentUnit.search_text),
    postgresql_using="gin",
    _table=ContentUnit.__table__,
)


class IndexGeneration(Base):
    __tablename__ = "index_generations"
    __table_args__ = (
        CheckConstraint("dimensions = 2560", name="ck_index_generations_dimensions"),
        CheckConstraint("status IN ('building', 'active', 'retired', 'failed')", name="ck_index_generations_status"),
        Index("ix_index_generations_status_created", "status", "created_at"),
        Index(
            "uq_index_generations_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmbeddingCache(Base):
    __tablename__ = "embedding_cache"

    cache_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(2560), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ContentUnitEmbedding(Base):
    __tablename__ = "content_unit_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(["content_unit_id"], ["content_units.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["generation_id"], ["index_generations.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["cache_key"], ["embedding_cache.cache_key"], ondelete="RESTRICT"),
        Index("ix_content_unit_embeddings_generation", "generation_id"),
    )

    content_unit_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    generation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(512), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(2560), nullable=False)


Index(
    "ix_content_unit_embeddings_halfvec_hnsw",
    literal_column("embedding::halfvec(2560)").label("embedding"),
    postgresql_using="hnsw",
    postgresql_with={"m": 16, "ef_construction": 64},
    postgresql_ops={"embedding": "halfvec_cosine_ops"},
    _table=ContentUnitEmbedding.__table__,
)


class ReadingState(Base):
    __tablename__ = "reading_states"
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), primary_key=True)
    locator: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ReadingAnnotation(Base):
    __tablename__ = "reading_annotations"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
    locator: Mapped[dict] = mapped_column(JSON, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="yellow")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OCRCache(Base):
    __tablename__ = "ocr_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ProviderCall(Base):
    __tablename__ = "provider_calls"
    __table_args__ = (
        ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        ForeignKeyConstraint(["task_id"], ["background_tasks.id"], ondelete="SET NULL"),
        CheckConstraint("status IN ('succeeded', 'failed', 'unknown')", name="ck_provider_calls_status"),
        Index("ix_provider_calls_document_time", "document_id", "created_at"),
        Index("ix_provider_calls_task", "task_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    task_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    purpose: Mapped[str] = mapped_column(String(24), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_cny: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    rate_input_cny_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    rate_output_cny_per_million: Mapped[float | None] = mapped_column(Float, nullable=True)
    rate_source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    provider_trace_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
