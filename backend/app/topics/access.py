from dataclasses import dataclass
from datetime import datetime
import hashlib
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.documents.models import ContentUnit, Document, DocumentPage, DocumentVersion
from app.memory.models import MemoryItem
from app.topics.scope import (
    ResearchScope,
    ScopeError,
    ScopeForbidden,
    ScopeStale,
    validate_scope_current,
)


@dataclass(frozen=True)
class ScopedDocumentInfo:
    document_id: UUID
    version_id: UUID
    title: str
    filename: str
    page_count: int


@dataclass(frozen=True)
class ScopedPdfInfo:
    document_id: UUID
    version_id: UUID
    filename: str
    storage_key: str
    page_count: int


@dataclass(frozen=True)
class ScopedContentUnit:
    id: UUID
    text: str
    content_type: str
    source: str
    is_derived: bool
    quote_eligible: bool
    bbox: list[float] | None
    locator: dict
    location_label: str


@dataclass(frozen=True)
class ScopedPage:
    document_id: UUID
    version_id: UUID
    page_number: int
    text: str
    extraction_method: str
    is_derived: bool
    content_units: tuple[ScopedContentUnit, ...]
    locator: dict
    location_label: str


@dataclass(frozen=True)
class ScopedReference:
    document_id: UUID
    version_id: UUID
    page_number: int
    content_unit_id: UUID | None
    content_type: str
    source: str
    is_derived: bool
    quote_eligible: bool
    content_sha256: str
    locator: dict
    location_label: str


@dataclass(frozen=True)
class ScopedMemoryContext:
    id: UUID
    memory_type: str
    content: str
    confirmed_at: datetime | None
    evidence_snapshot: tuple[dict, ...]
    is_evidence: bool = False


def list_scope_documents(session: Session, scope: ResearchScope) -> tuple[ScopedDocumentInfo, ...]:
    validate_scope_current(session, scope)
    rows = session.execute(
        select(Document, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.id == Document.active_version_id)
        .where(Document.id.in_([item.document_id for item in scope.documents]))
        .order_by(Document.id)
    ).all()
    if len(rows) != len(scope.documents):
        raise ScopeStale("scope_document_unavailable")
    expected_versions = {item.document_id: item.version_id for item in scope.documents}
    results = tuple(
        ScopedDocumentInfo(
            document_id=document.id,
            version_id=version.id,
            title=document.title,
            filename=version.filename,
            page_count=version.page_count,
        )
        for document, version in rows
        if expected_versions.get(document.id) == version.id
    )
    if len(results) != len(scope.documents):
        raise ScopeStale("scope_version_unavailable")
    return results


def scoped_pdf_info(session: Session, scope: ResearchScope, document_id: UUID) -> ScopedPdfInfo:
    scoped_document = scope.require_document(document_id)
    validate_scope_current(session, scope)
    version = session.get(DocumentVersion, scoped_document.version_id)
    if version is None or version.document_id != document_id or version.status != "ready":
        raise ScopeStale("scope_version_unavailable")
    return ScopedPdfInfo(
        document_id=document_id,
        version_id=version.id,
        filename=version.filename,
        storage_key=version.storage_key,
        page_count=version.page_count,
    )


def read_scope_page(
    session: Session,
    scope: ResearchScope,
    document_id: UUID,
    page_number: int,
) -> ScopedPage:
    scoped_document = scope.require_document(document_id)
    if page_number < 1:
        raise ScopeForbidden("page_number_invalid")
    validate_scope_current(session, scope)
    page = session.scalar(
        select(DocumentPage).where(
            DocumentPage.document_id == document_id,
            DocumentPage.version_id == scoped_document.version_id,
            DocumentPage.page_number == page_number,
        )
    )
    if page is None:
        raise ScopeError("scope_page_unavailable")
    units = session.scalars(
        select(ContentUnit)
        .where(
            ContentUnit.document_id == document_id,
            ContentUnit.version_id == scoped_document.version_id,
            ContentUnit.page_number == page_number,
        )
        .order_by(ContentUnit.ordinal)
    ).all()
    return ScopedPage(
        document_id=document_id,
        version_id=scoped_document.version_id,
        page_number=page_number,
        text=page.text,
        extraction_method=page.extraction_method,
        is_derived=page.extraction_method != "pdf_text",
        content_units=tuple(_content_unit(unit) for unit in units),
        locator=page.locator or {"kind": "page", "page": page.page_number},
        location_label=page.location_label or f"第 {page.page_number} 页",
    )


def validate_scope_reference(
    session: Session,
    scope: ResearchScope,
    *,
    document_id: UUID,
    version_id: UUID,
    index_generation_id: UUID,
    page_number: int,
    content_unit_id: UUID | None = None,
    excerpt: str | None = None,
) -> ScopedReference:
    scope.require_reference(document_id, version_id, index_generation_id)
    if page_number < 1:
        raise ScopeForbidden("page_number_invalid")
    validate_scope_current(session, scope)

    if content_unit_id is not None:
        unit = session.scalar(
            select(ContentUnit).where(
                ContentUnit.id == content_unit_id,
                ContentUnit.document_id == document_id,
                ContentUnit.version_id == version_id,
                ContentUnit.page_number == page_number,
            )
        )
        if unit is None:
            raise ScopeForbidden("evidence_content_unit_outside_scope")
        extra = unit.extra or {}
        source = extra.get("source", "pdf_text")
        is_derived = bool(extra.get("is_derived", source not in {"pdf_text", "epub_text", "plain_text"}))
        quote_eligible = bool(extra.get("quote_eligible", source in {"pdf_text", "epub_text", "plain_text"})) and not is_derived
        source_text = unit.text
        content_type = unit.content_type
        content_sha256 = unit.text_sha256
        if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != content_sha256:
            raise ScopeStale("scope_content_changed")
    else:
        page = session.scalar(
            select(DocumentPage).where(
                DocumentPage.document_id == document_id,
                DocumentPage.version_id == version_id,
                DocumentPage.page_number == page_number,
            )
        )
        if page is None:
            raise ScopeForbidden("evidence_page_outside_scope")
        source = page.extraction_method
        is_derived = source != "pdf_text"
        quote_eligible = not is_derived
        source_text = page.text
        content_type = "text"
        content_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()

    if excerpt and (not quote_eligible or excerpt not in source_text):
        raise ScopeForbidden("evidence_excerpt_not_verifiable")
    location = session.scalar(select(DocumentPage).where(
        DocumentPage.document_id == document_id,
        DocumentPage.version_id == version_id,
        DocumentPage.page_number == page_number,
    ))
    if location is None:
        raise ScopeForbidden("evidence_page_outside_scope")
    return ScopedReference(
        document_id=document_id,
        version_id=version_id,
        page_number=page_number,
        content_unit_id=content_unit_id,
        content_type=content_type,
        source=source,
        is_derived=is_derived,
        quote_eligible=quote_eligible,
        content_sha256=content_sha256,
        locator=location.locator or {"kind": "page", "page": page_number},
        location_label=location.location_label or f"第 {page_number} 页",
    )


def list_scope_memories(
    session: Session,
    scope: ResearchScope,
    *,
    include_process_summaries: bool = False,
) -> tuple[ScopedMemoryContext, ...]:
    validate_scope_current(session, scope)
    memory_conditions = [
        and_(MemoryItem.memory_type == "preference", MemoryItem.status == "active"),
        and_(MemoryItem.memory_type == "research_conclusion", MemoryItem.status == "confirmed"),
    ]
    if include_process_summaries and scope.mode in {"topic", "library"}:
        memory_conditions.append(
            and_(MemoryItem.memory_type == "process_summary", MemoryItem.status == "active")
        )
    rows = session.scalars(
        select(MemoryItem)
        .where(MemoryItem.topic_id == scope.topic_id, or_(*memory_conditions))
        .order_by(MemoryItem.updated_at.desc(), MemoryItem.id)
    ).all()

    results = []
    for memory in rows:
        evidence_snapshot = memory.evidence_snapshot or []
        if memory.memory_type in {"research_conclusion", "process_summary"}:
            if not isinstance(evidence_snapshot, list) or not evidence_snapshot:
                continue
            if not _snapshot_is_in_scope(session, scope, evidence_snapshot):
                continue
        results.append(ScopedMemoryContext(
            id=memory.id,
            memory_type=memory.memory_type,
            content=memory.content,
            confirmed_at=memory.confirmed_at,
            evidence_snapshot=tuple(evidence_snapshot),
            is_evidence=False,
        ))
    return tuple(results)


def _content_unit(unit: ContentUnit) -> ScopedContentUnit:
    extra = unit.extra or {}
    source = extra.get("source", "pdf_text")
    is_derived = bool(extra.get("is_derived", source not in {"pdf_text", "epub_text", "plain_text"}))
    return ScopedContentUnit(
        id=unit.id,
        text=unit.text,
        content_type=unit.content_type,
        source=source,
        is_derived=is_derived,
        quote_eligible=bool(extra.get("quote_eligible", source in {"pdf_text", "epub_text", "plain_text"})) and not is_derived,
        bbox=extra.get("bbox"),
        locator=extra.get("locator") if isinstance(extra.get("locator"), dict) else {},
        location_label=str(extra.get("location_label", ""))[:150],
    )


def _snapshot_is_in_scope(session: Session, scope: ResearchScope, evidence_snapshot: object) -> bool:
    if not isinstance(evidence_snapshot, list):
        return False
    try:
        for item in evidence_snapshot:
            if not isinstance(item, dict):
                return False
            document_id = UUID(item["document_id"])
            version_id = UUID(item["version_id"])
            page_number = int(item["page_number"])
            scope.require_document(document_id, version_id)
            if page_number < 1 or session.scalar(
                select(DocumentPage.version_id).where(
                    DocumentPage.document_id == document_id,
                    DocumentPage.version_id == version_id,
                    DocumentPage.page_number == page_number,
                )
            ) is None:
                return False
    except (ScopeError, KeyError, TypeError, ValueError):
        return False
    return True
