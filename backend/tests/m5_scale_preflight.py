"""Run a reproducible, provider-free scale preflight against an empty PostgreSQL database.

Example (inside the backend image):
    python tests/m5_scale_preflight.py --documents 100 --pages-per-document 20 --units-per-page 3

The script refuses a database that already contains documents, runs, tasks, or provider calls.
All generated rows are deleted before it exits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.v1 import documents as document_routes
from app.api.v1 import tasks as task_routes
from app.db.session import get_engine
from app.documents import pipeline
from app.documents.models import (
    ContentUnit,
    ContentUnitEmbedding,
    Document,
    DocumentPage,
    DocumentVersion,
    EmbeddingCache,
    IndexGeneration,
    ProviderCall,
)
from app.jobs.models import BackgroundTask
from app.jobs.store import claim_next_task, finish_task
from app.main import app
from app.runs.models import Run


VECTOR = [0.01] * 2560
SYNTHETIC_CONFIG_HASH = "c" * 64


def percentile_ms(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def count_rows(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def require_empty_database(engine) -> None:
    with Session(engine) as session:
        nonempty = {
            "documents": count_rows(session, Document),
            "runs": count_rows(session, Run),
            "tasks": count_rows(session, BackgroundTask),
            "provider_calls": count_rows(session, ProviderCall),
        }
    if any(nonempty.values()):
        raise SystemExit(f"refusing non-empty benchmark database: {nonempty}")


def seed_workload(engine, document_count: int, pages_per_document: int, units_per_page: int):
    document_ids: list = []
    version_ids: list = []
    cache_keys: list[str] = []
    task_ids: list = []
    unit_count = 0
    with Session(engine) as session:
        generation = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
        if generation is None:
            generation = IndexGeneration(
                id=uuid4(),
                provider="synthetic",
                model="scale-preflight-2560",
                dimensions=2560,
                config_hash=SYNTHETIC_CONFIG_HASH,
                status="active",
            )
            session.add(generation)
            session.flush()
        generation_id = generation.id
        model_name = generation.model

        for document_number in range(document_count):
            document_id, version_id = uuid4(), uuid4()
            label = f"m5-scale-{document_number:04d}"
            digest = hashlib.sha256(label.encode()).hexdigest()
            document = Document(
                id=document_id,
                title=f"Synthetic scale paper {document_number:04d}",
                filename=f"synthetic-{document_number:04d}.pdf",
                storage_key=uuid4().hex,
                sha256=digest,
                status="ready",
                page_count=pages_per_document,
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
                storage_key=document.storage_key,
                sha256=digest,
                status="ready",
                page_count=pages_per_document,
                processing_config_hash=SYNTHETIC_CONFIG_HASH,
            ))
            session.flush()
            document.active_version_id = version_id
            document_ids.append(document_id)
            version_ids.append(version_id)

            document_units: list[ContentUnit] = []
            document_caches: list[EmbeddingCache] = []
            document_embeddings: list[ContentUnitEmbedding] = []
            for page_number in range(1, pages_per_document + 1):
                page_text = f"Synthetic page {page_number} reports distributed retrieval performance and batch processing."
                session.add(DocumentPage(
                    document_id=document_id,
                    version_id=version_id,
                    page_number=page_number,
                    text=page_text,
                    text_char_count=len(page_text),
                    extraction_method="pdf_text",
                ))
                for ordinal in range(units_per_page):
                    unit_id = uuid4()
                    unit_text = f"{page_text} Finding unit {ordinal} for paper {document_number}."
                    unit_hash = hashlib.sha256(unit_text.encode()).hexdigest()
                    cache_key = f"synthetic-scale:{unit_hash}"
                    cache_keys.append(cache_key)
                    document_units.append(ContentUnit(
                        id=unit_id,
                        document_id=document_id,
                        version_id=version_id,
                        page_number=page_number,
                        ordinal=ordinal,
                        text=unit_text,
                        text_sha256=unit_hash,
                        search_text="synthetic page reports distributed retrieval performance batch processing finding unit paper",
                        token_estimate=16,
                        extra={"source": "pdf_text"},
                    ))
                    document_caches.append(EmbeddingCache(
                        cache_key=cache_key,
                        provider="synthetic",
                        model=model_name,
                        dimensions=2560,
                        config_hash=SYNTHETIC_CONFIG_HASH,
                        embedding=VECTOR,
                    ))
                    document_embeddings.append(ContentUnitEmbedding(
                        content_unit_id=unit_id,
                        generation_id=generation_id,
                        cache_key=cache_key,
                        embedding=VECTOR,
                    ))
                    unit_count += 1
            session.flush()
            session.add_all(document_units)
            session.add_all(document_caches)
            session.flush()
            session.add_all(document_embeddings)

        session.flush()
        session.add_all([
            BackgroundTask(
                id=(task_id := uuid4()),
                task_type="system.probe",
                status="queued",
                payload={"synthetic_scale_preflight": True},
                max_attempts=1,
            )
            for _ in range(100)
        ])
        task_ids = [row.id for row in session.new if isinstance(row, BackgroundTask)]
        session.commit()
    return document_ids, version_ids, cache_keys, task_ids, unit_count, generation_id


class FakeRetrievalClient:
    def embed(self, inputs, model=None, dimensions=None):
        assert dimensions == 2560
        return [VECTOR for _ in inputs], None

    def rerank(self, query, documents):
        return ([{"index": index, "relevance_score": float(len(documents) - index)} for index in range(len(documents))], None)


def run(args) -> dict:
    engine = get_engine()
    require_empty_database(engine)
    document_ids: list = []
    version_ids: list = []
    cache_keys: list[str] = []
    task_ids: list = []
    generation_id = None
    created_generation = False
    preexisting_cache_keys: set[str] = set()
    try:
        with Session(engine) as session:
            preexisting_cache_keys = set(session.scalars(select(EmbeddingCache.cache_key)).all())

        seed_started = time.perf_counter()
        document_ids, version_ids, cache_keys, task_ids, unit_count, generation_id = seed_workload(
            engine,
            args.documents,
            args.pages_per_document,
            args.units_per_page,
        )
        seed_seconds = round(time.perf_counter() - seed_started, 3)
        with Session(engine) as session:
            generation = session.get(IndexGeneration, generation_id)
            created_generation = generation is not None and generation.provider == "synthetic"

        monkeypatches = [
            (document_routes, "get_engine"),
            (task_routes, "get_engine"),
            (pipeline, "get_engine"),
        ]
        originals = [(module, name, getattr(module, name)) for module, name in monkeypatches]
        for module, name in monkeypatches:
            setattr(module, name, lambda engine=engine: engine)
        try:
            client = TestClient(app)
            list_latencies: list[float] = []
            for _ in range(args.samples):
                started = time.perf_counter()
                response = client.get("/api/v1/documents")
                list_latencies.append((time.perf_counter() - started) * 1000)
                assert response.status_code == 200
                assert len(response.json()["items"]) == args.documents

            queue_latencies: list[float] = []
            for _ in range(100):
                started = time.perf_counter()
                with Session(engine) as session:
                    lease = claim_next_task(session, "m5-scale-preflight", lease_seconds=60)
                assert lease is not None
                with Session(engine) as session:
                    assert finish_task(session, lease, {"ok": True})
                queue_latencies.append((time.perf_counter() - started) * 1000)

            task_status_latencies: list[float] = []
            for task_id in task_ids:
                with Session(engine) as session:
                    row = session.get(BackgroundTask, task_id)
                    assert row is not None and row.status == "succeeded"
                started = time.perf_counter()
                response = client.get(f"/api/v1/tasks/{task_id}")
                assert response.status_code == 200
                assert response.json()["status"] == "succeeded"
                task_status_latencies.append((time.perf_counter() - started) * 1000)

            retrieval_latencies: list[float] = []
            fake_client = FakeRetrievalClient()
            for _ in range(args.samples):
                started = time.perf_counter()
                hits = pipeline.retrieve_evidence(
                    document_ids[0],
                    "distributed retrieval performance batch processing",
                    fake_client,
                )
                retrieval_latencies.append((time.perf_counter() - started) * 1000)
                assert hits
        finally:
            for module, name, original in originals:
                setattr(module, name, original)

        with Session(engine) as session:
            extension = session.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
            pg_version = session.scalar(text("SHOW server_version"))
            table_sizes = {}
            for table_name in (
                "documents", "document_versions", "document_pages", "content_units",
                "content_unit_embeddings", "embedding_cache", "background_tasks",
            ):
                table_sizes[table_name] = int(session.scalar(text("SELECT pg_total_relation_size(CAST(:name AS regclass))"), {"name": table_name}) or 0)
            generated_query_keys = set(session.scalars(
                select(EmbeddingCache.cache_key).where(EmbeddingCache.cache_key.like("embedding:query:%"))
            ).all()) - preexisting_cache_keys
            provider_call_count = count_rows(session, ProviderCall)

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "classification": "synthetic infrastructure preflight; not A14 real-corpus acceptance",
            "workload": {
                "documents": args.documents,
                "pages_per_document": args.pages_per_document,
                "content_units_per_page": args.units_per_page,
                "content_units": unit_count,
                "queue_tasks": len(task_ids),
                "samples_per_http_path": args.samples,
                "concurrency": 1,
                "workers": 1,
                "provider_calls": provider_call_count,
                "seed_seconds": seed_seconds,
            },
            "measurements_ms": {
                "document_list": {"p50": percentile_ms(list_latencies, 0.50), "p95": percentile_ms(list_latencies, 0.95), "max": round(max(list_latencies), 3)},
                "task_status": {"p50": percentile_ms(task_status_latencies, 0.50), "p95": percentile_ms(task_status_latencies, 0.95), "max": round(max(task_status_latencies), 3)},
                "queue_claim_and_finish": {"p50": percentile_ms(queue_latencies, 0.50), "p95": percentile_ms(queue_latencies, 0.95), "max": round(max(queue_latencies), 3)},
                "local_hybrid_retrieval_fake_provider": {"p50": percentile_ms(retrieval_latencies, 0.50), "p95": percentile_ms(retrieval_latencies, 0.95), "max": round(max(retrieval_latencies), 3)},
            },
            "gates": {
                "document_list_p95_le_1000ms": percentile_ms(list_latencies, 0.95) <= 1000,
                "task_status_p95_le_1000ms": percentile_ms(task_status_latencies, 0.95) <= 1000,
                "local_retrieval_p95_le_2000ms": percentile_ms(retrieval_latencies, 0.95) <= 2000,
            },
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "logical_cpu_count": os.cpu_count(),
                "postgresql": pg_version,
                "pgvector": extension,
                "database_table_bytes": table_sizes,
            },
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        if not all(report["gates"].values()):
            raise SystemExit("one or more frozen local performance gates failed")
        return report
    finally:
        with Session(engine) as session:
            if task_ids:
                session.query(BackgroundTask).filter(BackgroundTask.id.in_(task_ids)).delete(synchronize_session=False)
            if document_ids:
                session.query(Document).filter(Document.id.in_(document_ids)).update(
                    {Document.active_version_id: None}, synchronize_session=False,
                )
                session.flush()
                session.query(Document).filter(Document.id.in_(document_ids)).delete(synchronize_session=False)
            cleanup_cache_keys = set(cache_keys)
            if 'generated_query_keys' in locals():
                cleanup_cache_keys.update(generated_query_keys)
            if cleanup_cache_keys:
                session.query(EmbeddingCache).filter(EmbeddingCache.cache_key.in_(cleanup_cache_keys)).delete(synchronize_session=False)
            if created_generation and generation_id is not None:
                session.query(IndexGeneration).filter(IndexGeneration.id == generation_id).delete(synchronize_session=False)
            session.commit()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=100)
    parser.add_argument("--pages-per-document", type=int, default=20)
    parser.add_argument("--units-per-page", type=int, default=3)
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()
    if min(args.documents, args.pages_per_document, args.units_per_page, args.samples) < 1:
        parser.error("all workload sizes must be positive")
    run(args)


if __name__ == "__main__":
    main()
