import multiprocessing
import os
import threading
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from sqlalchemy import create_engine

from app.api.v1 import documents as document_routes
from app.documents.models import Document, DocumentVersion
from app.jobs.models import BackgroundTask
from app.jobs.store import claim_next_task, fail_task, finish_task, renew_task_lease
from app.jobs import worker as worker_module
from app.runs.models import Run, RunEvent
from app.topics.models import Conversation, Topic


def _run_crash_acceptance_worker(
    database_url: str,
    blocked_task_id: str | None,
    started_event,
    started_attempt,
    successful_task_ids: tuple[str, ...] = (),
) -> None:
    """Run the real worker loop with deterministic, test-only ingestion work."""
    import os

    from app.jobs import worker as worker_module

    engine = create_engine(database_url, pool_pre_ping=True)
    worker_module.get_engine = lambda: engine
    worker_module.WORKER_LEASE_SECONDS = 3
    completed: set[str] = set()

    def handle_test_ingestion(lease):
        if lease.task_type != "documents.ingest":
            raise AssertionError("crash recovery test claimed an unexpected task")
        if str(lease.id) == blocked_task_id:
            started_attempt.value = lease.attempt_count
            started_event.set()
            while True:
                time.sleep(0.1)
        return {"processed": True, "attempt": lease.attempt_count}

    finish_task = worker_module.finish_task

    def finish_and_stop_after_batch(session, lease, result):
        finished = finish_task(session, lease, result)
        if finished:
            completed.add(str(lease.id))
            if successful_task_ids and set(successful_task_ids).issubset(completed):
                os._exit(0)
        return finished

    worker_module.handle_task = handle_test_ingestion
    worker_module.finish_task = finish_and_stop_after_batch
    worker_module.run_forever()


def test_expired_lease_is_reclaimed_and_fences_old_worker() -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL task recovery test")

    engine = create_engine(database_url, pool_pre_ping=True)
    task_id = uuid4()
    try:
        with Session(engine) as session:
            task = BackgroundTask(id=task_id, task_type="system.probe", status="queued", payload={}, max_attempts=3)
            session.add(task)
            session.commit()

        with Session(engine) as session:
            abandoned = claim_next_task(session, "worker-that-will-crash", lease_seconds=30)
        assert abandoned is not None
        assert abandoned.id == task_id
        assert abandoned.fencing_token == 1
        with Session(engine) as session:
            assert renew_task_lease(session, abandoned, lease_seconds=60) is True

        with Session(engine) as session:
            session.execute(
                update(BackgroundTask)
                .where(BackgroundTask.id == task_id)
                .values(lease_expires_at=func.now() - timedelta(seconds=1))
            )
            session.commit()

        with Session(engine) as session:
            assert renew_task_lease(session, abandoned, lease_seconds=60) is False
            assert finish_task(session, abandoned, {"stale": True}) is False
            assert fail_task(session, abandoned, "stale_failure") is False
            row = session.get(BackgroundTask, task_id)
            assert row is not None and row.status == "running"

        with Session(engine) as session:
            recovered = claim_next_task(session, "recovery-worker", lease_seconds=30)
        assert recovered is not None
        assert recovered.id == task_id
        assert recovered.fencing_token == 2
        assert recovered.attempt_count == 2

        with Session(engine) as session:
            assert finish_task(session, abandoned, {"stale": True}) is False
            assert finish_task(session, recovered, {"ok": True}) is True
            row = session.get(BackgroundTask, task_id)
            assert row is not None
            assert row.status == "succeeded"
            assert row.result == {"ok": True}
    finally:
        with Session(engine) as session:
            session.execute(BackgroundTask.__table__.delete().where(BackgroundTask.id == task_id))
            session.commit()
        engine.dispose()


def test_expired_ingestion_recovers_then_fails_visibly_and_preserves_batch_queue(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL ingestion recovery test")

    import hashlib

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(document_routes, "get_engine", lambda: engine)
    document_id, version_id, task_id = uuid4(), uuid4(), uuid4()
    queued_document_id, queued_version_id, queued_task_id = uuid4(), uuid4(), uuid4()
    sha = lambda value: hashlib.sha256(value.encode()).hexdigest()
    try:
        with Session(engine) as session:
            document = Document(
                id=document_id, title="Interrupted PDF", filename="interrupted.pdf",
                storage_key=uuid4().hex, sha256=sha(str(document_id)), status="processing",
                active_version_id=None, is_deleted=False,
            )
            queued_document = Document(
                id=queued_document_id, title="Queued PDF", filename="queued.pdf",
                storage_key=uuid4().hex, sha256=sha(str(queued_document_id)), status="queued",
                active_version_id=None, is_deleted=False,
            )
            session.add_all([document, queued_document])
            session.flush()
            version = DocumentVersion(
                id=version_id, document_id=document_id, version_number=1, filename=document.filename,
                storage_key=document.storage_key, sha256=document.sha256, status="processing",
                page_count=1, processing_config_hash=sha("config"),
            )
            queued_version = DocumentVersion(
                id=queued_version_id, document_id=queued_document_id, version_number=1, filename=queued_document.filename,
                storage_key=queued_document.storage_key, sha256=queued_document.sha256, status="queued",
                page_count=1, processing_config_hash=sha("config"),
            )
            session.add_all([version, queued_version])
            session.add_all([
                BackgroundTask(
                    id=task_id, task_type="documents.ingest", status="running",
                    payload={"document_id": str(document_id), "version_id": str(version_id)},
                    attempt_count=1, max_attempts=1, fencing_token=1,
                    lease_owner="crashed-worker", lease_expires_at=func.now() - timedelta(seconds=1),
                ),
                BackgroundTask(
                    id=queued_task_id, task_type="documents.ingest", status="queued",
                    payload={"document_id": str(queued_document_id), "version_id": str(queued_version_id)},
                    max_attempts=2, created_at=func.now() + timedelta(days=1),
                ),
            ])
            session.commit()

        with Session(engine) as session:
            recovered = claim_next_task(session, "restarted-worker")
        assert recovered is not None and recovered.id == task_id
        assert recovered.attempt_count == 2 and recovered.fencing_token == 2

        with Session(engine) as session:
            session.execute(
                update(BackgroundTask).where(BackgroundTask.id == task_id)
                .values(lease_expires_at=func.now() - timedelta(seconds=1))
            )
            session.commit()
        with Session(engine) as session:
            next_lease = claim_next_task(session, "batch-worker")
        assert next_lease is not None and next_lease.id == queued_task_id

        with Session(engine) as session:
            task = session.get(BackgroundTask, task_id)
            version = session.get(DocumentVersion, version_id)
            document = session.get(Document, document_id)
            assert task is not None and task.status == "failed"
            assert task.last_error_code == "lease_expired_attempts_exhausted"
            assert version is not None and version.status == "failed"
            assert version.error_code == "worker_lease_expired"
            assert document is not None and document.status == "failed"
            assert document.error_code == "worker_lease_expired"
            assert session.get(BackgroundTask, queued_task_id).status == "running"

        assert document_routes.retry_document_version(document_id, version_id).status == "queued"
        with Session(engine) as session:
            assert session.get(BackgroundTask, task_id).status == "queued"
            assert session.get(BackgroundTask, task_id).max_attempts == 3
            assert session.get(DocumentVersion, version_id).status == "queued"
    finally:
        with Session(engine) as session:
            session.execute(BackgroundTask.__table__.delete().where(BackgroundTask.id.in_([task_id, queued_task_id])))
            session.execute(update(Document).where(Document.id.in_([document_id, queued_document_id])).values(active_version_id=None))
            session.flush()
            session.execute(DocumentVersion.__table__.delete().where(DocumentVersion.id.in_([version_id, queued_version_id])))
            session.execute(Document.__table__.delete().where(Document.id.in_([document_id, queued_document_id])))
            session.commit()
        engine.dispose()


def test_worker_heartbeat_keeps_long_task_lease_alive(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL worker heartbeat test")

    engine = create_engine(database_url, pool_pre_ping=True)
    task_id = uuid4()
    try:
        with Session(engine) as session:
            session.add(BackgroundTask(id=task_id, task_type="system.probe", payload={}, max_attempts=2))
            session.commit()
        with Session(engine) as session:
            lease = claim_next_task(session, "long-running-worker", lease_seconds=3)
        assert lease is not None

        monkeypatch.setattr(worker_module, "WORKER_LEASE_SECONDS", 3)
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=worker_module._renew_lease_while_running, args=(engine, lease, stop), daemon=True,
        )
        heartbeat.start()
        time.sleep(3.4)
        stop.set()
        heartbeat.join(timeout=2)
        assert not heartbeat.is_alive()

        with Session(engine) as session:
            row = session.get(BackgroundTask, task_id)
            assert row is not None and row.lease_expires_at > session.scalar(select(func.clock_timestamp()))
        with Session(engine) as session:
            assert finish_task(session, lease, {"ok": True}) is True
    finally:
        with Session(engine) as session:
            session.execute(BackgroundTask.__table__.delete().where(BackgroundTask.id == task_id))
            session.commit()
        engine.dispose()


def test_worker_process_restart_recovers_batch_and_continues_queue() -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL worker process recovery test")
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("worker process recovery test currently requires the Linux fork start method")

    engine = create_engine(database_url, pool_pre_ping=True)
    first_task_id, next_task_id = uuid4(), uuid4()
    context = multiprocessing.get_context("fork")
    started = context.Event()
    started_attempt = context.Value("i", 0)
    crashed_worker = None
    recovery_worker = None
    try:
        with Session(engine) as session:
            session.add_all([
                BackgroundTask(
                    id=first_task_id,
                    task_type="documents.ingest",
                    status="queued",
                    payload={"test_batch": "crash-recovery"},
                    max_attempts=1,
                    created_at=func.clock_timestamp() - timedelta(seconds=5),
                ),
                BackgroundTask(
                    id=next_task_id,
                    task_type="documents.ingest",
                    status="queued",
                    payload={"test_batch": "crash-recovery"},
                    max_attempts=1,
                    created_at=func.clock_timestamp(),
                ),
            ])
            session.commit()

        crashed_worker = context.Process(
            target=_run_crash_acceptance_worker,
            args=(database_url, str(first_task_id), started, started_attempt),
        )
        crashed_worker.start()
        assert started.wait(timeout=10), "worker did not claim the first batch item"
        assert started_attempt.value == 1
        crashed_worker.terminate()
        crashed_worker.join(timeout=5)
        assert not crashed_worker.is_alive()

        expires_by = time.monotonic() + 10
        lease_expired = False
        while time.monotonic() < expires_by:
            with Session(engine) as session:
                row = session.get(BackgroundTask, first_task_id)
                lease_expired = bool(
                    row is not None
                    and row.status == "running"
                    and row.lease_expires_at <= session.scalar(select(func.clock_timestamp()))
                )
            if lease_expired:
                break
            time.sleep(0.1)
        assert lease_expired, "crashed worker lease did not expire"

        recovery_worker = context.Process(
            target=_run_crash_acceptance_worker,
            args=(database_url, None, context.Event(), context.Value("i", 0), (str(first_task_id), str(next_task_id))),
        )
        recovery_worker.start()
        recovery_worker.join(timeout=15)
        if recovery_worker.is_alive():
            recovery_worker.terminate()
            recovery_worker.join(timeout=5)
        assert recovery_worker.exitcode == 0, "recovery worker did not finish both batch items"

        with Session(engine) as session:
            first = session.get(BackgroundTask, first_task_id)
            next_task = session.get(BackgroundTask, next_task_id)
            assert first is not None and first.status == "succeeded"
            assert first.attempt_count == 2 and first.max_attempts == 2
            assert first.result == {"processed": True, "attempt": 2}
            assert next_task is not None and next_task.status == "succeeded"
            assert next_task.attempt_count == 1 and next_task.max_attempts == 2
            assert next_task.result == {"processed": True, "attempt": 1}
    finally:
        for process in (crashed_worker, recovery_worker):
            if process is not None and process.is_alive():
                process.terminate()
                process.join(timeout=5)
        with Session(engine) as session:
            session.execute(BackgroundTask.__table__.delete().where(BackgroundTask.id.in_([first_task_id, next_task_id])))
            session.commit()
        engine.dispose()


@pytest.mark.parametrize("cancel_requested", [False, True])
def test_expired_research_run_becomes_interrupted_or_cancelled(cancel_requested: bool) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL research recovery test")

    engine = create_engine(database_url, pool_pre_ping=True)
    topic_id, conversation_id, run_id, task_id = uuid4(), uuid4(), uuid4(), uuid4()
    expected = "cancelled" if cancel_requested else "interrupted"
    try:
        with Session(engine) as session:
            topic = Topic(id=topic_id, title="Recovery test")
            session.add(topic)
            session.flush()
            conversation = Conversation(id=conversation_id, topic_id=topic_id, title="Recovery")
            session.add(conversation)
            session.flush()
            run = Run(
                id=run_id,
                conversation_id=conversation_id,
                status="running",
                scope_mode="topic",
                scope_snapshot={"schema_version": 1},
                scope_hash="f" * 64,
                budget_snapshot={"max_tool_calls": 12},
                idempotency_key=uuid4().hex,
                attempt_count=1,
                cancel_requested_at=func.now() if cancel_requested else None,
            )
            task = BackgroundTask(
                id=task_id,
                task_type="research.run",
                status="running",
                payload={"run_id": str(run_id)},
                max_attempts=1,
                attempt_count=1,
                fencing_token=1,
                lease_owner="worker-that-will-crash",
            )
            session.add_all([run, task])
            session.flush()
            task.lease_expires_at = func.now() - timedelta(seconds=1)
            session.commit()

        with Session(engine) as session:
            assert claim_next_task(session, "recovery-worker") is None
        with Session(engine) as session:
            run = session.get(Run, run_id)
            task = session.get(BackgroundTask, task_id)
            event = session.scalar(select(RunEvent).where(RunEvent.run_id == run_id))
            assert run is not None and run.status == expected
            assert task is not None and task.status == "failed"
            assert event is not None and event.event_type == expected
            assert event.payload["reason"] == ("run_cancelled" if cancel_requested else "worker_lease_expired")
    finally:
        with Session(engine) as session:
            session.execute(BackgroundTask.__table__.delete().where(BackgroundTask.id == task_id))
            session.execute(RunEvent.__table__.delete().where(RunEvent.run_id == run_id))
            session.execute(Run.__table__.delete().where(Run.id == run_id))
            session.execute(Conversation.__table__.delete().where(Conversation.id == conversation_id))
            session.execute(Topic.__table__.delete().where(Topic.id == topic_id))
            session.commit()
        engine.dispose()
