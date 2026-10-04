import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.artifacts import api as artifact_routes
from app.artifacts.models import Artifact, ArtifactVersion
from app.artifacts.schemas import ArtifactFromRunCreate
from app.documents.models import Document, DocumentPage, DocumentVersion, IndexGeneration
from app.main import app
from app.memory import api as memory_routes
from app.memory.models import MemoryItem
from app.runs.models import Evidence, Message, Run
from app.topics.access import list_scope_memories
from app.topics.models import Conversation, Topic, TopicDocument
from app.topics.scope import ResearchScope, ScopedDocument


def test_memory_confirmation_and_artifact_version_lifecycle(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL memory and artifact API test")

    from sqlalchemy import create_engine

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(memory_routes, "get_engine", lambda: engine)
    monkeypatch.setattr(artifact_routes, "get_engine", lambda: engine)
    topic_id, conversation_id = uuid4(), uuid4()
    document_id, version_id, run_id, cancelled_run_id = uuid4(), uuid4(), uuid4(), uuid4()
    artifact_id = None
    artifact_ids: list[str] = []
    evidence_id = uuid4()
    memory_ids: list[str] = []
    client = TestClient(app)
    body = "A carefully measured result supports the method."

    try:
        with Session(engine) as session:
            generation_id = session.scalar(select(IndexGeneration.id).where(IndexGeneration.status == "active"))
            assert generation_id is not None
            topic = Topic(id=topic_id, title="Lifecycle test")
            document = Document(
                id=document_id, title="Evidence paper", filename="evidence.pdf",
                storage_key=uuid4().hex, sha256=uuid4().hex + uuid4().hex,
                status="ready", page_count=1, active_version_id=None, is_deleted=False,
            )
            version = DocumentVersion(
                id=version_id, document_id=document_id, version_number=1, filename="evidence.pdf",
                storage_key=uuid4().hex, sha256=uuid4().hex + uuid4().hex,
                status="ready", page_count=1, processing_config_hash="e" * 64,
            )
            session.add_all([topic, document, version])
            session.flush()
            document.active_version_id = version_id
            session.add_all([
                DocumentPage(document_id=document_id, version_id=version_id, page_number=1,
                             text=body, text_char_count=len(body), extraction_method="pdf_text"),
                TopicDocument(topic_id=topic_id, document_id=document_id),
                Conversation(id=conversation_id, topic_id=topic_id, title="Lifecycle test"),
            ])
            session.flush()
            scope = ResearchScope.create("topic", topic_id, [ScopedDocument(document_id, version_id)], generation_id)
            run = Run(
                id=run_id, conversation_id=conversation_id, status="completed", scope_mode="topic",
                scope_snapshot=scope.to_snapshot(), scope_hash=scope.scope_hash,
                budget_snapshot={"max_tool_calls": 12}, idempotency_key=uuid4().hex,
            )
            session.add(run)
            session.flush()
            user_message = Message(run_id=run_id, sequence_number=1, role="user", content="Summarize the result.")
            answer = Message(run_id=run_id, sequence_number=2, role="assistant", content="The study reports a measured result.", metadata_json={})
            session.add_all([user_message, answer])
            session.flush()
            evidence = Evidence(
                id=evidence_id,
                run_id=run_id, message_id=answer.id, document_id=document_id, version_id=version_id,
                page_number=1, content_type="text", source="pdf_text", is_derived=False,
                quote_eligible=True, content_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                excerpt=body, read_at=datetime.now(timezone.utc), validated_at=datetime.now(timezone.utc),
            )
            session.add(evidence)
            session.flush()
            answer.metadata_json = {"citations": [{"evidence_id": str(evidence.id), "document_id": str(document_id), "version_id": str(version_id), "page_number": 1}]}
            session.commit()

        preference = client.post(f"/api/v1/topics/{topic_id}/memories", json={
            "memory_type": "preference", "content": "Prefer concise comparisons.",
        })
        assert preference.status_code == 201, preference.text
        preference_id = preference.json()["id"]
        memory_ids.append(preference_id)
        assert preference.json()["status"] == "active"
        edited_preference = client.patch(f"/api/v1/memories/{preference_id}", json={
            "expected_revision": 1, "action": "edit", "content": "Prefer concise, sourced comparisons.",
        })
        assert edited_preference.status_code == 200, edited_preference.text
        assert edited_preference.json()["revision"] == 2
        assert client.patch(f"/api/v1/memories/{preference_id}", json={
            "expected_revision": 1, "action": "archive",
        }).status_code == 409

        conclusion = client.post(f"/api/v1/topics/{topic_id}/memories", json={
            "memory_type": "research_conclusion", "content": "The measured result supports the method.",
                "source_run_id": str(run_id), "evidence_ids": [str(evidence_id)],
        })
        assert conclusion.status_code == 201, conclusion.text
        conclusion_id = conclusion.json()["id"]
        memory_ids.append(conclusion_id)
        assert conclusion.json()["status"] == "pending"
        with Session(engine) as session:
            assert all(item.memory_type != "research_conclusion" for item in list_scope_memories(session, scope))
        confirmed = client.patch(f"/api/v1/memories/{conclusion_id}", json={"expected_revision": 1, "action": "confirm"})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "confirmed"
        with Session(engine) as session:
            assert sum(item.memory_type == "research_conclusion" for item in list_scope_memories(session, scope)) == 1

        save_barrier = Barrier(2)

        def save_answer_after_barrier():
            save_barrier.wait(timeout=10)
            return artifact_routes.create_artifact_from_run(topic_id, ArtifactFromRunCreate(
                title="Saved answer", artifact_type="note", run_id=run_id,
            ))

        with ThreadPoolExecutor(max_workers=2) as pool:
            concurrent_saves = list(pool.map(lambda _: save_answer_after_barrier(), range(2)))
        run_saved_id = str(concurrent_saves[0].id)
        assert str(concurrent_saves[1].id) == run_saved_id
        artifact_ids.append(run_saved_id)
        assert concurrent_saves[0].current_version.origin == "generated"
        assert concurrent_saves[0].current_version.evidence_snapshot
        run_saved_replay = client.post(f"/api/v1/topics/{topic_id}/artifacts/from-run", json={
            "title": "A duplicate title is ignored", "artifact_type": "draft", "run_id": str(run_id),
        })
        assert run_saved_replay.status_code == 201
        assert run_saved_replay.json()["id"] == run_saved_id

        saved = client.post(f"/api/v1/topics/{topic_id}/artifacts", json={
            "title": "Reading note", "artifact_type": "note", "body_text": "Initial note.",
        })
        assert saved.status_code == 201, saved.text
        artifact_id = saved.json()["id"]
        artifact_ids.append(artifact_id)
        first_version = saved.json()["current_version_id"]
        updated = client.patch(f"/api/v1/artifacts/{artifact_id}", json={
            "expected_revision": 1, "body_text": "Edited note.",
        })
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision"] == 2
        second_version = updated.json()["current_version_id"]

        candidate = client.post(f"/api/v1/artifacts/{artifact_id}/candidates", json={"run_id": str(run_id)})
        assert candidate.status_code == 201, candidate.text
        candidate_id = candidate.json()["id"]
        assert candidate.json()["status"] == "candidate"
        replay = client.post(f"/api/v1/artifacts/{artifact_id}/candidates", json={"run_id": str(run_id)})
        assert replay.status_code == 201 and replay.json()["id"] == candidate_id
        comparison = client.get(
            f"/api/v1/artifacts/{artifact_id}/compare",
            params={"base_version_id": second_version, "compare_version_id": candidate_id},
        )
        assert comparison.status_code == 200, comparison.text
        assert "Edited note." in comparison.json()["unified_diff"]
        assert "The study reports a measured result." in comparison.json()["unified_diff"]
        adopted = client.post(f"/api/v1/artifacts/{artifact_id}/adopt", json={
            "expected_revision": 2, "version_id": candidate_id,
        })
        assert adopted.status_code == 200, adopted.text
        assert adopted.json()["current_version_id"] == candidate_id
        assert adopted.json()["revision"] == 3
        assert client.patch(f"/api/v1/artifacts/{artifact_id}", json={
            "expected_revision": 2, "body_text": "A stale edit.",
        }).status_code == 409
        versions = client.get(f"/api/v1/artifacts/{artifact_id}/versions")
        assert versions.status_code == 200 and len(versions.json()["items"]) == 3
        assert first_version != second_version

        stale_candidate = client.post(f"/api/v1/topics/{topic_id}/memories", json={
            "memory_type": "research_conclusion", "content": "A conclusion whose source will change.",
            "source_run_id": str(run_id), "evidence_ids": [str(evidence_id)],
        })
        assert stale_candidate.status_code == 201, stale_candidate.text
        stale_memory_id = stale_candidate.json()["id"]
        memory_ids.append(stale_memory_id)
        new_version_id = uuid4()
        with Session(engine) as session:
            session.add(DocumentVersion(
                id=new_version_id, document_id=document_id, version_number=2, filename="evidence-v2.pdf",
                storage_key=uuid4().hex, sha256=uuid4().hex + uuid4().hex,
                status="ready", page_count=1, processing_config_hash="f" * 64,
            ))
            document = session.get(Document, document_id)
            document.active_version_id = new_version_id
            session.commit()
        stale_confirmation = client.patch(f"/api/v1/memories/{stale_memory_id}", json={
            "expected_revision": 1, "action": "confirm",
        })
        assert stale_confirmation.status_code == 409
        with Session(engine) as session:
            invalidated = session.get(MemoryItem, UUID(stale_memory_id))
            assert invalidated.status == "invalidated"
            assert invalidated.revision == 2

        with Session(engine) as session:
            session.add(Run(
                id=cancelled_run_id,
                conversation_id=conversation_id,
                status="cancelled",
                scope_mode="topic",
                scope_snapshot={},
                scope_hash="c" * 64,
                budget_snapshot={},
                idempotency_key=uuid4().hex,
            ))
            session.commit()
        cancelled_save = client.post(f"/api/v1/topics/{topic_id}/artifacts/from-run", json={
            "title": "Stopped answer", "artifact_type": "note", "run_id": str(cancelled_run_id),
        })
        assert cancelled_save.status_code == 422
        with Session(engine) as session:
            assert session.scalar(select(ArtifactVersion.id).where(ArtifactVersion.source_run_id == cancelled_run_id)) is None

    finally:
        with Session(engine) as session:
            for value in artifact_ids:
                artifact = session.get(Artifact, UUID(value))
                if artifact is not None:
                    artifact.current_version_id = None
            if artifact_ids:
                session.flush()
                session.execute(delete(ArtifactVersion).where(ArtifactVersion.artifact_id.in_([UUID(value) for value in artifact_ids])))
                session.execute(delete(Artifact).where(Artifact.id.in_([UUID(value) for value in artifact_ids])))
            if memory_ids:
                session.execute(delete(MemoryItem).where(MemoryItem.id.in_([UUID(value) for value in memory_ids])))
            session.execute(delete(Run).where(Run.id.in_([run_id, cancelled_run_id])))
            session.execute(delete(Conversation).where(Conversation.id == conversation_id))
            session.execute(delete(TopicDocument).where(TopicDocument.topic_id == topic_id))
            session.execute(delete(Topic).where(Topic.id == topic_id))
            document = session.get(Document, document_id)
            if document is not None:
                document.active_version_id = None
            session.flush()
            session.execute(delete(Document).where(Document.id == document_id))
            session.commit()
        engine.dispose()
