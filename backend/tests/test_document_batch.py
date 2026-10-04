import hashlib
import os
from types import SimpleNamespace

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session

from app.api.v1 import documents as document_routes
from app.documents.models import Document, DocumentVersion
from app.jobs.models import BackgroundTask
from app.main import app


def _pdf_bytes(label: str) -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), label)
    data = document.tobytes()
    document.close()
    return data


def test_batch_upload_returns_per_file_results_and_deduplicates(monkeypatch, tmp_path) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL batch upload test")

    engine = create_engine(database_url, pool_pre_ping=True)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(document_routes, "get_engine", lambda: engine)
    monkeypatch.setattr(document_routes, "get_settings", lambda: SimpleNamespace(
        data_dir=str(data_dir),
        max_pdf_bytes=100 * 1024 * 1024,
        max_pdf_pages=500,
        siliconflow_ocr_model="test-ocr",
        siliconflow_embedding_model="test-embedding",
        embedding_dimensions=2560,
    ))
    client = TestClient(app)
    items = [(f"paper-{index}.pdf", _pdf_bytes(f"batch test paper {index}")) for index in range(29)]
    thirty_items = [(f"limit-{index}.pdf", _pdf_bytes(f"maximum batch paper {index}")) for index in range(30)]
    hashes = {hashlib.sha256(content).hexdigest() for _, content in items + thirty_items}

    try:
        payload = [("files", (name, content, "application/pdf")) for name, content in items]
        payload.append(("files", ("unsupported.bin", b"not a supported document", "application/octet-stream")))
        first = client.post("/api/v1/documents/batch", files=payload)
        assert first.status_code == 202
        first_items = first.json()["items"]
        assert len(first_items) == 30
        assert [row["status"] for row in first_items].count("queued") == 29
        assert first_items[-1]["status"] == "rejected"
        assert "PDF" in first_items[-1]["error_message"] and "EPUB" in first_items[-1]["error_message"]

        repeated_payload = [("files", (name, content, "application/pdf")) for name, content in items]
        repeated_payload.append(("files", ("unsupported.bin", b"not a supported document", "application/octet-stream")))
        repeated = client.post("/api/v1/documents/batch", files=repeated_payload)
        assert repeated.status_code == 202
        repeated_items = repeated.json()["items"]
        assert [row["status"] for row in repeated_items].count("duplicate") == 29
        assert repeated_items[-1]["status"] == "rejected"

        maximum_payload = [("files", (name, content, "application/pdf")) for name, content in thirty_items]
        maximum = client.post("/api/v1/documents/batch", files=maximum_payload)
        assert maximum.status_code == 202
        assert len(maximum.json()["items"]) == 30
        assert all(row["status"] == "queued" for row in maximum.json()["items"])

        too_many = client.post(
            "/api/v1/documents/batch",
            files=[("files", (f"overflow-{index}.pdf", _pdf_bytes(f"overflow {index}"), "application/pdf")) for index in range(31)],
        )
        assert too_many.status_code == 413

        with Session(engine) as session:
            versions = session.scalars(select(DocumentVersion).where(DocumentVersion.sha256.in_(hashes))).all()
            assert len(versions) == 59
            document_ids = {version.document_id for version in versions}
            tasks = session.scalars(select(BackgroundTask).where(BackgroundTask.task_type == "documents.ingest")).all()
            batch_tasks = [task for task in tasks if task.payload.get("document_id") in {str(item) for item in document_ids}]
            assert len(batch_tasks) == 59
            assert all(task.status == "queued" and task.max_attempts == 1 for task in batch_tasks)
    finally:
        with Session(engine) as session:
            documents = session.scalars(select(Document).where(Document.sha256.in_(hashes))).all()
            document_ids = [row.id for row in documents]
            if document_ids:
                document_id_strings = {str(item) for item in document_ids}
                for task in session.scalars(select(BackgroundTask).where(BackgroundTask.task_type == "documents.ingest")).all():
                    if task.payload.get("document_id") in document_id_strings:
                        session.delete(task)
                session.execute(update(Document).where(Document.id.in_(document_ids)).values(active_version_id=None))
                session.flush()
                session.execute(delete(Document).where(Document.id.in_(document_ids)))
                session.commit()
        engine.dispose()
