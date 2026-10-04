"""PostgreSQL integration coverage for the global assistant workspace APIs/tools."""
import hashlib
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.orm import Session

from app.agent import tools
from app.agent import executor
from app.agent.tools import AgentToolError, ToolContext, run_tool
from app.api.v1 import conversations as conversation_routes
from app.api.v1 import runs as run_routes
from app.documents.models import ContentUnit, Document, DocumentPage, DocumentVersion
from app.jobs.models import BackgroundTask
from app.jobs.store import TaskLease
from app.main import app
from app.notes import api as note_routes
from app.notes.models import Note, NoteVersion
from app.providers.siliconflow import ProviderBudget
from app.runs.models import Evidence, Message, Run, RunEvent, ToolCall, UIAction
from app.topics.models import Conversation
from app.topics.scope import ResearchScope


EPUB_LOCATOR = {
    "kind": "epub", "chapter": "OPS/Text/ch1.xhtml", "section": 1,
    "cfi": "epubcfi(/6/2[ch1]!/4/1:0)",
}
TEXT_LOCATOR = {"kind": "text", "anchor": "p-1", "section": 1}
PDF_LOCATOR = {"kind": "page", "page": 1}
NOTE_CONTENT = {
    "type": "doc",
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "original note"}]}],
}
PROPOSED_CONTENT = {
    "type": "doc",
    "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "中文候选第一段：请保留原始来源。"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "第二段验证 persisted tool result 可序列化。"}]},
    ],
}


def _new_document(session: Session, title: str, filename: str, locator: dict, text_value: str) -> tuple[UUID, UUID]:
    document_id, version_id = uuid4(), uuid4()
    sha = uuid4().hex + uuid4().hex
    document = Document(
        id=document_id, title=title, filename=filename, storage_key=uuid4().hex,
        sha256=sha, status="ready", page_count=1, active_version_id=None, is_deleted=False,
    )
    session.add(document)
    session.flush()
    version = DocumentVersion(
        id=version_id, document_id=document_id, version_number=1, filename=filename,
        storage_key=uuid4().hex, sha256=uuid4().hex + uuid4().hex, status="ready",
        page_count=1, processing_config_hash="d" * 64,
    )
    session.add(version)
    session.flush()
    document.active_version_id = version_id
    if locator["kind"] == "epub":
        label, source = "Chapter One", "epub_text"
    elif locator["kind"] == "text":
        label, source = "Paragraph 1", "plain_text"
    else:
        label, source = "第 1 页", "pdf_text"
    session.add(DocumentPage(
        document_id=document_id, version_id=version_id, page_number=1,
        text=text_value, locator=locator, location_label=label,
        text_char_count=len(text_value), extraction_method=source,
    ))
    session.flush()
    session.add(ContentUnit(
        document_id=document_id, version_id=version_id, page_number=1, ordinal=0,
        content_type="text", text=text_value, text_sha256=hashlib.sha256(text_value.encode()).hexdigest(),
        search_text=text_value, token_estimate=8,
        extra={"source": source, "quote_eligible": True, "locator": locator, "location_label": label},
    ))
    return document_id, version_id


def _tool_context(run_id: UUID, scope_snapshot: dict, scope_hash: str, page_context: dict | None) -> ToolContext:
    return ToolContext(
        task_id=uuid4(), run_id=run_id,
        scope=ResearchScope.from_snapshot(scope_snapshot, scope_hash),
        budget=ProviderBudget(max_requests=2, purpose_limits={"chat": 2}, max_input_tokens=2000, max_output_tokens=1000),
        page_context=page_context,
    )


def test_global_conversation_scope_context_tools_actions_and_note_cas(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run assistant workspace PostgreSQL integration test")

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(conversation_routes, "get_engine", lambda: engine)
    monkeypatch.setattr(run_routes, "get_engine", lambda: engine)
    monkeypatch.setattr(note_routes, "get_engine", lambda: engine)
    monkeypatch.setattr(tools, "get_engine", lambda: engine)
    client = TestClient(app)
    document_ids: list[UUID] = []
    version_ids: list[UUID] = []
    conversation_id: UUID | None = None
    note_id: UUID | None = None
    run_ids: list[UUID] = []
    task_ids: list[UUID] = []
    try:
        with Session(engine) as session:
            epub_id, epub_version = _new_document(
                session, "Study Alpha", "alpha.epub", EPUB_LOCATOR,
                "Chapter one native EPUB passage.",
            )
            text_id, text_version = _new_document(
                session, "Study Beta", "beta.md", TEXT_LOCATOR,
                "Native plain text paragraph evidence.",
            )
            pdf_id, pdf_version = _new_document(
                session, "PDF control", "control.pdf", PDF_LOCATOR,
                "Native PDF page passage.",
            )
            document_ids.extend([epub_id, text_id, pdf_id])
            version_ids.extend([epub_version, text_version, pdf_version])
            session.commit()

        created_conversation = client.post("/api/v1/conversations", json={"title": "Global assistant"})
        assert created_conversation.status_code == 201, created_conversation.text
        conversation_id = UUID(created_conversation.json()["id"])
        assert created_conversation.json()["topic_id"] is None
        assert any(item["id"] == str(conversation_id) for item in client.get("/api/v1/conversations").json()["items"])

        epub_context = {
            "page_type": "reader", "document_id": str(epub_id), "version_id": str(epub_version),
            "locator": EPUB_LOCATOR, "selection": "selection from chapter one",
        }
        first_response = client.post(f"/api/v1/conversations/{conversation_id}/runs", json={
            "question": "Read the EPUB in the library", "idempotency_key": "global-library-1",
            "scope": {"mode": "library"}, "page_context": epub_context,
        })
        assert first_response.status_code == 202, first_response.text
        first = first_response.json()
        first_run_id = UUID(first["id"])
        run_ids.append(first_run_id)
        assert first["scope_mode"] == "library"
        assert first["scope_snapshot"]["schema_version"] == 2
        assert "topic_id" not in first["scope_snapshot"]
        assert {row["document_id"] for row in first["scope_snapshot"]["documents"]} == {
            str(epub_id), str(text_id), str(pdf_id),
        }
        assert first["page_context"]["locator"] == EPUB_LOCATOR
        assert first["page_context"]["selection"] == "selection from chapter one"

        # No TopicDocument links exist: selected scope is a global library selection.
        second_response = client.post(f"/api/v1/conversations/{conversation_id}/runs", json={
            "question": "Use only the selected markdown", "idempotency_key": "global-selected-1",
            "scope": {"mode": "selected", "document_ids": [str(text_id)]},
            "page_context": {"page_type": "settings"},
        })
        assert second_response.status_code == 202, second_response.text
        second = second_response.json()
        second_run_id = UUID(second["id"])
        run_ids.append(second_run_id)
        assert second["scope_snapshot"]["schema_version"] == 2
        assert [row["document_id"] for row in second["scope_snapshot"]["documents"]] == [str(text_id)]
        assert second["page_context"]["page_type"] == "settings"
        home_response = client.post(f"/api/v1/conversations/{conversation_id}/runs", json={
            "question": "Home context remains explicit", "idempotency_key": "global-home-1",
            "scope": {"mode": "library"}, "page_context": {"page_type": "home"},
        })
        assert home_response.status_code == 202, home_response.text
        home_run_id = UUID(home_response.json()["id"])
        run_ids.append(home_run_id)
        assert home_response.json()["page_context"]["page_type"] == "home"
        assert client.post(f"/api/v1/conversations/{conversation_id}/runs", json={
            "question": "Outside current selected scope", "idempotency_key": "out-of-scope-context",
            "scope": {"mode": "selected", "document_ids": [str(text_id)]},
            "page_context": {"page_type": "reader", "document_id": str(epub_id),
                             "version_id": str(epub_version), "locator": EPUB_LOCATOR},
        }).status_code == 403
        assert client.post(f"/api/v1/conversations/{conversation_id}/runs", json={
            "question": "Too much selection", "idempotency_key": "oversized-selection",
            "scope": {"mode": "library"},
            "page_context": {"page_type": "home", "selection": "x" * 4001},
        }).status_code == 422

        with Session(engine) as session:
            frozen = session.get(Run, first_run_id)
            assert frozen is not None
            assert frozen.budget_snapshot["max_input_tokens"] == 512_000
            assert frozen.budget_snapshot["max_output_tokens"] == 128_000
            assert frozen.budget_snapshot["max_chat_output_tokens"] == 64_000
            assert frozen.budget_snapshot["max_provider_requests"] == 160
            assert frozen.page_context["locator"] == EPUB_LOCATOR
            assert frozen.page_context["page_type"] == "reader"
            assert frozen.page_context["selection"] == "selection from chapter one"
            frozen.status = "running"
            session.commit()

        current_context = _tool_context(
            first_run_id, first["scope_snapshot"], first["scope_hash"], first["page_context"],
        )
        current_page = run_tool("read_current_page", {}, current_context)
        assert current_page["text"] == "Chapter one native EPUB passage."
        assert current_page["locator"] == EPUB_LOCATOR
        assert current_page["location_label"] == "Chapter One"
        assert "page_number" not in current_page
        md_location = run_tool("read_location", {
            "document_id": str(text_id), "locator": TEXT_LOCATOR,
        }, current_context)
        assert md_location["locator"] == TEXT_LOCATOR
        assert md_location["location_label"] == "Paragraph 1"
        assert md_location["content_units"][0]["text"] == "Native plain text paragraph evidence."
        assert md_location["content_units"][0]["source"] == "plain_text"
        assert md_location["content_units"][0]["quote_eligible"] is True
        assert "page_number" not in md_location

        pdf_response = client.post(f"/api/v1/conversations/{conversation_id}/runs", json={
            "question": "Read a physical PDF page", "idempotency_key": "pdf-page-context",
            "scope": {"mode": "selected", "document_ids": [str(pdf_id)]},
            "page_context": {"page_type": "reader", "document_id": str(pdf_id),
                             "version_id": str(pdf_version), "locator": PDF_LOCATOR},
        })
        assert pdf_response.status_code == 202, pdf_response.text
        pdf_run = pdf_response.json()
        pdf_run_id = UUID(pdf_run["id"])
        run_ids.append(pdf_run_id)
        with Session(engine) as session:
            session.get(Run, pdf_run_id).status = "running"
            session.commit()
        pdf_context = _tool_context(pdf_run_id, pdf_run["scope_snapshot"], pdf_run["scope_hash"], pdf_run["page_context"])
        pdf_location = run_tool("read_location", {
            "document_id": str(pdf_id), "locator": PDF_LOCATOR,
        }, pdf_context)
        assert pdf_location["locator"] == PDF_LOCATOR
        assert pdf_location["page_number"] == 1
        assert pdf_location["content_units"][0]["text"] == "Native PDF page passage."

        # A fuzzy title match persists a stable candidate action; ACK accepts only that exact pair.
        choices = run_tool("open_document", {"query": "Study"}, current_context)
        assert choices["status"] == "ambiguous"
        assert {item["document_id"] for item in choices["candidates"]} == {str(epub_id), str(text_id)}
        action_id = choices["action_id"]
        detail = client.get(f"/api/v1/runs/{first_run_id}")
        assert detail.status_code == 200, detail.text
        assert any(item["action_id"] == action_id and item["action_type"] == "show_document_choices"
                   and item["status"] == "pending" for item in detail.json()["ui_actions"])
        assert client.post(f"/api/v1/runs/{first_run_id}/actions/{action_id}/ack", json={
            "status": "succeeded", "selected_document_id": str(uuid4()),
            "selected_version_id": str(uuid4()),
        }).status_code == 422
        selected = next(item for item in choices["candidates"] if item["document_id"] == str(text_id))
        ack_payload = {
            "status": "succeeded", "selected_document_id": selected["document_id"],
            "selected_version_id": selected["version_id"],
        }
        ack = client.post(f"/api/v1/runs/{first_run_id}/actions/{action_id}/ack", json=ack_payload)
        assert ack.status_code == 200 and ack.json()["acknowledged"] is True
        replay_ack = client.post(f"/api/v1/runs/{first_run_id}/actions/{action_id}/ack", json=ack_payload)
        assert replay_ack.status_code == 200 and replay_ack.json()["acknowledged"] is False

        # A note candidate is tied to this running run, remains a preview, and uses revision CAS on adoption.
        note_response = client.post("/api/v1/notes", json={"title": "Agent note", "content": NOTE_CONTENT})
        assert note_response.status_code == 201, note_response.text
        note_id = UUID(note_response.json()["id"])
        candidate_run_response = client.post(f"/api/v1/conversations/{conversation_id}/runs", json={
            "question": "Create a note preview", "idempotency_key": "note-candidate-run",
            "scope": {"mode": "library"},
            "page_context": {"page_type": "note", "note_id": str(note_id), "note_revision": 1},
        })
        assert candidate_run_response.status_code == 202, candidate_run_response.text
        candidate_run_id = UUID(candidate_run_response.json()["id"])
        run_ids.append(candidate_run_id)
        with Session(engine) as session:
            candidate_run = session.get(Run, candidate_run_id)
            candidate_run.status = "running"
            session.commit()
        note_context_raw = candidate_run_response.json()["page_context"]
        note_context = _tool_context(
            candidate_run_id, candidate_run_response.json()["scope_snapshot"],
            candidate_run_response.json()["scope_hash"], note_context_raw,
        )
        lease = TaskLease(uuid4(), "research.run", {"run_id": str(candidate_run_id)}, "candidate-test", 1, 1)
        note_context = ToolContext(
            task_id=note_context.task_id, run_id=note_context.run_id, scope=note_context.scope,
            budget=note_context.budget, lease=lease, page_context=note_context.page_context,
        )
        # Exercise the actual executor persistence/JSONB boundary without a live worker lease.
        monkeypatch.setattr(executor, "task_lease_is_current", lambda *args, **kwargs: True)
        monkeypatch.setattr(tools, "task_lease_is_current", lambda *args, **kwargs: True)
        candidate_result, retry = executor._execute_persisted_tool(
            candidate_run_id,
            {"id": "multilingual-note-candidate", "name": "propose_note_candidate", "arguments": {"content": PROPOSED_CONTENT}},
            note_context,
            lambda: False,
        )
        assert retry is False
        candidate_id = UUID(candidate_result["candidate_id"])
        with Session(engine) as session:
            candidate = session.get(NoteVersion, candidate_id)
            note = session.get(Note, note_id)
            assert candidate is not None and candidate.source_run_id == candidate_run_id and candidate.status == "candidate"
            assert candidate.plain_text == "中文候选第一段：请保留原始来源。\n第二段验证 persisted tool result 可序列化。"
            persisted_call = session.scalar(select(ToolCall).where(ToolCall.run_id == candidate_run_id))
            assert persisted_call is not None and persisted_call.status == "succeeded"
            assert persisted_call.result == candidate_result
            import json
            assert json.loads(json.dumps(persisted_call.result, ensure_ascii=False)) == candidate_result
            assert note is not None and note.revision == 1 and note.current_version_id != candidate_id

        edited = client.patch(f"/api/v1/notes/{note_id}", json={
            "expected_revision": 1,
            "content": {"type": "doc", "content": [{"type": "paragraph", "content":[
                {"type": "text", "text": "human edit wins"},
            ]}]},
        })
        assert edited.status_code == 200 and edited.json()["revision"] == 2
        conflict = client.post(f"/api/v1/notes/{note_id}/adopt", json={
            "expected_revision": 1, "version_id": str(candidate_id),
        })
        assert conflict.status_code == 409
        assert client.get(f"/api/v1/notes/{note_id}").json()["plain_text"] == "human edit wins"

        # Even a correctly bound note context cannot write after cancellation is requested.
        cancel_response = client.post(f"/api/v1/conversations/{conversation_id}/runs", json={
            "question": "Cancelled candidate test", "idempotency_key": "cancel-candidate-run",
            "scope": {"mode": "library"},
            "page_context": {"page_type": "note", "note_id": str(note_id), "note_revision": 2},
        })
        assert cancel_response.status_code == 202, cancel_response.text
        cancel_run_id = UUID(cancel_response.json()["id"])
        run_ids.append(cancel_run_id)
        with Session(engine) as session:
            run = session.get(Run, cancel_run_id)
            run.status = "running"
            run.cancel_requested_at = datetime.now(timezone.utc)
            session.commit()
        cancelled_context = _tool_context(
            cancel_run_id, cancel_response.json()["scope_snapshot"], cancel_response.json()["scope_hash"],
            cancel_response.json()["page_context"],
        )
        with pytest.raises(AgentToolError, match="run_cancelled"):
            run_tool("propose_note_candidate", {"content": PROPOSED_CONTENT}, cancelled_context)
        with Session(engine) as session:
            assert session.scalar(select(NoteVersion.id).where(NoteVersion.id == candidate_id)) == candidate_id
            assert session.scalar(select(NoteVersion).where(NoteVersion.source_run_id == cancel_run_id)) is None
            assert session.scalar(select(func.count()).select_from(NoteVersion).where(NoteVersion.note_id == note_id)) == 3

        # Message and run history remain attached to the topicless conversation across scope changes.
        messages = client.get(f"/api/v1/conversations/{conversation_id}/messages")
        assert messages.status_code == 200
        assert {item["content"] for item in messages.json()["messages"]} == {
            "Read the EPUB in the library", "Use only the selected markdown", "Home context remains explicit",
            "Read a physical PDF page", "Create a note preview", "Cancelled candidate test",
        }
        summaries = client.get(f"/api/v1/conversations/{conversation_id}/runs")
        assert summaries.status_code == 200
        assert {item["id"] for item in summaries.json()["items"]} >= {
            str(first_run_id), str(second_run_id), str(home_run_id), str(pdf_run_id),
            str(candidate_run_id), str(cancel_run_id),
        }
    finally:
        with Session(engine) as session:
            if run_ids:
                for task in session.scalars(select(BackgroundTask).where(BackgroundTask.task_type == "research.run")).all():
                    if task.payload.get("run_id") in {str(item) for item in run_ids}:
                        task_ids.append(task.id)
                if task_ids:
                    session.execute(delete(BackgroundTask).where(BackgroundTask.id.in_(task_ids)))
                session.execute(delete(UIAction).where(UIAction.run_id.in_(run_ids)))
                session.execute(delete(Evidence).where(Evidence.run_id.in_(run_ids)))
                session.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
                session.execute(delete(Message).where(Message.run_id.in_(run_ids)))
                session.execute(delete(Run).where(Run.id.in_(run_ids)))
            if note_id is not None:
                session.execute(text("UPDATE notes SET current_version_id = NULL WHERE id = :note_id"), {"note_id": note_id})
                session.execute(text("UPDATE note_versions SET parent_version_id = NULL WHERE note_id = :note_id"), {"note_id": note_id})
                session.execute(delete(NoteVersion).where(NoteVersion.note_id == note_id))
                session.execute(delete(Note).where(Note.id == note_id))
            if conversation_id is not None:
                session.execute(delete(Conversation).where(Conversation.id == conversation_id))
            for document in session.scalars(select(Document).where(Document.id.in_(document_ids))).all():
                document.active_version_id = None
            session.flush()
            if document_ids:
                session.execute(delete(Document).where(Document.id.in_(document_ids)))
            session.commit()
            if run_ids:
                remaining_test_tasks = session.scalars(select(BackgroundTask).where(
                    BackgroundTask.task_type == "research.run",
                    BackgroundTask.payload["run_id"].as_string().in_([str(item) for item in run_ids]),
                )).all()
                assert remaining_test_tasks == []
        engine.dispose()
