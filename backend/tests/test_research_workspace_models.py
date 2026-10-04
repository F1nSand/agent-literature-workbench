import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.artifacts.models import Artifact, ArtifactVersion
from app.db.base import Base
from app.documents.models import ContentUnit, Document, DocumentPage, DocumentVersion
from app.memory.models import MemoryItem
from app.runs.models import Evidence, Message, Run, RunEvent, ToolCall
from app.topics.models import Conversation, Topic, TopicDocument


def test_m4_domain_models_are_registered_with_alembic_metadata() -> None:
    assert {
        "topics",
        "topic_documents",
        "conversations",
        "runs",
        "messages",
        "run_events",
        "evidence",
        "tool_calls",
        "memory_items",
        "artifacts",
        "artifact_versions",
    }.issubset(Base.metadata.tables)


def test_topic_scope_and_history_rows_preserve_shared_document_links() -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL M4 model test")

    engine = create_engine(database_url, pool_pre_ping=True)
    document_id = uuid4()
    version_id = uuid4()
    unit_id = uuid4()
    topic_a_id = uuid4()
    topic_b_id = uuid4()
    run_id = uuid4()
    try:
        with Session(engine) as session:
            document = Document(
                id=document_id,
                title="shared paper",
                filename="shared-paper.pdf",
                storage_key=uuid4().hex,
                sha256=uuid4().hex + uuid4().hex,
                status="ready",
                page_count=1,
                active_version_id=None,
                is_deleted=False,
            )
            version = DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_number=1,
                filename="shared-paper.pdf",
                storage_key=uuid4().hex,
                sha256=uuid4().hex + uuid4().hex,
                status="ready",
                page_count=1,
                processing_config_hash="a" * 64,
            )
            session.add_all([document, version])
            session.flush()
            document.active_version_id = version_id
            session.add(DocumentPage(
                document_id=document_id,
                version_id=version_id,
                page_number=1,
                text="Evidence body",
                text_char_count=13,
            ))
            session.flush()
            session.add(ContentUnit(
                id=unit_id,
                document_id=document_id,
                version_id=version_id,
                page_number=1,
                ordinal=0,
                content_type="text",
                text="Evidence body",
                text_sha256="b" * 64,
                token_estimate=3,
                extra={"source": "pdf_text", "quote_eligible": True},
            ))
            topic_a = Topic(id=topic_a_id, title="Topic A", goal="Compare methods")
            topic_b = Topic(id=topic_b_id, title="Topic B", goal="Read shared paper")
            link_a = TopicDocument(topic_id=topic_a_id, document_id=document_id)
            link_b = TopicDocument(topic_id=topic_b_id, document_id=document_id)
            session.add_all([topic_a, topic_b, link_a, link_b])
            session.flush()

            conversation = Conversation(topic_id=topic_a_id, title="Reading session")
            session.add(conversation)
            session.flush()
            run = Run(
                id=run_id,
                conversation_id=conversation.id,
                status="queued",
                scope_mode="topic",
                scope_snapshot={
                    "mode": "topic",
                    "topic_id": str(topic_a_id),
                    "documents": [{"document_id": str(document_id), "version_id": str(version_id)}],
                    "index_generation_id": "frozen-generation-id",
                },
                scope_hash="c" * 64,
                budget_snapshot={"max_tool_calls": 12, "max_model_steps": 8},
                idempotency_key=uuid4().hex,
            )
            session.add(run)
            session.flush()
            user_message = Message(run_id=run_id, sequence_number=1, role="user", content="What does the paper say?")
            answer_message = Message(run_id=run_id, sequence_number=2, role="assistant", content="The cited page describes the method.")
            session.add_all([user_message, answer_message])
            session.flush()
            session.add(Evidence(
                run_id=run_id,
                message_id=answer_message.id,
                document_id=document_id,
                version_id=version_id,
                content_unit_id=unit_id,
                page_number=1,
                content_type="text",
                source="pdf_text",
                is_derived=False,
                quote_eligible=True,
                content_sha256="b" * 64,
                excerpt="Evidence body",
                read_at=func.now(),
                validated_at=func.now(),
            ))
            session.add(ToolCall(
                run_id=run_id,
                message_id=user_message.id,
                sequence_number=1,
                tool_name="retrieve_evidence",
                idempotency_key=uuid4().hex,
                status="succeeded",
                arguments={"query": "method"},
                result={"evidence_count": 1},
            ))
            session.add(RunEvent(
                run_id=run_id,
                sequence_number=1,
                event_type="run.status",
                payload={"status": "queued"},
            ))
            session.add(MemoryItem(
                topic_id=topic_a_id,
                memory_type="research_conclusion",
                status="pending",
                content="Candidate conclusion stays unconfirmed.",
                source_run_id=run_id,
                evidence_snapshot=[{"document_id": str(document_id), "version_id": str(version_id), "page_number": 1}],
            ))
            artifact = Artifact(topic_id=topic_a_id, title="Reading note", artifact_type="note", revision=1)
            session.add(artifact)
            session.flush()
            version_record = ArtifactVersion(
                artifact_id=artifact.id,
                version_number=1,
                parent_version_id=None,
                origin="user",
                status="current",
                body_text="Initial note",
                content_sha256="d" * 64,
                source_run_id=run_id,
                evidence_snapshot=[{"document_id": str(document_id), "version_id": str(version_id), "page_number": 1}],
            )
            session.add(version_record)
            session.flush()
            artifact.current_version_id = version_record.id
            session.flush()

            assert session.scalar(select(Run.scope_snapshot).where(Run.id == run_id))["documents"][0]["version_id"] == str(version_id)
            assert session.scalar(select(func.count()).select_from(Evidence).where(Evidence.run_id == run_id)) == 1
            assert session.scalar(select(func.count()).select_from(Message).where(Message.run_id == run_id)) == 2
            assert session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)) == 1
            assert session.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.run_id == run_id)) == 1
            assert session.get(Artifact, artifact.id).current_version_id == version_record.id

            session.delete(link_a)
            session.flush()
            assert session.get(Document, document_id) is not None
            assert session.get(TopicDocument, (topic_b_id, document_id)) is not None
            session.rollback()
    finally:
        engine.dispose()
