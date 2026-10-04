from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.jobs.models import BackgroundTask
from app.documents.models import Document, DocumentVersion
from app.runs.events import append_run_event
from app.runs.models import Run


@dataclass(frozen=True)
class TaskLease:
    id: UUID
    task_type: str
    payload: dict
    owner: str
    fencing_token: int
    attempt_count: int


def task_lease_is_current(session: Session, lease: TaskLease, *, lock: bool = False) -> bool:
    query = select(BackgroundTask.id).where(
        BackgroundTask.id == lease.id,
        BackgroundTask.status == "running",
        BackgroundTask.lease_owner == lease.owner,
        BackgroundTask.fencing_token == lease.fencing_token,
        BackgroundTask.lease_expires_at > func.clock_timestamp(),
    )
    if lock:
        query = query.with_for_update()
    return session.scalar(query) is not None


def renew_task_lease(session: Session, lease: TaskLease, lease_seconds: int = 30) -> bool:
    """Extend a live lease without allowing an expired or fenced worker to revive it."""
    with session.begin():
        updated = session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == lease.id,
                BackgroundTask.status == "running",
                BackgroundTask.lease_owner == lease.owner,
                BackgroundTask.fencing_token == lease.fencing_token,
                BackgroundTask.lease_expires_at > func.clock_timestamp(),
            )
            .values(
                lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds),
                updated_at=func.now(),
            )
        )
        return updated.rowcount == 1


def enqueue_task(session: Session, task_type: str, payload: dict) -> BackgroundTask:
    task = BackgroundTask(task_type=task_type, payload=payload)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def claim_next_task(session: Session, owner: str, lease_seconds: int = 30) -> TaskLease | None:
    claimable = or_(
        BackgroundTask.status == "queued",
        and_(BackgroundTask.status == "running", BackgroundTask.lease_expires_at < func.clock_timestamp()),
    )
    with session.begin():
        # Older workers may already hold an ingestion task claimed with the old
        # single-attempt default. Give that first in-flight claim the same one
        # automatic crash recovery as tasks claimed by this version.
        session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.task_type == "documents.ingest",
                BackgroundTask.status == "running",
                BackgroundTask.lease_expires_at < func.clock_timestamp(),
                BackgroundTask.attempt_count == 1,
                BackgroundTask.max_attempts == 1,
            )
            .values(max_attempts=2)
        )
        exhausted_tasks = session.scalars(
            select(BackgroundTask)
            .where(
                BackgroundTask.status == "running",
                BackgroundTask.lease_expires_at < func.clock_timestamp(),
                BackgroundTask.attempt_count >= BackgroundTask.max_attempts,
            )
            .with_for_update(skip_locked=True)
        ).all()
        for task in exhausted_tasks:
            if task.task_type == "documents.ingest":
                try:
                    document_id = UUID(str(task.payload.get("document_id", "")))
                    version_id = UUID(str(task.payload.get("version_id", "")))
                except (TypeError, ValueError, AttributeError):
                    continue
                version = session.scalar(
                    select(DocumentVersion)
                    .where(DocumentVersion.id == version_id, DocumentVersion.document_id == document_id)
                    .with_for_update()
                )
                document = session.scalar(
                    select(Document).where(Document.id == document_id).with_for_update()
                )
                if version is not None:
                    version.status = "failed"
                    version.error_code = "worker_lease_expired"
                if document is not None and not document.is_deleted and (
                    document.active_version_id is None or document.active_version_id == version_id
                ):
                    document.status = "failed"
                    document.error_code = "worker_lease_expired"
                continue
            if task.task_type != "research.run":
                continue
            try:
                run_id = UUID(str(task.payload.get("run_id", "")))
            except (TypeError, ValueError, AttributeError):
                continue
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            if run is None or run.status != "running":
                continue
            run.status = "cancelled" if run.cancel_requested_at is not None else "interrupted"
            run.error_code = "run_cancelled" if run.cancel_requested_at is not None else "worker_lease_expired"
            run.finished_at = func.now()
            append_run_event(session, run_id, run.status, {"status": run.status, "reason": run.error_code})
        session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.status == "running",
                BackgroundTask.lease_expires_at < func.clock_timestamp(),
                BackgroundTask.attempt_count >= BackgroundTask.max_attempts,
            )
            .values(
                status="failed",
                lease_owner=None,
                lease_expires_at=None,
                last_error_code="lease_expired_attempts_exhausted",
                updated_at=func.now(),
            )
        )
        task = session.scalar(
            select(BackgroundTask)
            .where(claimable, BackgroundTask.attempt_count < BackgroundTask.max_attempts)
            .order_by(BackgroundTask.created_at, BackgroundTask.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if task is None:
            return None
        if task.task_type == "documents.ingest" and task.attempt_count == 0 and task.max_attempts == 1:
            # Reserve one automatic retry for an interrupted worker while preserving
            # the existing user retry budget, which is extended by the retry API.
            task.max_attempts = 2
        task.status = "running"
        task.lease_owner = owner
        effective_lease = 900 if task.task_type == "research.run" else lease_seconds
        task.lease_expires_at = func.clock_timestamp() + timedelta(seconds=effective_lease)
        task.attempt_count += 1
        task.fencing_token += 1
        session.flush()
        lease = TaskLease(task.id, task.task_type, task.payload, owner, task.fencing_token, task.attempt_count)
    return lease


def finish_task(session: Session, lease: TaskLease, result: dict) -> bool:
    with session.begin():
        updated = session.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == lease.id,
                BackgroundTask.status == "running",
                BackgroundTask.lease_owner == lease.owner,
                BackgroundTask.fencing_token == lease.fencing_token,
                BackgroundTask.lease_expires_at > func.clock_timestamp(),
            )
            .values(status="succeeded", result=result, lease_owner=None, lease_expires_at=None, last_error_code=None, updated_at=func.now())
        )
        return updated.rowcount == 1


def fail_task(session: Session, lease: TaskLease, error_code: str) -> bool:
    with session.begin():
        task = session.scalar(
            select(BackgroundTask)
            .where(
                BackgroundTask.id == lease.id,
                BackgroundTask.status == "running",
                BackgroundTask.lease_owner == lease.owner,
                BackgroundTask.fencing_token == lease.fencing_token,
                BackgroundTask.lease_expires_at > func.clock_timestamp(),
            )
            .with_for_update()
        )
        if task is None:
            return False
        task.status = "queued" if task.attempt_count < task.max_attempts else "failed"
        task.last_error_code = error_code
        task.lease_owner = None
        task.lease_expires_at = None
    return True
