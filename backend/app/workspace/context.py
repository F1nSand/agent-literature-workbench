from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.notes.models import Note
from app.topics.scope import ResearchScope, ScopeError, ScopeForbidden, validate_scope_current


class PageContextError(ScopeError):
    pass


def validate_locator_shape(locator: Any) -> dict[str, Any]:
    if not isinstance(locator, dict) or len(str(locator)) > 2000:
        raise PageContextError("page_context_locator_invalid")
    kind = locator.get("kind")
    allowed = {
        "page": {"kind", "page", "page_number", "rendition"},
        "epub": {"kind", "chapter", "section", "cfi"},
        "text": {"kind", "anchor", "section"},
    }
    if kind not in allowed or set(locator) - allowed[kind]:
        raise PageContextError("page_context_locator_invalid")
    if kind == "page":
        number = locator.get("page", locator.get("page_number"))
        if "page" in locator and "page_number" in locator:
            raise PageContextError("page_context_locator_invalid")
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 10000:
            raise PageContextError("page_context_locator_invalid")
        rendition = locator.get("rendition")
        if rendition is not None and (not isinstance(rendition, str) or not rendition or len(rendition) > 80):
            raise PageContextError("page_context_locator_invalid")
    elif kind == "epub":
        chapter, section, cfi = locator.get("chapter"), locator.get("section"), locator.get("cfi")
        if not isinstance(chapter, str) or not chapter or len(chapter) > 500:
            raise PageContextError("page_context_locator_invalid")
        if section is not None and (not isinstance(section, int) or isinstance(section, bool) or section < 1):
            raise PageContextError("page_context_locator_invalid")
        if cfi is not None and (not isinstance(cfi, str) or not cfi.startswith("epubcfi(") or not cfi.endswith(")") or len(cfi) > 2000):
            raise PageContextError("page_context_locator_invalid")
    else:
        anchor, section = locator.get("anchor"), locator.get("section")
        if not isinstance(anchor, str) or not anchor or len(anchor) > 500:
            raise PageContextError("page_context_locator_invalid")
        if section is not None and (not isinstance(section, int) or isinstance(section, bool) or section < 1):
            raise PageContextError("page_context_locator_invalid")
    return locator


def validate_page_context(session: Session, raw: dict[str, Any] | None, scope: ResearchScope) -> dict[str, Any] | None:
    """Freeze a small, server-checked reader state for one run."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {
        "page_type", "document_id", "version_id", "locator", "selection", "note_id", "note_revision"
    }:
        raise PageContextError("page_context_invalid")
    selection = raw.get("selection")
    if selection is not None and (not isinstance(selection, str) or len(selection) > 4000):
        raise PageContextError("page_context_invalid")
    page_type = raw.get("page_type")
    if page_type not in {"home", "library", "reader", "note", "assistant", "settings"}:
        raise PageContextError("page_context_invalid")
    locator = raw.get("locator")
    if locator is not None:
        validate_locator_shape(locator)
    document_id, version_id = raw.get("document_id"), raw.get("version_id")
    if (document_id is None) != (version_id is None):
        raise PageContextError("page_context_invalid")
    if page_type == "reader" and document_id is None:
        raise PageContextError("page_context_invalid")
    if document_id is not None:
        try:
            doc_uuid, version_uuid = UUID(str(document_id)), UUID(str(version_id))
            scoped = scope.require_document(doc_uuid, version_uuid)
        except ScopeForbidden:
            raise PageContextError("page_context_document_outside_scope") from None
        except (ValueError, TypeError):
            raise PageContextError("page_context_invalid") from None
        validate_scope_current(session, scope)
        if locator is not None:
            kind = locator.get("kind")
            if kind not in {"page", "epub", "text"}:
                raise PageContextError("page_context_locator_invalid")
            try:
                from app.documents.locations import validate_document_locator
                validate_document_locator(session, doc_uuid, scoped.version_id, locator)
            except ValueError:
                raise PageContextError("page_context_locator_unavailable") from None
            if kind == "epub" and (not isinstance(locator.get("chapter"), str) or len(locator["chapter"]) > 500):
                raise PageContextError("page_context_locator_invalid")
            if kind == "text" and (not isinstance(locator.get("anchor"), str) or len(locator["anchor"]) > 500):
                raise PageContextError("page_context_locator_invalid")
    note_id, note_revision = raw.get("note_id"), raw.get("note_revision")
    if (note_id is None) != (note_revision is None) or (page_type == "note" and note_id is None):
        raise PageContextError("page_context_invalid")
    if note_id is not None:
        try:
            note_uuid = UUID(str(note_id))
        except (ValueError, TypeError):
            raise PageContextError("page_context_invalid") from None
        note = session.get(Note, note_uuid)
        if note is None or (note_revision is not None and note.revision != note_revision):
            raise PageContextError("page_context_note_unavailable")
    frozen = {
        "page_type": page_type,
        "document_id": str(document_id) if document_id is not None else None,
        "version_id": str(version_id) if version_id is not None else None,
        "locator": locator,
        "selection": selection,
        "note_id": str(note_id) if note_id is not None else None,
        "note_revision": note_revision,
    }
    return frozen
