import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.api.v1 import topics as topic_routes
from app.documents.models import Document
from app.main import app
from app.topics.models import Conversation, Topic, TopicDocument


def test_topic_lifecycle_shared_documents_and_conversations(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL topic API test")

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(topic_routes, "get_engine", lambda: engine)
    document_a_id, document_b_id = uuid4(), uuid4()

    with Session(engine) as session:
        for document_id, name in ((document_a_id, "paper-a"), (document_b_id, "paper-b")):
            session.add(Document(
                id=document_id,
                title=name,
                filename=f"{name}.pdf",
                storage_key=uuid4().hex,
                sha256=uuid4().hex + uuid4().hex,
                status="ready",
                page_count=1,
                active_version_id=None,
                is_deleted=False,
            ))
        session.commit()

    topic_a_id = topic_b_id = conversation_id = None
    client = TestClient(app)
    try:
        created_a = client.post("/api/v1/topics", json={"title": "  Method comparison  ", "goal": "Compare evidence"})
        created_b = client.post("/api/v1/topics", json={"title": "Reading notes"})
        assert created_a.status_code == created_b.status_code == 201
        topic_a_id, topic_b_id = created_a.json()["id"], created_b.json()["id"]
        assert created_a.json()["title"] == "Method comparison"
        assert client.post("/api/v1/topics", json={"title": "   "}).status_code == 422

        first_link = client.put(f"/api/v1/topics/{topic_a_id}/documents/{document_a_id}")
        shared_link = client.put(f"/api/v1/topics/{topic_b_id}/documents/{document_a_id}")
        repeated_link = client.put(f"/api/v1/topics/{topic_a_id}/documents/{document_a_id}")
        assert first_link.status_code == shared_link.status_code == repeated_link.status_code == 200
        assert len(client.get(f"/api/v1/topics/{topic_a_id}/documents").json()["items"]) == 1

        conversation = client.post(
            f"/api/v1/topics/{topic_a_id}/conversations",
            json={"title": "First reading"},
        )
        assert conversation.status_code == 201
        conversation_id = conversation.json()["id"]
        assert client.get(f"/api/v1/topics/{topic_a_id}/conversations").json()["items"][0]["id"] == conversation_id

        updated = client.patch(f"/api/v1/topics/{topic_a_id}", json={"goal": "Find supporting and opposing results"})
        assert updated.status_code == 200
        assert updated.json()["document_count"] == 1
        assert updated.json()["conversation_count"] == 1
        assert client.patch(f"/api/v1/topics/{topic_a_id}", json={}).status_code == 422
        assert client.patch(f"/api/v1/topics/{topic_a_id}", json={"title": None}).status_code == 422

        assert client.delete(f"/api/v1/topics/{topic_a_id}/documents/{document_a_id}").status_code == 204
        with Session(engine) as session:
            assert session.get(Document, document_a_id) is not None
            assert session.get(TopicDocument, (topic_b_id, document_a_id)) is not None

        with Session(engine) as session:
            document_b = session.get(Document, document_b_id)
            assert document_b is not None
            document_b.is_deleted = True
            session.commit()
        assert client.put(f"/api/v1/topics/{topic_a_id}/documents/{document_b_id}").status_code == 404

        assert client.delete(f"/api/v1/topics/{topic_b_id}").status_code == 204
        assert all(row["id"] != topic_b_id for row in client.get("/api/v1/topics").json()["items"])
        assert any(row["id"] == topic_b_id for row in client.get("/api/v1/topics?include_archived=true").json()["items"])
        assert client.post(f"/api/v1/topics/{topic_b_id}/conversations", json={}).status_code == 409
        assert client.patch(f"/api/v1/topics/{topic_b_id}", json={"title": "Renamed"}).status_code == 409
        assert client.delete(f"/api/v1/topics/{topic_b_id}").status_code == 204
        restored = client.post(f"/api/v1/topics/{topic_b_id}/restore")
        assert restored.status_code == 200
        assert restored.json()["archived_at"] is None
        assert any(row["id"] == topic_b_id for row in client.get("/api/v1/topics").json()["items"])
    finally:
        with Session(engine) as session:
            topic_ids = [value for value in (topic_a_id, topic_b_id) if value]
            if topic_ids:
                session.execute(delete(Conversation).where(Conversation.topic_id.in_(topic_ids)))
                session.execute(delete(TopicDocument).where(TopicDocument.topic_id.in_(topic_ids)))
                session.execute(delete(Topic).where(Topic.id.in_(topic_ids)))
            session.execute(delete(Document).where(Document.id.in_([document_a_id, document_b_id])))
            session.commit()
        engine.dispose()
