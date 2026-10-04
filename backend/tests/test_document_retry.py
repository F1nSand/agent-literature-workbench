import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session

from app.api.v1 import documents as document_routes
from app.documents.models import Document, DocumentVersion
from app.jobs.models import BackgroundTask


def test_failed_replacement_version_can_be_retried_without_changing_active_version(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL version retry test")

    engine = create_engine(database_url, pool_pre_ping=True)
    document_id = uuid4()
    active_version_id = uuid4()
    failed_version_id = uuid4()
    monkeypatch.setattr(document_routes, "get_engine", lambda: engine)

    def sha(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    try:
        with Session(engine) as session:
            document = Document(
                id=document_id,
                title="retry integration test",
                filename="active.pdf",
                storage_key=uuid4().hex,
                sha256=sha("document" + str(document_id)),
                status="ready",
                page_count=1,
                active_version_id=None,
                is_deleted=False,
            )
            session.add(document)
            session.flush()
            active_version = DocumentVersion(
                id=active_version_id,
                document_id=document_id,
                version_number=1,
                filename="active.pdf",
                storage_key=uuid4().hex,
                sha256=sha("active" + str(document_id)),
                status="ready",
                page_count=1,
                processing_config_hash=sha("config"),
            )
            failed_version = DocumentVersion(
                id=failed_version_id,
                document_id=document_id,
                version_number=2,
                filename="replacement.pdf",
                storage_key=uuid4().hex,
                sha256=sha("failed" + str(document_id)),
                status="failed",
                page_count=1,
                error_code="provider_timeout",
                processing_config_hash=sha("config"),
            )
            session.add_all([active_version, failed_version])
            session.flush()
            document.active_version_id = active_version_id
            session.add(BackgroundTask(
                task_type="documents.ingest",
                status="failed",
                payload={"document_id": str(document_id), "version_id": str(failed_version_id)},
                attempt_count=1,
                max_attempts=1,
                fencing_token=1,
                last_error_code="provider_timeout",
            ))
            session.commit()

        first = document_routes.retry_document_version(document_id, failed_version_id)
        second = document_routes.retry_document_version(document_id, failed_version_id)

        assert first.status == second.status == "queued"
        assert first.is_active is False
        with Session(engine) as session:
            document = session.get(Document, document_id)
            version = session.get(DocumentVersion, failed_version_id)
            tasks = [
                task for task in session.scalars(select(BackgroundTask).where(BackgroundTask.task_type == "documents.ingest"))
                if task.payload.get("document_id") == str(document_id)
                and task.payload.get("version_id") == str(failed_version_id)
            ]
            assert document is not None and document.status == "ready"
            assert document.active_version_id == active_version_id
            assert version is not None and version.error_code is None
            assert len(tasks) == 1
            assert tasks[0].status == "queued"
            assert tasks[0].max_attempts == 2
    finally:
        with Session(engine) as session:
            tasks = session.scalars(select(BackgroundTask).where(BackgroundTask.task_type == "documents.ingest")).all()
            for task in tasks:
                if task.payload.get("document_id") == str(document_id):
                    session.delete(task)
            session.execute(update(Document).where(Document.id == document_id).values(active_version_id=None))
            session.execute(delete(Document).where(Document.id == document_id))
            session.commit()
        engine.dispose()
