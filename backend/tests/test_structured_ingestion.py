import hashlib
import os
from types import SimpleNamespace
from uuid import uuid4

import fitz
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session

from app.api.v1 import documents as document_routes
from app.documents import pipeline
from app.documents.models import (
    ContentUnit,
    Document,
    DocumentVersion,
    EmbeddingCache,
    IndexGeneration,
)
from app.providers.siliconflow import ProviderResult


class FakeEmbeddingClient:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, inputs: list[str], model: str | None = None, dimensions: int | None = None):
        self.calls += 1
        vector = [1.0] + [0.0] * 2559
        return [vector for _ in inputs], ProviderResult({}, 10, 0, 10, 1)


def _structured_pdf(path, token: str) -> str:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 54), f"Study note {token} with enough searchable text to keep native extraction active.")
    page.insert_text((72, 88), "Table 1: Metrics")
    x_edges = [72, 245, 418]
    y_edges = [105, 145, 185]
    for x in x_edges:
        page.draw_line((x, y_edges[0]), (x, y_edges[-1]), color=(0, 0, 0))
    for y in y_edges:
        page.draw_line((x_edges[0], y), (x_edges[-1], y), color=(0, 0, 0))
    for row_index, row in enumerate([["Method", "Score"], [token[:5], "0.86"]]):
        for column_index, value in enumerate(row):
            page.insert_text((x_edges[column_index] + 10, y_edges[row_index] + 25), value)
    page.insert_text((72, 220), f"score_{token[:5]} = 1 - (1 - p)^k")
    page.draw_rect(fitz.Rect(80, 260, 420, 420), color=(0, 0, 0))
    page.insert_text((84, 445), f"Figure 1: Accuracy for {token[:5]}")
    page.insert_text((72, 485), f"The native PDF page ends with source-specific marker {token}.")
    document.save(path)
    document.close()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_ingestion_persists_structured_source_boundaries_and_reader_metadata(monkeypatch, tmp_path) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run structured ingestion test")

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(pipeline, "get_engine", lambda: engine)
    monkeypatch.setattr(document_routes, "get_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path), max_pdf_pages=500))
    monkeypatch.setattr(document_routes, "_storage_path", lambda key: pdf_dir / f"{key}.pdf")
    fake_client = FakeEmbeddingClient()
    monkeypatch.setattr(pipeline, "SiliconFlowClient", lambda document_id, task_id: fake_client)

    token = uuid4().hex
    document_id, version_id, task_id = uuid4(), uuid4(), uuid4()
    storage_key = uuid4().hex
    pdf_dir = tmp_path / "documents"
    pdf_dir.mkdir()
    pdf_path = pdf_dir / f"{storage_key}.pdf"
    file_hash = _structured_pdf(pdf_path, token)
    config_hash = hashlib.sha256(b"structured-parser-test").hexdigest()
    cache_keys: list[str] = []
    try:
        with Session(engine) as session:
            generation = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
            assert generation is not None
            document = Document(
                id=document_id,
                title="structured parser test",
                filename="structured-parser-test.pdf",
                storage_key=storage_key,
                sha256=file_hash,
                status="queued",
                page_count=1,
                active_version_id=None,
                is_deleted=False,
            )
            session.add(document)
            session.flush()
            session.add(DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_number=1,
                filename=document.filename,
                storage_key=storage_key,
                sha256=file_hash,
                status="queued",
                page_count=1,
                processing_config_hash=config_hash,
            ))
            session.flush()
            document.active_version_id = version_id
            session.commit()

        result = pipeline.process_document_ingestion(document_id, task_id, version_id)
        assert result["status"] == "ready"
        assert fake_client.calls > 0

        with Session(engine) as session:
            units = session.scalars(
                select(ContentUnit).where(ContentUnit.version_id == version_id).order_by(ContentUnit.ordinal)
            ).all()
            cache_keys = [
                pipeline._embedding_cache_key(unit.text, "content", "Qwen/Qwen3-Embedding-4B", unit.text_sha256)
                for unit in units
            ]
            typed_units = {unit.content_type: unit for unit in units}
            assert {"text", "table", "formula", "chart"}.issubset(typed_units)
            assert typed_units["text"].extra["source"] == "pdf_text"
            assert typed_units["text"].extra["quote_eligible"] is True
            assert typed_units["table"].extra["is_derived"] is True
            assert typed_units["table"].extra["bbox"]
            assert typed_units["formula"].extra["quote_eligible"] is True
            assert typed_units["chart"].extra["source"] == "pdf_text"
            assert typed_units["chart"].extra["quote_eligible"] is True

        page = document_routes.read_page(document_id, 1)
        assert page.extraction_method == "pdf_text"
        assert page.is_derived is False
        assert {unit.content_type for unit in page.content_units} >= {"text", "table", "formula", "chart"}
        assert any(
            unit.content_type == "table" and unit.is_derived and not unit.quote_eligible
            for unit in page.content_units
        )
        preview = document_routes.read_page_preview(document_id, 1, version_id)
        assert preview.media_type == "image/png"
        assert preview.body.startswith(b"\x89PNG\r\n\x1a\n")
        with pytest.raises(HTTPException) as stale_version:
            document_routes.read_page_preview(document_id, 1, uuid4())
        assert stale_version.value.status_code == 409
    finally:
        with Session(engine) as session:
            session.execute(update(Document).where(Document.id == document_id).values(active_version_id=None))
            session.execute(delete(Document).where(Document.id == document_id))
            if cache_keys:
                session.execute(delete(EmbeddingCache).where(EmbeddingCache.cache_key.in_(cache_keys)))
            session.commit()
        engine.dispose()
