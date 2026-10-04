import logging
import os
import socket
import threading
import time
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.documents.pipeline import JobFailure, answer_document_question, process_document_ingestion, rebuild_index_generation
from app.jobs.store import TaskLease, claim_next_task, fail_task, finish_task, renew_task_lease

logger = logging.getLogger("workbench.worker")
WORKER_LEASE_SECONDS = 30


def _renew_lease_while_running(engine, lease: TaskLease, stop: threading.Event) -> None:
    interval = max(1, WORKER_LEASE_SECONDS // 3)
    while not stop.wait(interval):
        try:
            with Session(engine) as session:
                if not renew_task_lease(session, lease, WORKER_LEASE_SECONDS):
                    logger.warning("task lease lost id=%s", lease.id)
                    return
        except Exception:
            logger.error("task lease renewal failed id=%s", lease.id)


def handle_task(lease: TaskLease) -> dict:
    if lease.task_type == "system.probe":
        return {"ok": True, "worker_attempt": lease.attempt_count}
    if lease.task_type == "documents.ingest":
        try:
            document_id = UUID(lease.payload["document_id"])
            version_id = UUID(lease.payload["version_id"]) if lease.payload.get("version_id") else None
        except (KeyError, ValueError, TypeError):
            raise JobFailure("document_task_payload_invalid") from None
        return process_document_ingestion(document_id, lease.id, version_id, lease)
    if lease.task_type == "index.rebuild":
        try:
            source_generation_id = UUID(lease.payload["source_generation_id"])
            generation_id = UUID(lease.payload["generation_id"])
        except (KeyError, ValueError, TypeError):
            raise JobFailure("index_rebuild_task_payload_invalid") from None
        return rebuild_index_generation(source_generation_id, generation_id)
    if lease.task_type == "documents.answer":
        try:
            document_id = UUID(lease.payload["document_id"])
            version_id = UUID(lease.payload["version_id"]) if lease.payload.get("version_id") else None
            question = lease.payload["question"]
            if not isinstance(question, str):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise JobFailure("question_task_payload_invalid") from None
        return answer_document_question(document_id, lease.id, question, version_id)
    if lease.task_type == "research.run":
        try:
            run_id = UUID(lease.payload["run_id"])
            resume = lease.payload.get("resume", False)
            if not isinstance(resume, bool):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise JobFailure("research_task_payload_invalid") from None
        from app.agent.executor import ResearchRunError, execute_research_task

        try:
            return execute_research_task(lease, run_id, resume=resume)
        except ResearchRunError as error:
            raise JobFailure(error.error_code) from None
    raise JobFailure("worker_unhandled_task")


def run_forever() -> None:
    worker_id = f"{socket.gethostname()}-{uuid4().hex[:8]}"
    engine = get_engine()
    logger.info("worker started id=%s", worker_id)
    while True:
        lease = None
        try:
            with Session(engine) as session:
                lease = claim_next_task(session, worker_id, lease_seconds=WORKER_LEASE_SECONDS)
                if lease is not None:
                    stop_heartbeat = threading.Event()
                    heartbeat = threading.Thread(
                        target=_renew_lease_while_running,
                        args=(engine, lease, stop_heartbeat),
                        name=f"task-lease-{lease.id}",
                        daemon=True,
                    )
                    heartbeat.start()
                    try:
                        try:
                            result = handle_task(lease)
                        except JobFailure as error:
                            fail_task(session, lease, error.error_code)
                        except ValueError:
                            fail_task(session, lease, "worker_unhandled_task")
                        else:
                            if not finish_task(session, lease, result):
                                logger.warning("task lease lost id=%s", lease.id)
                    finally:
                        stop_heartbeat.set()
                        heartbeat.join(timeout=WORKER_LEASE_SECONDS)
                    logger.info("task handled id=%s attempt=%d", lease.id, lease.attempt_count)
                else:
                    time.sleep(1)
        except Exception:
            # Retain a generic event only; exception text can contain payloads or connection data.
            logger.error("worker loop failed")
            if lease is not None:
                try:
                    with Session(engine) as session:
                        fail_task(session, lease, "worker_internal_error")
                except Exception:
                    logger.error("could not persist worker failure id=%s", lease.id)
            time.sleep(2)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_forever()
