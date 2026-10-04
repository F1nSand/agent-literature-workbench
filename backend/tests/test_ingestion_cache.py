import hashlib
import os
from types import SimpleNamespace
from uuid import uuid4

import fitz
import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session

from app.documents import pipeline
from app.jobs.store import claim_next_task
from app.documents.models import (
    ContentUnit,
    ContentUnitEmbedding,
    Document,
    DocumentVersion,
    EmbeddingCache,
    IndexGeneration,
)
from app.providers.siliconflow import ProviderResult


TEXT_SUFFIX = " A repeated paragraph provides enough native PDF text for deterministic cache testing across two papers."


class FakeEmbeddingClient:
    def __init__(self) -> None:
        self.embed_calls = 0
        self.inputs: list[list[str]] = []

    def embed(self, inputs: list[str], model: str | None = None, dimensions: int | None = None):
        self.embed_calls += 1
        self.inputs.append(inputs)
        vector = [1.0] + [0.0] * 2559
        return [vector for _ in inputs], ProviderResult({}, 10, 0, 10, 1)


def _pdf(path, title: str, text: str) -> str:
    document = fitz.open()
    for _ in range(2):
        page = document.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    document.set_metadata({"title": title})
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_ingestion_deduplicates_embedding_requests_and_writes_generation_links(monkeypatch, tmp_path) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL ingestion cache test")

    engine = create_engine(database_url, pool_pre_ping=True)
    text_value = f"{uuid4()}{TEXT_SUFFIX}"
    document_root = tmp_path / "documents"
    document_root.mkdir()
    monkeypatch.setattr(pipeline, "get_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path), max_pdf_pages=500))
    fake_client = FakeEmbeddingClient()
    monkeypatch.setattr(pipeline, "SiliconFlowClient", lambda document_id, task_id: fake_client)

    document_ids = [uuid4(), uuid4()]
    version_ids = [uuid4(), uuid4()]
    task_ids = [uuid4(), uuid4()]
    storage_keys = [uuid4().hex, uuid4().hex]
    try:
        with Session(engine) as session:
            generation = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
            assert generation is not None
            for index, document_id in enumerate(document_ids):
                path = document_root / f"{storage_keys[index]}.pdf"
                digest = _pdf(path, f"cache test {index}", text_value)
                document = Document(
                    id=document_id,
                    title=f"cache test {index}",
                    filename=f"cache-test-{index}.pdf",
                    storage_key=storage_keys[index],
                    sha256=digest,
                    status="queued",
                    page_count=2,
                    active_version_id=None,
                    is_deleted=False,
                )
                version = DocumentVersion(
                    id=version_ids[index],
                    document_id=document_id,
                    version_number=1,
                    filename=f"cache-test-{index}.pdf",
                    storage_key=storage_keys[index],
                    sha256=digest,
                    status="queued",
                    page_count=2,
                    processing_config_hash="0" * 64,
                )
                session.add(document)
                session.flush()
                session.add(version)
            session.commit()

        results = [
            pipeline.process_document_ingestion(document_id, task_id, version_id)
            for document_id, task_id, version_id in zip(document_ids, task_ids, version_ids, strict=True)
        ]

        assert [result["status"] for result in results] == ["ready", "ready"]
        assert [result["content_unit_count"] for result in results] == [2, 2]
        assert results[0]["embedding_cache_hits"] == 1
        assert results[1]["embedding_cache_hits"] == 2
        assert fake_client.embed_calls == 1
        assert len(fake_client.inputs) == 1
        assert len(fake_client.inputs[0]) == 1
        with Session(engine) as session:
            for document_id, version_id in zip(document_ids, version_ids, strict=True):
                document = session.get(Document, document_id)
                assert document is not None and document.status == "ready"
                assert document.active_version_id == version_id
                unit_ids = session.scalars(select(ContentUnit.id).where(ContentUnit.version_id == version_id)).all()
                assert len(unit_ids) == 2
                assert session.scalar(
                    select(ContentUnitEmbedding.generation_id)
                    .where(ContentUnitEmbedding.content_unit_id == unit_ids[0])
                ) is not None
    finally:
        cache_keys = [
            pipeline._embedding_cache_key(value, "content", "Qwen/Qwen3-Embedding-4B", hashlib.sha256(value.encode()).hexdigest())
            for batch in fake_client.inputs
            for value in batch
        ]
        with Session(engine) as session:
            session.execute(update(Document).where(Document.id.in_(document_ids)).values(active_version_id=None))
            session.execute(delete(Document).where(Document.id.in_(document_ids)))
            if cache_keys:
                session.execute(delete(EmbeddingCache).where(EmbeddingCache.cache_key.in_(cache_keys)))
            session.commit()
        engine.dispose()


def test_lost_ingestion_lease_cannot_commit_pages_or_content_units(monkeypatch, tmp_path) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL ingestion fencing test")

    from datetime import timedelta
    from sqlalchemy import func
    from app.documents.models import DocumentPage
    from app.jobs.models import BackgroundTask
    from app.documents.pipeline import ParsedContentUnit

    engine = create_engine(database_url, pool_pre_ping=True)
    document_id, version_id, task_id = uuid4(), uuid4(), uuid4()
    storage_key = uuid4().hex
    document_root = tmp_path / "documents"
    document_root.mkdir()
    pdf_path = document_root / f"{storage_key}.pdf"
    _pdf(pdf_path, "fencing test", f"{uuid4()}{TEXT_SUFFIX}")
    monkeypatch.setattr(pipeline, "get_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path), max_pdf_pages=500))
    monkeypatch.setattr(pipeline, "SiliconFlowClient", lambda document_id, task_id: FakeEmbeddingClient())
    recovered_leases = []
    lease_expired = False

    with Session(engine) as session:
        document = Document(
            id=document_id, title="fencing test", filename="fencing.pdf", storage_key=storage_key,
            sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(), status="queued", page_count=2,
            active_version_id=None, is_deleted=False,
        )
        session.add(document)
        session.flush()
        version = DocumentVersion(
            id=version_id, document_id=document_id, version_number=1, filename="fencing.pdf",
            storage_key=storage_key, sha256=document.sha256, status="queued", page_count=2,
            processing_config_hash="1" * 64,
        )
        session.add_all([version, BackgroundTask(
            id=task_id, task_type="documents.ingest", status="queued",
            payload={"document_id": str(document_id), "version_id": str(version_id)}, max_attempts=2,
        )])
        session.commit()

    try:
        with Session(engine) as session:
            stale_lease = claim_next_task(session, "worker-a")
        assert stale_lease is not None and stale_lease.id == task_id

        def expire_and_reclaim(*args, **kwargs):
            nonlocal lease_expired
            if lease_expired:
                return [ParsedContentUnit(
                    content_type="text", text="synthetic page content", source="pdf_text",
                    is_derived=False, quote_eligible=True,
                )]
            lease_expired = True
            with Session(engine) as session:
                session.execute(
                    update(BackgroundTask).where(BackgroundTask.id == task_id)
                    .values(lease_expires_at=func.now() - timedelta(seconds=1))
                )
                session.commit()
            with Session(engine) as session:
                recovered_leases.append(claim_next_task(session, "worker-b"))
            return [ParsedContentUnit(
                content_type="text", text="synthetic page content", source="pdf_text",
                is_derived=False, quote_eligible=True,
            )]

        monkeypatch.setattr(pipeline, "parse_page_content", expire_and_reclaim)
        with pytest.raises(pipeline.JobFailure, match="worker_lease_lost"):
            pipeline.process_document_ingestion(document_id, task_id, version_id, stale_lease)

        assert recovered_leases[0] is not None and recovered_leases[0].fencing_token == 2
        with Session(engine) as session:
            assert session.scalar(select(DocumentPage).where(DocumentPage.version_id == version_id)) is None
            assert session.scalar(select(ContentUnit).where(ContentUnit.version_id == version_id)) is None
            document = session.get(Document, document_id)
            version = session.get(DocumentVersion, version_id)
            assert document is not None and document.status == "processing"
            assert version is not None and version.status == "processing"
            assert session.get(BackgroundTask, task_id).lease_owner == "worker-b"
    finally:
        with Session(engine) as session:
            session.execute(BackgroundTask.__table__.delete().where(BackgroundTask.id == task_id))
            session.execute(update(Document).where(Document.id == document_id).values(active_version_id=None))
            session.flush()
            session.execute(delete(Document).where(Document.id == document_id))
            session.commit()
        engine.dispose()
