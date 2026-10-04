import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.v1 import runs as run_routes
from app.documents.models import Document, DocumentVersion, IndexGeneration
from app.jobs.models import BackgroundTask
from app.main import app
from app.runs.models import Message, Run, RunEvent
from app.topics.models import Conversation, Topic, TopicDocument


def test_run_creation_pins_scope_and_is_idempotent(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL run API test")

    from sqlalchemy import create_engine

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(run_routes, "get_engine", lambda: engine)
    topic_a_id, topic_b_id = uuid4(), uuid4()
    conversation_id = uuid4()
    doc_a_id, doc_b_id, shared_id, library_id = uuid4(), uuid4(), uuid4(), uuid4()
    document_ids = [doc_a_id, doc_b_id, shared_id, library_id]
    run_ids: list = []
    client = TestClient(app)

    try:
        with Session(engine) as session:
            assert session.scalar(select(IndexGeneration.id).where(IndexGeneration.status == "active")) is not None
            session.add_all([
                Topic(id=topic_a_id, title="Run scope A"),
                Topic(id=topic_b_id, title="Run scope B"),
            ])
            session.flush()
            session.add(Conversation(id=conversation_id, topic_id=topic_a_id, title="Run test"))
            session.flush()
            for document_id, title in zip(document_ids, ("run-a", "run-b", "shared", "library")):
                version_id = uuid4()
                document = Document(
                    id=document_id,
                    title=title,
                    filename=f"{title}.pdf",
                    storage_key=uuid4().hex,
                    sha256=uuid4().hex + uuid4().hex,
                    status="ready",
                    page_count=1,
                    active_version_id=None,
                    is_deleted=False,
                )
                session.add(document)
                session.flush()
                session.add(DocumentVersion(
                    id=version_id,
                    document_id=document_id,
                    version_number=1,
                    filename=f"{title}.pdf",
                    storage_key=uuid4().hex,
                    sha256=uuid4().hex + uuid4().hex,
                    status="ready",
                    page_count=1,
                    processing_config_hash="d" * 64,
                ))
                session.flush()
                document.active_version_id = version_id
            session.add_all([
                TopicDocument(topic_id=topic_a_id, document_id=doc_a_id),
                TopicDocument(topic_id=topic_a_id, document_id=shared_id),
                TopicDocument(topic_id=topic_b_id, document_id=doc_b_id),
                TopicDocument(topic_id=topic_b_id, document_id=shared_id),
            ])
            session.commit()

        headers = {"Idempotency-Key": "topic-run-1"}
        created = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers=headers,
            json={"question": "  Compare the main findings.  ", "scope_mode": "topic"},
        )
        assert created.status_code == 202, created.text
        topic_run = created.json()
        topic_run_id = topic_run["id"]
        run_ids.append(topic_run_id)
        assert topic_run["scope_mode"] == "topic"
        assert topic_run["scope_hash"]
        assert topic_run["status"] == "queued"
        assert topic_run["budget_snapshot"]["max_tool_calls"] == 48
        assert topic_run["budget_snapshot"]["max_model_steps"] == 64
        assert topic_run["budget_snapshot"]["max_input_tokens"] == 512_000
        assert topic_run["budget_snapshot"]["max_output_tokens"] == 128_000
        assert topic_run["budget_snapshot"]["max_chat_output_tokens"] == 64_000
        assert {row["document_id"] for row in topic_run["scope_snapshot"]["documents"]} == {
            str(doc_a_id), str(shared_id),
        }

        repeated = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers=headers,
            json={"question": "Compare the main findings.", "scope_mode": "topic"},
        )
        assert repeated.status_code == 202
        assert repeated.json()["id"] == topic_run_id
        conflict = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers=headers,
            json={"question": "A different question", "scope_mode": "topic"},
        )
        assert conflict.status_code == 409
        assert client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            json={"question": "missing key"},
        ).status_code == 422

        selected = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={"Idempotency-Key": "selected-run-1"},
            json={"question": "Read the shared paper", "scope_mode": "selected", "document_ids": [str(shared_id)]},
        )
        assert selected.status_code == 202, selected.text
        selected_run_id = selected.json()["id"]
        run_ids.append(selected_run_id)
        assert [row["document_id"] for row in selected.json()["scope_snapshot"]["documents"]] == [str(shared_id)]

        outside = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={"Idempotency-Key": "selected-outside-1"},
            json={"question": "Read another topic", "scope_mode": "selected", "document_ids": [str(doc_b_id)]},
        )
        assert outside.status_code == 403
        assert client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={"Idempotency-Key": "empty-selected-1"},
            json={"question": "No selection", "scope_mode": "selected", "document_ids": []},
        ).status_code == 422

        library = client.post(
            f"/api/v1/conversations/{conversation_id}/runs",
            headers={"Idempotency-Key": "library-run-1"},
            json={"question": "Compare across the library", "scope_mode": "library"},
        )
        assert library.status_code == 202, library.text
        library_run_id = library.json()["id"]
        run_ids.append(library_run_id)
        assert {row["document_id"] for row in library.json()["scope_snapshot"]["documents"]} == {
            str(value) for value in document_ids
        }

        with Session(engine) as session:
            assert session.scalar(select(Run).where(Run.id == topic_run_id)).scope_snapshot["mode"] == "topic"
            assert session.scalar(select(Message).where(
                Message.run_id == topic_run_id, Message.sequence_number == 1
            )).content == "Compare the main findings."
            assert session.scalar(select(RunEvent).where(
                RunEvent.run_id == topic_run_id, RunEvent.sequence_number == 1
            )).event_type == "queued"
            queued_tasks = session.scalars(select(BackgroundTask).where(
                BackgroundTask.task_type == "research.run"
            )).all()
            assert sum(task.payload.get("run_id") == topic_run_id for task in queued_tasks) == 1

        detail = client.get(f"/api/v1/runs/{topic_run_id}")
        assert detail.status_code == 200, detail.text
        assert [message["role"] for message in detail.json()["messages"]] == ["user"]
        assert detail.json()["evidence"] == []

        run_list = client.get(f"/api/v1/conversations/{conversation_id}/runs")
        assert run_list.status_code == 200, run_list.text
        assert any(item["id"] == topic_run_id and item["question"] == "Compare the main findings." for item in run_list.json()["items"])

        stopped = client.post(f"/api/v1/runs/{topic_run_id}/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["status"] == "cancelled"
        continued_before_interruption = client.post(
            f"/api/v1/runs/{topic_run_id}/continue",
            headers={"Idempotency-Key": "too-early"},
        )
        assert continued_before_interruption.status_code == 409

        with Session(engine) as session:
            run = session.get(Run, selected_run_id)
            assert run is not None
            run.status = "interrupted"
            session.commit()
        continued = client.post(
            f"/api/v1/runs/{selected_run_id}/continue",
            headers={"Idempotency-Key": "continue-selected-1"},
        )
        assert continued.status_code == 202, continued.text
        assert continued.json()["status"] == "queued"
        continued_replay = client.post(
            f"/api/v1/runs/{selected_run_id}/continue",
            headers={"Idempotency-Key": "continue-selected-1"},
        )
        assert continued_replay.status_code == 202
        assert continued_replay.json()["id"] == selected_run_id
    finally:
        with Session(engine) as session:
            stored_tasks = session.scalars(select(BackgroundTask).where(
                BackgroundTask.task_type == "research.run"
            )).all()
            for task in stored_tasks:
                if task.payload.get("run_id") in run_ids:
                    session.delete(task)
            session.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
            session.execute(delete(Run).where(Run.id.in_(run_ids)))
            session.execute(delete(Conversation).where(Conversation.id == conversation_id))
            session.execute(delete(TopicDocument).where(TopicDocument.topic_id.in_([topic_a_id, topic_b_id])))
            session.execute(delete(Topic).where(Topic.id.in_([topic_a_id, topic_b_id])))
            for document in session.scalars(select(Document).where(Document.id.in_(document_ids))).all():
                document.active_version_id = None
            session.flush()
            session.execute(delete(Document).where(Document.id.in_(document_ids)))
            session.commit()
        engine.dispose()
