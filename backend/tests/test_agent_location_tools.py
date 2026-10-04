from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agent import tools
from app.agent.tools import ReadLocationArgs, ToolContext
from app.documents.locations import validate_document_locator
from app.workspace.context import PageContextError, validate_locator_shape


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _ReaderSession:
    def __init__(self, document, version, pages):
        self.document, self.version, self.pages = document, version, pages

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, model, _identity):
        return self.document if model.__name__ == "Document" else self.version

    def scalars(self, _statement):
        return _Rows(self.pages)


def test_typed_locator_shape_rejects_bool_section_and_bad_cfi() -> None:
    assert validate_locator_shape({"kind": "epub", "chapter": "OPS/ch1.xhtml", "section": 1,
                                   "cfi": "epubcfi(/6/2[chapter1]!/4/1:0)"})["section"] == 1
    assert validate_locator_shape({"kind": "text", "anchor": "p-2", "section": 2})["anchor"] == "p-2"
    with pytest.raises(PageContextError, match="page_context_locator_invalid"):
        validate_locator_shape({"kind": "text", "anchor": "p-1", "section": True})
    with pytest.raises(PageContextError, match="page_context_locator_invalid"):
        validate_locator_shape({"kind": "epub", "chapter": "OPS/ch1.xhtml", "cfi": "javascript:bad"})


@pytest.mark.parametrize(("filename", "locator", "page_number"), [
    ("paper.epub", {"kind": "epub", "chapter": "OPS/ch1.xhtml", "section": 1}, 1),
    ("paper.md", {"kind": "text", "anchor": "p-2", "section": 2}, 2),
    ("paper.pdf", {"kind": "page", "page": 2}, 2),
])
def test_reader_location_validator_uses_typed_location_and_checks_section(filename, locator, page_number) -> None:
    document_id, version_id = uuid4(), uuid4()
    doc = SimpleNamespace(id=document_id, is_deleted=False)
    version = SimpleNamespace(id=version_id, document_id=document_id, status="ready", filename=filename)
    pages = [
        SimpleNamespace(version_id=version_id, page_number=1, locator={"kind": "epub", "chapter": "OPS/ch1.xhtml", "section": 1}),
        SimpleNamespace(version_id=version_id, page_number=2, locator={"kind": "text", "anchor": "p-2", "section": 2}),
        SimpleNamespace(version_id=version_id, page_number=2, locator={"kind": "page", "page": 2}),
    ]
    page = validate_document_locator(_ReaderSession(doc, version, pages), document_id, version_id, locator)
    assert page.page_number == page_number


def test_reader_location_tool_resolves_internal_page_without_returning_it(monkeypatch) -> None:
    document_id, version_id = uuid4(), uuid4()
    page = SimpleNamespace(page_number=3, locator={"kind": "epub", "chapter": "OPS/ch3.xhtml", "section": 3})
    monkeypatch.setattr(tools, "Session", lambda _engine: _ReaderSession(None, None, []))
    monkeypatch.setattr(tools, "get_engine", lambda: None)
    monkeypatch.setattr(tools, "_assert_execution_current", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tools, "validate_document_locator", lambda *_args: page)
    observed = {}

    def read_page(args, _context, *, typed_location=False):
        observed.update(page_number=args.page_number, typed=typed_location)
        return {"locator": page.locator, "location_label": "Chapter 3"}

    monkeypatch.setattr(tools, "_read_page", read_page)
    context = SimpleNamespace(scope=SimpleNamespace(require_document=lambda *_args: SimpleNamespace(version_id=version_id)))
    result = tools._read_location(ReadLocationArgs(document_id=document_id, locator=page.locator), context)
    assert observed == {"page_number": 3, "typed": True}
    assert result == {"locator": page.locator, "location_label": "Chapter 3"}
    assert "page_number" not in result


def test_blank_read_location_uses_only_the_same_document_frozen_context(monkeypatch) -> None:
    document_id, version_id = uuid4(), uuid4()
    locator = {"kind": "epub", "chapter": "OPS/ch3.xhtml", "section": 3}
    page = SimpleNamespace(page_number=3, locator=locator)
    monkeypatch.setattr(tools, "Session", lambda _engine: _ReaderSession(None, None, []))
    monkeypatch.setattr(tools, "get_engine", lambda: None)
    monkeypatch.setattr(tools, "_assert_execution_current", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tools, "validate_document_locator", lambda _session, _doc, _version, value: page if value == locator else None)
    monkeypatch.setattr(tools, "_read_page", lambda args, _context, **_kwargs: {"page_number": args.page_number})
    context = SimpleNamespace(
        scope=SimpleNamespace(require_document=lambda *_args: SimpleNamespace(version_id=version_id)),
        page_context={"document_id": str(document_id), "version_id": str(version_id), "locator": locator},
    )
    result = tools._read_location(ReadLocationArgs(document_id=document_id, locator=""), context)
    assert result == {"page_number": 3}

    with pytest.raises(tools.AgentToolError, match="location_unavailable"):
        tools._read_location(ReadLocationArgs(document_id=uuid4(), locator=""), context)


def test_native_unit_locator_is_returned_with_evidence_and_must_match_page_location() -> None:
    locator = {"kind": "epub", "chapter": "OPS/ch1.xhtml", "section": 1, "cfi": "epubcfi(/6/2)"}
    ref = SimpleNamespace(locator=locator, location_label="Chapter One")
    assert tools._verified_unit_location(locator, "Chapter One", ref) == (locator, "Chapter One")
    with pytest.raises(tools.AgentToolError, match="evidence_locator_invalid"):
        tools._verified_unit_location({"kind": "epub", "chapter": "OPS/ch2.xhtml", "section": 2},
                                      "Chapter Two", ref)
    with pytest.raises(tools.AgentToolError, match="evidence_locator_invalid"):
        tools._verified_unit_location(locator, "Forged Chapter", ref)


def test_legacy_content_unit_without_location_uses_authoritative_page_metadata() -> None:
    ref = SimpleNamespace(locator={"kind": "page", "page": 2}, location_label="第 2 页")
    assert tools._verified_unit_location({}, "", ref) == (ref.locator, ref.location_label)
    assert tools._verified_unit_location(None, None, ref) == (ref.locator, ref.location_label)


def test_open_document_normalizes_only_unambiguous_legacy_pdf_page() -> None:
    assert tools._normalize_open_locator({"page": 2}, "paper.pdf") == {"kind": "page", "page": 2}
    assert tools._normalize_open_locator({"page_number": 2}, "paper.docx") == {"kind": "page", "page": 2}
    assert tools._normalize_open_locator({"page": 2}, "paper.epub") == {"page": 2}
    assert tools._normalize_open_locator({"page": True}, "paper.pdf") == {"page": True}


def test_tool_schemas_describe_typed_locations_and_tiptap_content() -> None:
    by_name = {item["function"]["name"]: item["function"] for item in tools.TOOL_SCHEMAS}
    open_locator = by_name["open_document"]["parameters"]["properties"]["locator"]
    assert set(open_locator["properties"]["kind"]["enum"]) == {"page", "epub", "text"}
    assert open_locator["properties"]["page"]["minimum"] == 1
    assert open_locator["properties"]["section"]["minimum"] == 1
    assert "{kind:'page',page:2}" in by_name["open_document"]["description"]
    note_schema = by_name["propose_note_candidate"]["parameters"]["properties"]["content"]
    assert note_schema["properties"]["type"]["enum"] == ["doc"]
    assert "Tiptap doc JSON" in by_name["propose_note_candidate"]["description"]
    assert "不必先读证据" in by_name["open_document"]["description"]


def test_ambiguous_open_emits_a_whitelisted_document_choices_action(monkeypatch) -> None:
    run_id, doc_a, doc_b, version_a, version_b = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    locator = {"kind": "epub", "chapter": "OPS/ch1.xhtml", "section": 1}
    candidates = [
        SimpleNamespace(document_id=doc_a, version_id=version_a, title="Study A", filename="a.epub"),
        SimpleNamespace(document_id=doc_b, version_id=version_b, title="Study B", filename="b.epub"),
    ]
    page = SimpleNamespace(locator=locator, location_label="Chapter One")
    events, stored = [], []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def scalar(self, statement):
            return None if "run_ui_actions" in str(statement) else page

        def add(self, row):
            stored.append(row)

        def commit(self):
            pass

    monkeypatch.setattr(tools, "Session", lambda _engine: FakeSession())
    monkeypatch.setattr(tools, "get_engine", lambda: None)
    monkeypatch.setattr(tools, "_assert_execution_current", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tools, "list_scope_documents", lambda *_args: tuple(candidates))
    monkeypatch.setattr(tools, "append_run_event", lambda _session, _run, _type, payload: events.append(payload))
    monkeypatch.setattr("app.workspace.context.validate_page_context", lambda _session, raw, _scope: raw)
    context = SimpleNamespace(run_id=run_id, scope=object(), page_context=None)

    result = tools._open_document(tools.OpenDocumentArgs(query="Study"), context)
    assert result["status"] == "ambiguous"
    assert [item["document_id"] for item in result["candidates"]] == [str(doc_a), str(doc_b)]
    assert all(item["locator"] == locator and item["location_label"] == "Chapter One"
               for item in result["candidates"])
    assert events[0]["type"] == "show_document_choices"
    assert events[0]["action_id"] == result["action_id"]
    assert stored[0].action_type == "show_document_choices"
