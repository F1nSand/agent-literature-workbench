import hashlib
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.orm import Session

from app.documents import pipeline
from app.documents.models import (
    ContentUnit,
    ContentUnitEmbedding,
    Document,
    DocumentPage,
    DocumentVersion,
    EmbeddingCache,
    IndexGeneration,
)


def _target_generation(source: IndexGeneration) -> IndexGeneration:
    return IndexGeneration(
        id=uuid4(),
        provider=source.provider,
        model=source.model,
        dimensions=source.dimensions,
        config_hash=source.config_hash,
        status="building",
    )


def _seed_ready_unit(session: Session) -> tuple[UUID, str]:
    source = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
    assert source is not None
    document_id, version_id, content_unit_id = uuid4(), uuid4(), uuid4()
    storage_key = uuid4().hex
    digest = hashlib.sha256(str(document_id).encode()).hexdigest()
    text_value = "Controlled content for atomic index generation acceptance."
    cache_key = f"test:{uuid4()}"
    vector = [1.0] + [0.0] * 2559
    document = Document(
        id=document_id,
        title="index rebuild acceptance fixture",
        filename="index-rebuild-fixture.pdf",
        storage_key=storage_key,
        sha256=digest,
        status="ready",
        page_count=1,
        active_version_id=None,
        is_deleted=False,
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(
        id=version_id,
        document_id=document_id,
        version_number=1,
        filename=document.filename,
        storage_key=storage_key,
        sha256=digest,
        status="ready",
        page_count=1,
        processing_config_hash=source.config_hash,
    )
    session.add(version)
    session.flush()
    document.active_version_id = version_id
    session.add(DocumentPage(
        document_id=document_id,
        version_id=version_id,
        page_number=1,
        text=text_value,
        text_char_count=len(text_value),
    ))
    session.flush()
    session.add(EmbeddingCache(
        cache_key=cache_key,
        provider=source.provider,
        model=source.model,
        dimensions=source.dimensions,
        config_hash=source.config_hash,
        embedding=vector,
    ))
    session.add(ContentUnit(
        id=content_unit_id,
        document_id=document_id,
        version_id=version_id,
        page_number=1,
        ordinal=0,
        content_type="text",
        text=text_value,
        text_sha256=hashlib.sha256(text_value.encode()).hexdigest(),
        search_text=text_value,
        token_estimate=8,
        extra={"source": "acceptance_fixture"},
    ))
    session.flush()
    session.add(ContentUnitEmbedding(
        content_unit_id=content_unit_id,
        generation_id=source.id,
        cache_key=cache_key,
        embedding=vector,
    ))
    session.commit()
    return document_id, cache_key


def test_reindex_copies_all_ready_units_then_switches_and_can_restore(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL index rebuild test")

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(pipeline, "get_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "RETIRED_INDEX_GENERATIONS_TO_KEEP", 1000)
    target_id = uuid4()
    source_id = None
    fixture_document_id = None
    fixture_cache_key = None
    try:
        with Session(engine) as session:
            source = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
            assert source is not None
            source_id = source.id
            fixture_document_id, fixture_cache_key = _seed_ready_unit(session)
            expected_count = pipeline._ready_unit_count(session)
            assert expected_count > 0
            target = _target_generation(source)
            target_id = target.id
            session.add(target)
            session.commit()

        result = pipeline.rebuild_index_generation(source_id, target_id)
        assert result["status"] == "active"
        assert result["indexed_content_unit_count"] == expected_count

        with Session(engine) as session:
            assert session.scalar(select(IndexGeneration.id).where(IndexGeneration.status == "active")) == target_id
            assert session.scalar(select(IndexGeneration.status).where(IndexGeneration.id == source_id)) == "retired"
            assert pipeline._ready_unit_count(session, target_id, session.get(IndexGeneration, target_id)) == expected_count

    finally:
        if source_id is not None:
            with Session(engine) as session:
                session.execute(
                    update(IndexGeneration)
                    .where(IndexGeneration.id == target_id, IndexGeneration.status == "active")
                    .values(status="retired")
                )
                session.execute(
                    update(IndexGeneration)
                    .where(IndexGeneration.id == source_id, IndexGeneration.status == "retired")
                    .values(status="active")
                )
                session.execute(delete(IndexGeneration).where(IndexGeneration.id == target_id))
                if fixture_document_id is not None:
                    session.execute(update(Document).where(Document.id == fixture_document_id).values(active_version_id=None))
                    session.execute(delete(Document).where(Document.id == fixture_document_id))
                if fixture_cache_key is not None:
                    session.execute(delete(EmbeddingCache).where(EmbeddingCache.cache_key == fixture_cache_key))
                session.commit()
        engine.dispose()


def test_reindex_failure_keeps_active_generation_when_a_ready_unit_is_unindexed(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL index failure test")

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(pipeline, "get_engine", lambda: engine)
    document_id, version_id, target_id = uuid4(), uuid4(), uuid4()
    source_id = None
    config_hash = hashlib.sha256(b"test-index-config").hexdigest()
    try:
        with Session(engine) as session:
            source = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
            assert source is not None
            source_id = source.id
            document = Document(
                id=document_id,
                title="unindexed fixture",
                filename="unindexed-fixture.pdf",
                storage_key=uuid4().hex,
                sha256=hashlib.sha256(str(document_id).encode()).hexdigest(),
                status="ready",
                page_count=1,
                active_version_id=None,
                is_deleted=False,
            )
            session.add(document)
            session.flush()
            version = DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_number=1,
                filename=document.filename,
                storage_key=document.storage_key,
                sha256=document.sha256,
                status="ready",
                page_count=1,
                processing_config_hash=config_hash,
            )
            session.add(version)
            session.flush()
            document.active_version_id = version_id
            session.add(DocumentPage(
                document_id=document_id,
                version_id=version_id,
                page_number=1,
                text="ready content missing its index association",
                text_char_count=44,
            ))
            session.flush()
            session.add(ContentUnit(
                id=uuid4(),
                document_id=document_id,
                version_id=version_id,
                page_number=1,
                ordinal=0,
                content_type="text",
                text="ready content missing its index association",
                text_sha256=hashlib.sha256(b"unindexed-unit").hexdigest(),
                search_text="ready content missing index association",
                token_estimate=8,
                extra={"source": "test_fixture"},
            ))
            target = _target_generation(source)
            target_id = target.id
            session.add(target)
            session.commit()

        with pytest.raises(pipeline.JobFailure, match="index_rebuild_source_incomplete"):
            pipeline.rebuild_index_generation(source_id, target_id)

        with Session(engine) as session:
            assert session.scalar(select(IndexGeneration.id).where(IndexGeneration.status == "active")) == source_id
            assert session.scalar(select(IndexGeneration.error_code).where(IndexGeneration.id == target_id)) == "index_rebuild_source_incomplete"
            assert session.scalar(select(func.count(ContentUnitEmbedding.content_unit_id)).where(ContentUnitEmbedding.generation_id == target_id)) == 0
    finally:
        with Session(engine) as session:
            session.execute(update(Document).where(Document.id == document_id).values(active_version_id=None))
            session.execute(delete(Document).where(Document.id == document_id))
            session.execute(delete(IndexGeneration).where(IndexGeneration.id == target_id))
            session.commit()
        engine.dispose()
