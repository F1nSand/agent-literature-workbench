import hashlib
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session

from app.api.v1 import documents as document_routes
from app.documents import pipeline
from app.documents.models import Document, DocumentVersion
from app.documents.schemas import QuestionCreate
from app.jobs.models import BackgroundTask


def _seed_document(engine, document_id, versions: list[tuple], active_version_id):
    def sha(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    with Session(engine) as session:
        document = Document(
            id=document_id,
            title="lifecycle integration test",
            filename="active.pdf",
            storage_key=uuid4().hex,
            sha256=sha("doc" + str(document_id)),
            status="ready",
            page_count=1,
            active_version_id=None,
            is_deleted=False,
        )
        session.add(document)
        session.flush()
        for version_id, number, filename, status in versions:
            session.add(DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_number=number,
                filename=filename,
                storage_key=uuid4().hex,
                sha256=sha(str(document_id) + filename),
                status=status,
                page_count=1,
                error_code="provider_timeout" if status == "failed" else None,
                processing_config_hash=sha("config"),
            ))
        session.flush()
        document.active_version_id = active_version_id
        session.commit()


def _cleanup(engine, document_id) -> None:
    with Session(engine) as session:
        tasks = session.scalars(select(BackgroundTask)).all()
        for task in tasks:
            if task.payload.get("document_id") == str(document_id):
                session.delete(task)
        session.execute(update(Document).where(Document.id == document_id).values(active_version_id=None))
        session.execute(delete(Document).where(Document.id == document_id))
        session.commit()


def test_failed_replacement_leaves_previous_active_version_available(monkeypatch, tmp_path) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL lifecycle test")

    engine = create_engine(database_url, pool_pre_ping=True)
    document_id, active_id, replacement_id, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    monkeypatch.setattr(pipeline, "get_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path), max_pdf_pages=500))
    _seed_document(
        engine,
        document_id,
        [(active_id, 1, "active.pdf", "ready"), (replacement_id, 2, "replacement.pdf", "queued")],
        active_id,
    )
    try:
        with pytest.raises(pipeline.JobFailure, match="document_file_missing"):
            pipeline.process_document_ingestion(document_id, task_id, replacement_id)
        with Session(engine) as session:
            document = session.get(Document, document_id)
            replacement = session.get(DocumentVersion, replacement_id)
            assert document is not None and document.active_version_id == active_id
            assert document.status == "ready"
            assert replacement is not None and replacement.status == "failed"
    finally:
        _cleanup(engine, document_id)
        engine.dispose()


def test_soft_deleted_document_is_rejected_by_reader_and_question_routes(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL deletion test")

    engine = create_engine(database_url, pool_pre_ping=True)
    document_id, version_id = uuid4(), uuid4()
    monkeypatch.setattr(document_routes, "get_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "get_engine", lambda: engine)

    class TestSecret:
        @staticmethod
        def get_secret_value() -> str:
            return "test-only"

    monkeypatch.setattr(document_routes, "get_settings", lambda: SimpleNamespace(siliconflow_api_key=TestSecret()))
    _seed_document(engine, document_id, [(version_id, 1, "active.pdf", "ready")], version_id)
    try:
        document_routes.delete_document(document_id)
        protected_calls = [
            lambda: document_routes.read_document(document_id),
            lambda: document_routes.list_document_versions(document_id),
            lambda: document_routes.read_pdf(document_id),
            lambda: document_routes.read_page(document_id, 1),
            lambda: document_routes.ask_document_question(document_id, QuestionCreate(question="请总结")),
        ]
        for call in protected_calls:
            with pytest.raises(HTTPException) as error:
                call()
            assert error.value.status_code == 404

        with pytest.raises(pipeline.JobFailure, match="document_not_available"):
            pipeline.retrieve_evidence(document_id, "question", object())
    finally:
        _cleanup(engine, document_id)
        engine.dispose()
