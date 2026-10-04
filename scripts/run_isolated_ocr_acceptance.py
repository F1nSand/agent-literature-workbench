"""Run an isolated real-provider OCR → retrieval → answer → page-preview acceptance flow."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, "/app")

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("expected path to a one-page image-only PDF")
    fixture_path = Path(sys.argv[1]).resolve()
    if not fixture_path.is_file():
        raise SystemExit("image-only fixture is missing")

    base_url = make_url(os.environ["DATABASE_URL"])
    database_name = f"m3_ocr_{uuid4().hex[:12]}"
    test_url = base_url.set(database=database_name)
    admin_engine = create_engine(base_url.set(database="postgres"), poolclass=NullPool)
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')

    data_dir = Path("/tmp") / f"{database_name}-data"
    os.environ["DATABASE_URL"] = test_url.render_as_string(hide_password=False)
    os.environ["DATA_DIR"] = str(data_dir)
    data_dir.mkdir(parents=True, exist_ok=False)

    result: dict[str, object] = {"status": "failed", "test_database_isolated": True}
    app_engine = None
    try:
        os.chdir("/app")
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("/app/alembic.ini"), "head")

        from fastapi.testclient import TestClient

        from app.db.session import get_engine
        from app.documents.models import ContentUnit, Document, DocumentPage, IndexGeneration, ProviderCall
        from app.documents.pipeline import _embedding_profile_hash
        from app.core.config import get_settings
        from app.main import app
        from app.jobs.models import BackgroundTask
        from app.jobs.store import claim_next_task, fail_task, finish_task
        from app.documents.pipeline import JobFailure
        from app.jobs.worker import handle_task

        app_engine = get_engine()
        settings = get_settings()
        with Session(app_engine) as session:
            active = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
            if active is None:
                model = settings.siliconflow_embedding_model
                session.add(IndexGeneration(
                    provider="siliconflow",
                    model=model,
                    dimensions=settings.embedding_dimensions,
                    config_hash=_embedding_profile_hash(model, settings.embedding_dimensions),
                    status="active",
                ))
                session.commit()

        def run_one_task(owner: str) -> dict[str, object]:
            with Session(app_engine) as session:
                lease = claim_next_task(session, owner)
            if lease is None:
                raise RuntimeError("expected task was not claimable")
            try:
                task_result = handle_task(lease)
            except JobFailure as error:
                with Session(app_engine) as session:
                    fail_task(session, lease, error.error_code)
                return {"task_id": str(lease.id), "task_type": lease.task_type, "status": "failed", "error_code": error.error_code}
            except Exception as error:
                with Session(app_engine) as session:
                    fail_task(session, lease, "acceptance_unexpected_error")
                return {"task_id": str(lease.id), "task_type": lease.task_type, "status": "failed", "error_type": type(error).__name__}
            with Session(app_engine) as session:
                completed = finish_task(session, lease, task_result)
            if not completed:
                return {"task_id": str(lease.id), "task_type": lease.task_type, "status": "failed", "error_code": "fencing_rejected"}
            return {"task_id": str(lease.id), "task_type": lease.task_type, "status": "succeeded"}

        with TestClient(app) as client:
            with fixture_path.open("rb") as handle:
                uploaded = client.post(
                    "/api/v1/documents/batch",
                    files=[("files", ("rasterized-mobia-page-05.pdf", handle, "application/pdf"))],
                )
            if uploaded.status_code != 202:
                raise RuntimeError(f"upload_http_{uploaded.status_code}")
            item = uploaded.json()["items"][0]
            if item["status"] != "queued" or not item.get("document"):
                raise RuntimeError("image_only_document_was_not_queued")
            document_id = item["document"]["id"]
            with Session(app_engine) as session:
                ingestion = next(
                    (
                        task for task in session.scalars(
                            select(BackgroundTask).where(BackgroundTask.task_type == "documents.ingest")
                        ).all()
                        if (task.payload or {}).get("document_id") == document_id
                    ),
                    None,
                )
                if ingestion is None:
                    raise RuntimeError("ingestion_task_missing")
                ingestion.max_attempts = 1
                session.commit()
            ingestion_task = run_one_task("m3-ocr-acceptance-ingest")
            if ingestion_task["status"] != "succeeded":
                raise RuntimeError(f"ingest_failed:{ingestion_task.get('error_code', ingestion_task.get('error_type', 'unknown'))}")

            page_response = client.get(f"/api/v1/documents/{document_id}/pages/1")
            preview_response = client.get(f"/api/v1/documents/{document_id}/pages/1/preview")
            if page_response.status_code != 200 or preview_response.status_code != 200:
                raise RuntimeError("source_page_read_failed")
            page_data = page_response.json()
            with Session(app_engine) as session:
                queued = client.post(
                    f"/api/v1/documents/{document_id}/questions",
                    json={"question": "图1下方的中文图注写了什么？"},
                )
                if queued.status_code != 202:
                    raise RuntimeError(f"question_http_{queued.status_code}")
                question_task_id = queued.json()["task_id"]
                question_task = session.get(BackgroundTask, question_task_id)
                if question_task is None:
                    raise RuntimeError("question_task_missing")
                question_task.max_attempts = 1
                session.commit()

            answer_task = run_one_task("m3-ocr-acceptance-answer")
            task_response = client.get(f"/api/v1/tasks/{question_task_id}")
            if task_response.status_code != 200:
                raise RuntimeError("answer_task_read_failed")
            task_data = task_response.json()
            answer_result = task_data.get("result") or {}
            citations = answer_result.get("citations", [])

            with Session(app_engine) as session:
                document = session.get(Document, UUID(document_id))
                pages = session.scalars(select(DocumentPage).where(DocumentPage.document_id == document_id)).all()
                units = session.scalars(select(ContentUnit).where(ContentUnit.document_id == document_id)).all()
                calls = session.scalars(select(ProviderCall).where(ProviderCall.document_id == document_id).order_by(ProviderCall.created_at)).all()
                tasks = session.scalars(select(BackgroundTask).order_by(BackgroundTask.created_at)).all()

            call_summary = [
                {
                    "purpose": row.purpose,
                    "model": row.model,
                    "status": row.status,
                    "prompt_tokens": row.prompt_tokens,
                    "estimated_prompt_tokens": row.estimated_prompt_tokens,
                    "completion_tokens": row.completion_tokens,
                    "estimated_cost_cny": float(row.estimated_cost_cny) if row.estimated_cost_cny is not None else None,
                    "error_code": row.error_code,
                }
                for row in calls
            ]
            result = {
                "status": "completed",
                "test_database_isolated": True,
                "document_id": document_id,
                "upload_outcome_status": item["status"],
                "upload_summary_status": item["document"]["status"],
                "final_document_status": document.status if document is not None else "missing",
                "page_count": len(pages),
                "ocr_pages": sum(1 for page in pages if page.ocr_used),
                "extraction_methods": sorted({page.extraction_method for page in pages}),
                "retrieved_unit_count": len(units),
                "unit_sources": sorted({(unit.extra or {}).get("source", "") for unit in units}),
                "derived_unit_count": sum(1 for unit in units if (unit.extra or {}).get("is_derived")),
                "embedded_unit_count": sum(1 for unit in units if unit.embedding is not None),
                "ingestion_task": ingestion_task,
                "answer_task": answer_task,
                "answer": answer_result.get("answer"),
                "citations": citations,
                "retrieval": answer_result.get("retrieval"),
                "page_read_is_derived": page_data.get("is_derived"),
                "page_preview_http_status": preview_response.status_code,
                "page_preview_content_type": preview_response.headers.get("content-type"),
                "page_preview_png_signature_valid": preview_response.content.startswith(b"\x89PNG\r\n\x1a\n"),
                "page_preview_bytes": len(preview_response.content),
                "provider_calls": call_summary,
                "provider_call_count": len(calls),
                "total_reported_prompt_tokens": sum(row.prompt_tokens or 0 for row in calls),
                "total_estimated_prompt_tokens": sum(row.estimated_prompt_tokens or 0 for row in calls),
                "total_completion_tokens": sum(row.completion_tokens or 0 for row in calls),
                "estimated_cost_cny": round(sum(float(row.estimated_cost_cny or 0) for row in calls), 8),
                "task_attempts": [{"type": row.task_type, "status": row.status, "attempt_count": row.attempt_count, "max_attempts": row.max_attempts} for row in tasks],
            }
            if result["ocr_pages"] != 1 or result["derived_unit_count"] != len(units) or result["embedded_unit_count"] != len(units):
                raise RuntimeError("OCR-derived page was not fully indexed")
            if result["final_document_status"] != "ready":
                raise RuntimeError("OCR-derived document did not reach ready status")
            if answer_task["status"] != "succeeded" or not citations:
                raise RuntimeError("OCR evidence did not produce a cited answer")
            if not any(
                citation.get("page_number") == 1
                and citation.get("quote") == ""
                and citation.get("is_derived") is True
                for citation in citations
            ):
                raise RuntimeError("OCR citation did not use page-only derived semantics")
            if not result["page_preview_png_signature_valid"]:
                raise RuntimeError("original page preview was not a PNG")

        result["status"] = "completed"
    finally:
        if app_engine is not None:
            app_engine.dispose()
        admin_engine.dispose()
        cleanup_engine = create_engine(base_url.set(database="postgres"), poolclass=NullPool)
        with cleanup_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        cleanup_engine.dispose()
        shutil.rmtree(data_dir, ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}, ensure_ascii=False))
        raise SystemExit(1)
