import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session

from app.main import app
from app.notes import api as note_routes
from app.notes.models import Note, NoteVersion
from app.notes.content import validate_content
from app.notes.schemas import NoteCreate
from app.notes.service import NoteServiceError, create_note, update_note


DOC = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "first draft"}]}]}


def test_notes_idempotency_candidate_preview_adoption_and_permanent_delete(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL notes API tests")
    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(note_routes, "get_engine", lambda: engine)
    client = TestClient(app)
    note_ids: list[UUID] = []
    create_key = uuid4().hex
    try:
        first = client.post("/api/v1/notes", json={"title": "Ignored title", "content": DOC, "idempotency_key": create_key})
        assert first.status_code == 201, first.text
        note_ids.append(UUID(first.json()["id"]))
        assert first.json()["revision"] == 1 and first.json()["plain_text"] == "first draft"
        assert first.json()["title"] == "first draft"
        replay = client.post("/api/v1/notes", json={"title": "Ignored replay title", "content": DOC, "idempotency_key": create_key})
        assert replay.status_code == 201 and replay.json()["id"] == first.json()["id"]
        assert replay.json()["title"] == "first draft"

        candidate_content = {"type": "doc", "content": [{"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "AI suggestion"}]}]}
        candidate_key = uuid4().hex
        candidate = client.post(f"/api/v1/notes/{first.json()['id']}/candidates", json={
            "expected_revision": 1, "content": candidate_content, "idempotency_key": candidate_key,
        })
        assert candidate.status_code == 201, candidate.text
        candidate_id = candidate.json()["candidate"]["id"]
        assert candidate.json()["candidate"]["status"] == "candidate"
        candidate_replay = client.post(f"/api/v1/notes/{first.json()['id']}/candidates", json={
            "expected_revision": 1, "content": candidate_content, "idempotency_key": candidate_key,
        })
        assert candidate_replay.status_code == 201
        assert candidate_replay.json()["candidate"]["id"] == candidate_id
        assert client.get(f"/api/v1/notes/{first.json()['id']}").json()["plain_text"] == "first draft"

        edited = client.patch(f"/api/v1/notes/{first.json()['id']}", json={"expected_revision": 1, "content": {
            "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "human edit"}]}],
        }})
        assert edited.status_code == 200 and edited.json()["revision"] == 2
        conflict = client.post(f"/api/v1/notes/{first.json()['id']}/adopt", json={"expected_revision": 1, "version_id": candidate_id})
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["current_revision"] == 2
        assert client.get(f"/api/v1/notes/{first.json()['id']}").json()["plain_text"] == "human edit"
        invalid_run = client.post(f"/api/v1/notes/{first.json()['id']}/candidates", json={
            "expected_revision": 2, "content": candidate_content, "source_run_id": str(uuid4()),
        })
        assert invalid_run.status_code == 422
        fresh_candidate = client.post(f"/api/v1/notes/{first.json()['id']}/candidates", json={
            "expected_revision": 2, "content": candidate_content,
        })
        assert fresh_candidate.status_code == 201, fresh_candidate.text
        adopted = client.post(f"/api/v1/notes/{first.json()['id']}/adopt", json={
            "expected_revision": 2, "version_id": fresh_candidate.json()["candidate"]["id"],
        })
        assert adopted.status_code == 200, adopted.text
        assert adopted.json()["revision"] == 3 and adopted.json()["plain_text"] == "AI suggestion"
        versions = client.get(f"/api/v1/notes/{first.json()['id']}/versions")
        assert versions.status_code == 200
        assert len(versions.json()["items"]) == 4
        assert any(item["plain_text"] == "human edit" and item["status"] == "superseded" for item in versions.json()["items"])
        assert client.get(f"/api/v1/notes/{first.json()['id']}/export").text.startswith("# AI suggestion")
        deleted = client.delete(f"/api/v1/notes/{first.json()['id']}")
        assert deleted.status_code == 204
        assert client.get(f"/api/v1/notes/{first.json()['id']}").status_code == 404
        with Session(engine) as session:
            assert session.scalar(select(NoteVersion.id).where(NoteVersion.note_id == UUID(first.json()["id"]))) is None
        assert client.get("/api/v1/notes").json()["items"] == []
    finally:
        with Session(engine) as session, session.begin():
            if note_ids:
                # The owner FK is cyclic; remove the pointer before deleting either side.
                session.execute(text("UPDATE notes SET current_version_id = NULL WHERE id = ANY(:ids)"), {"ids": list(note_ids)})
                session.execute(text("UPDATE note_versions SET parent_version_id = NULL WHERE note_id = ANY(:ids)"), {"ids": list(note_ids)})
                session.execute(delete(NoteVersion).where(NoteVersion.note_id.in_(note_ids)))
                session.execute(delete(Note).where(Note.id.in_(note_ids)))
        engine.dispose()


def test_note_content_schema_rejects_active_html_and_unbounded_documents() -> None:
    client = TestClient(app)
    invalid_contents = [
        {"type": "doc", "content": [{"type": "html", "attrs": {"html": "<script>alert(1)</script>"}}]},
        {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": [{"type": "link", "attrs": {"href": "javascript:alert(1)"}}]}]}]},
        {"type": "doc", "content": [{"type": "heading", "attrs": {"level": 9}}]},
        {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": [{"type": "link", "attrs": {"href": "https://trusted.example/\n(javascript:alert(1))"}}]}]}]},
        {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": [{"type": "link", "attrs": {"href": "https://"}}]}]}]},
        {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x" * 200_001}]}]},
    ]
    for content in invalid_contents:
        response = client.post("/api/v1/notes", json={"title": "Invalid", "content": content})
        assert response.status_code == 422

    parsed = NoteCreate.model_validate({"title": "   ", "content": DOC})
    assert parsed.title.strip() == ""


def test_note_links_allow_only_safe_internal_document_routes() -> None:
    document_id = uuid4()
    version_id = uuid4()
    valid = {"type": "doc", "content": [{"type": "paragraph", "content": [{
        "type": "text", "text": "open paper", "marks": [{"type": "link", "attrs": {
            "href": f"/documents/{document_id}?version={version_id}&page=3",
        }}],
    }]}]}
    normalized = validate_content(valid)
    assert normalized["content"][0]["content"][0]["marks"][0]["attrs"]["href"] == valid["content"][0]["content"][0]["marks"][0]["attrs"]["href"]
    epub_href = f"/documents/{document_id}?version={version_id}&chapter=OPS%2FText%2Fch1.xhtml&cfi=epubcfi%28%2F6%2F2%5Bch1%5D%29"
    epub_content = {"type": "doc", "content": [{"type": "paragraph", "content": [{
        "type": "text", "text": "open EPUB", "marks": [{"type": "link", "attrs": {"href": epub_href}}],
    }]}]}
    assert validate_content(epub_content)["content"][0]["content"][0]["marks"][0]["attrs"]["href"] == epub_href
    anchor_href = f"/documents/{document_id}?version={version_id}&anchor=p-3"
    anchor_content = {"type": "doc", "content": [{"type": "paragraph", "content": [{
        "type": "text", "text": "open text", "marks": [{"type": "link", "attrs": {"href": anchor_href}}],
    }]}]}
    assert validate_content(anchor_content)["content"][0]["content"][0]["marks"][0]["attrs"]["href"] == anchor_href

    unsafe_hrefs = [
        "//example.com/documents/" + str(document_id),
        f"/admin/{document_id}",
        f"/documents/{document_id}/extra",
        f"/documents/{document_id}?version={version_id}&page=3&redirect=/admin",
        f"/documents/{document_id}?page=0",
        f"/documents/{document_id}?version={version_id}&anchor=p-0",
        f"/documents/{document_id}?version={version_id}&page=3&anchor=p-3",
        f"/documents/{document_id}?version={version_id}&chapter=../secret.xhtml",
        f"/documents/{document_id}?version={version_id}&chapter=https://evil.example/x.xhtml",
        f"/documents/{document_id}?version={version_id}&chapter=OPS%2FText%0Ach1.xhtml",
        f"/documents/{document_id}?version={version_id}&cfi=not-a-cfi",
        f"/documents/{document_id}?version=not-a-uuid",
        f"/documents/{document_id}?version=urn:uuid:{version_id}",
        f"/documents/{document_id}?page=1&page=2",
    ]
    for href in unsafe_hrefs:
        invalid = {"type": "doc", "content": [{"type": "paragraph", "content": [{
            "type": "text", "text": "link", "marks": [{"type": "link", "attrs": {"href": href}}],
        }]}]}
        with pytest.raises(ValueError):
            validate_content(invalid)


def test_note_text_style_mark_has_finite_font_palette() -> None:
    text_style = {"type": "doc", "content": [{"type": "paragraph", "content": [{
        "type": "text", "text": "styled", "marks": [{"type": "textStyle", "attrs": {
            "fontFamily": "Noto Sans SC", "fontSize": "22px", "color": "#416c5a",
        }}],
    }]}]}
    assert validate_content(text_style)["content"][0]["content"][0]["marks"][0]["attrs"]["fontFamily"] == "Noto Sans SC"
    # Tiptap emits null defaults for extensions that are not active on the mark.
    tiptap_defaulted_style = {"type": "doc", "content": [{"type": "paragraph", "content": [{
        "type": "text", "text": "styled", "marks": [{"type": "textStyle", "attrs": {
            "fontFamily": "Georgia", "fontSize": None, "color": None,
        }}],
    }]}]}
    parsed = NoteCreate.model_validate({"title": "Georgia draft", "content": tiptap_defaulted_style})
    assert parsed.content["content"][0]["content"][0]["marks"][0]["attrs"] == {"fontFamily": "Georgia"}
    tiptap_reparsed_style = {"type": "doc", "content": [{"type": "paragraph", "content": [{
        "type": "text", "text": "styled", "marks": [{"type": "textStyle", "attrs": {
            "fontFamily": "Georgia", "fontSize": "", "color": "",
        }}],
    }]}]}
    reparsed = NoteCreate.model_validate({"title": "Georgia draft", "content": tiptap_reparsed_style})
    assert reparsed.content["content"][0]["content"][0]["marks"][0]["attrs"] == {"fontFamily": "Georgia"}
    for attrs in (
        {"fontFamily": "Comic Sans"},
        {"fontSize": "999px"},
        {"color": "red"},
        {"fontFamily": "Arial", "style": "position:fixed"},
        {"fontFamily": "Georgia", "unknown": None},
        {"fontFamily": "Georgia", "unknown": ""},
    ):
        invalid = {"type": "doc", "content": [{"type": "paragraph", "content": [{
            "type": "text", "text": "styled", "marks": [{"type": "textStyle", "attrs": attrs}],
        }]}]}
        with pytest.raises(ValueError):
            validate_content(invalid)


def test_note_title_uses_first_body_line_and_blank_content_has_fallback() -> None:
    client = TestClient(app)
    # Schema normalization is local and exercises both non-empty and blank title derivation inputs.
    from app.notes.service import derive_title
    assert derive_title(DOC) == "first draft"
    assert derive_title({"type": "doc", "content": [{"type": "paragraph"}]}) == "未命名笔记"


def test_tiptap_null_link_and_codeblock_defaults_are_normalized_without_accepting_unknowns() -> None:
    content = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "link", "marks": [{
            "type": "link", "attrs": {"href": "https://example.org", "title": None},
        }]}]},
        {"type": "codeBlock", "attrs": {"language": None}, "content": [{"type": "text", "text": "code"}]},
    ]}
    normalized = validate_content(content)
    assert normalized["content"][0]["content"][0]["marks"][0]["attrs"]["href"] == "https://example.org"
    assert "attrs" not in normalized["content"][1]

    for invalid in (
        {"type": "link", "attrs": {"href": "https://example.org", "title": None, "onclick": None}},
        {"type": "codeBlock", "attrs": {"language": None, "unknown": None}},
    ):
        if invalid["type"] == "link":
            doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{
                "type": "text", "text": "link", "marks": [invalid],
            }]}]}
        else:
            doc = {"type": "doc", "content": [invalid]}
        with pytest.raises(ValueError):
            validate_content(doc)


def test_note_revision_compare_and_swap_allows_only_one_concurrent_writer(monkeypatch) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL notes concurrency test")
    engine = create_engine(database_url, pool_pre_ping=True)
    note_id: UUID | None = None
    try:
        with Session(engine) as session, session.begin():
            note, _ = create_note(session, title="Concurrent CAS", content=DOC)
            note_id = note.id

        barrier = Barrier(2)
        def writer(text: str) -> str:
            barrier.wait(timeout=10)
            try:
                with Session(engine) as session, session.begin():
                    update_note(session, note_id, expected_revision=1, content={
                        "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
                    })
                return "saved"
            except NoteServiceError as exc:
                assert exc.code == "revision_conflict"
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(writer, ["writer A", "writer B"]))
        assert sorted(outcomes) == ["conflict", "saved"]
        with Session(engine) as session:
            note = session.get(Note, note_id)
            assert note.revision == 2
            versions = session.scalars(select(NoteVersion).where(NoteVersion.note_id == note_id)).all()
            assert len(versions) == 2
    finally:
        if note_id:
            with Session(engine) as session, session.begin():
                session.execute(text("UPDATE notes SET current_version_id = NULL WHERE id = :id"), {"id": note_id})
                session.execute(text("UPDATE note_versions SET parent_version_id = NULL WHERE note_id = :id"), {"id": note_id})
                session.execute(delete(NoteVersion).where(NoteVersion.note_id == note_id))
                session.execute(delete(Note).where(Note.id == note_id))
        engine.dispose()
