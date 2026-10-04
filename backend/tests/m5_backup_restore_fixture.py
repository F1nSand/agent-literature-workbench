"""Seed or verify a tiny synthetic DB + PDF fixture for the M5 restore drill."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5

import fitz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.artifacts.models import Artifact, ArtifactVersion
from app.db.session import get_engine
from app.documents.models import ContentUnit, Document, DocumentPage, DocumentVersion, IndexGeneration
from app.runs.models import Evidence, Message, Run
from app.topics.models import Conversation, Topic, TopicDocument
from app.topics.scope import ResearchScope, ScopedDocument


NAMESPACE = UUID("35e6ae29-5f6e-4ac0-9bc3-b386abfdbaea")
IDS = {name: uuid5(NAMESPACE, name) for name in (
    "topic", "conversation", "document", "version", "unit", "run", "user-message",
    "assistant-message", "evidence", "artifact", "artifact-version",
)}
STORAGE_KEY = "m5-restore-fixture"
QUOTE = "可核验的恢复引用片段。"
ARTIFACT_BODY = "恢复演练保存的成果正文。"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pdf_bytes() -> bytes:
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), QUOTE)
    data = pdf.tobytes()
    pdf.close()
    return data


def seed(engine, data_dir: Path) -> dict:
    with Session(engine) as session:
        existing = session.scalar(select(func.count()).select_from(Document)) or 0
        if existing:
            raise SystemExit("restore drill requires an empty synthetic database")
        generation = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
        if generation is None:
            raise SystemExit("restore drill database needs the active index generation from migrations")
        generation_id = generation.id

    pdf_data = _pdf_bytes()
    pdf_hash = _sha256(pdf_data)
    pdf_path = data_dir / "documents" / f"{STORAGE_KEY}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(pdf_data)
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        topic = Topic(id=IDS["topic"], title="Restore fixture", goal="Synthetic backup test data.")
        session.add(topic)
        session.flush()
        session.add(Conversation(id=IDS["conversation"], topic_id=topic.id, title="Restore conversation"))
        document = Document(
            id=IDS["document"],
            title="Synthetic restore fixture",
            filename="restore-fixture.pdf",
            storage_key=STORAGE_KEY,
            sha256=pdf_hash,
            status="ready",
            page_count=1,
            active_version_id=None,
            is_deleted=False,
        )
        session.add(document)
        session.flush()
        session.add(DocumentVersion(
            id=IDS["version"],
            document_id=document.id,
            version_number=1,
            filename=document.filename,
            storage_key=STORAGE_KEY,
            sha256=pdf_hash,
            status="ready",
            page_count=1,
            processing_config_hash="d" * 64,
        ))
        session.flush()
        document.active_version_id = IDS["version"]
        session.add(TopicDocument(topic_id=topic.id, document_id=document.id))
        unit_text = f"本文包含{QUOTE}"
        unit_hash = _sha256(unit_text.encode("utf-8"))
        session.add(DocumentPage(
            document_id=document.id,
            version_id=IDS["version"],
            page_number=1,
            text=unit_text,
            text_char_count=len(unit_text),
            extraction_method="pdf_text",
        ))
        session.flush()
        session.add(ContentUnit(
            id=IDS["unit"],
            document_id=document.id,
            version_id=IDS["version"],
            page_number=1,
            ordinal=0,
            content_type="text",
            text=unit_text,
            text_sha256=unit_hash,
            search_text="restore fixture citation",
            token_estimate=8,
            extra={"source": "pdf_text"},
        ))

        scope = ResearchScope.create(
            "topic",
            topic.id,
            [ScopedDocument(document.id, IDS["version"])],
            generation_id,
        )
        run = Run(
            id=IDS["run"],
            conversation_id=IDS["conversation"],
            status="completed",
            scope_mode=scope.mode,
            scope_snapshot=scope.to_snapshot(),
            scope_hash=scope.scope_hash,
            budget_snapshot={"max_tool_calls": 12, "max_model_steps": 16},
            idempotency_key="m5-restore-fixture",
            started_at=now,
            finished_at=now,
        )
        session.add(run)
        session.flush()
        session.add(Message(
            id=IDS["user-message"],
            run_id=run.id,
            sequence_number=1,
            role="user",
            content="核对恢复后的引用。",
            metadata_json={},
        ))
        evidence = Evidence(
            id=IDS["evidence"],
            run_id=run.id,
            document_id=document.id,
            version_id=IDS["version"],
            content_unit_id=IDS["unit"],
            page_number=1,
            content_type="text",
            source="pdf_text",
            is_derived=False,
            quote_eligible=True,
            content_sha256=unit_hash,
            excerpt=unit_text,
            read_at=now,
            validated_at=now,
        )
        session.add(evidence)
        assistant_message = Message(
            id=IDS["assistant-message"],
            run_id=run.id,
            sequence_number=2,
            role="assistant",
            content="已核对恢复后的文献引用。",
            metadata_json={"citations": [{
                "evidence_id": str(evidence.id),
                "document_id": str(document.id),
                "page_number": 1,
                "quote": QUOTE,
            }]},
        )
        session.add(assistant_message)
        session.flush()
        evidence.message_id = assistant_message.id

        artifact = Artifact(
            id=IDS["artifact"],
            topic_id=topic.id,
            title="恢复演练成果",
            artifact_type="note",
            current_version_id=None,
            revision=1,
        )
        session.add(artifact)
        session.flush()
        session.add(ArtifactVersion(
            id=IDS["artifact-version"],
            artifact_id=artifact.id,
            version_number=1,
            parent_version_id=None,
            origin="user",
            status="current",
            body_text=ARTIFACT_BODY,
            structured_content={"kind": "restore-fixture"},
            content_sha256=_sha256(ARTIFACT_BODY.encode("utf-8")),
            source_run_id=run.id,
            evidence_snapshot=[{"evidence_id": str(evidence.id), "page_number": 1}],
        ))
        session.flush()
        artifact.current_version_id = IDS["artifact-version"]
        session.commit()

    return {
        "document_id": str(IDS["document"]),
        "version_id": str(IDS["version"]),
        "pdf_sha256": pdf_hash,
        "page_count": 1,
        "content_unit_count": 1,
        "citation_evidence_id": str(IDS["evidence"]),
        "artifact_id": str(IDS["artifact"]),
        "artifact_version_id": str(IDS["artifact-version"]),
    }


def verify(engine, data_dir: Path) -> dict:
    pdf_path = data_dir / "documents" / f"{STORAGE_KEY}.pdf"
    if not pdf_path.is_file():
        raise SystemExit("restored PDF is missing")
    pdf_hash = _sha256(pdf_path.read_bytes())
    with Session(engine) as session:
        document = session.get(Document, IDS["document"])
        version = session.get(DocumentVersion, IDS["version"])
        page = session.get(DocumentPage, (IDS["version"], 1))
        unit = session.get(ContentUnit, IDS["unit"])
        run = session.get(Run, IDS["run"])
        evidence = session.get(Evidence, IDS["evidence"])
        message = session.get(Message, IDS["assistant-message"])
        artifact = session.get(Artifact, IDS["artifact"])
        artifact_version = session.get(ArtifactVersion, IDS["artifact-version"])

        checks = {
            "pdf_hash_matches_document_and_version": bool(
                document and version and document.sha256 == version.sha256 == pdf_hash
            ),
            "active_version_restored": bool(document and document.active_version_id == IDS["version"]),
            "page_and_content_unit_restored": bool(page and unit and unit.version_id == IDS["version"]),
            "completed_run_restored": bool(run and run.status == "completed"),
            "citation_link_restored": bool(
                evidence and message and evidence.message_id == message.id
                and evidence.page_number == 1 and QUOTE in (evidence.excerpt or "")
            ),
            "artifact_current_version_restored": bool(
                artifact and artifact_version
                and artifact.current_version_id == artifact_version.id
                and artifact_version.status == "current"
                and artifact_version.body_text == ARTIFACT_BODY
            ),
        }
        if not all(checks.values()):
            raise SystemExit(f"restored database/file verification failed: {checks}")
        return {
            "document_id": str(IDS["document"]),
            "version_id": str(IDS["version"]),
            "pdf_sha256": pdf_hash,
            "citation_evidence_id": str(IDS["evidence"]),
            "artifact_id": str(IDS["artifact"]),
            "artifact_version_id": str(IDS["artifact-version"]),
            "checks": checks,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("seed", "verify"))
    args = parser.parse_args()
    data_dir = Path(os.environ.get("DATA_DIR", "/data")).expanduser().resolve()
    result = seed(get_engine(), data_dir) if args.mode == "seed" else verify(get_engine(), data_dir)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
