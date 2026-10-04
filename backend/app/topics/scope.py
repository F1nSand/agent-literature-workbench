from dataclasses import dataclass
import hashlib
import json
from typing import Literal, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.documents.models import Document, DocumentVersion, IndexGeneration
from app.topics.models import Topic, TopicDocument

ScopeMode = Literal["topic", "selected", "library"]


class ScopeError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ScopeForbidden(ScopeError):
    pass


class ScopeStale(ScopeError):
    pass


@dataclass(frozen=True, order=True)
class ScopedDocument:
    document_id: UUID
    version_id: UUID


@dataclass(frozen=True)
class ResearchScope:
    mode: ScopeMode
    topic_id: UUID | None
    documents: tuple[ScopedDocument, ...]
    index_generation_id: UUID
    scope_hash: str

    @classmethod
    def create(
        cls,
        mode: ScopeMode,
        topic_id: UUID,
        documents: Sequence[ScopedDocument],
        index_generation_id: UUID,
    ) -> "ResearchScope":
        normalized_documents = tuple(sorted(documents))
        if not normalized_documents:
            raise ScopeError("scope_empty")
        if len({item.document_id for item in normalized_documents}) != len(normalized_documents):
            raise ScopeError("scope_duplicate_document")
        payload = _snapshot_payload(mode, topic_id, normalized_documents, index_generation_id)
        return cls(
            mode=mode,
            topic_id=topic_id,
            documents=normalized_documents,
            index_generation_id=index_generation_id,
            scope_hash=_hash_payload(payload),
        )

    @classmethod
    def create_assistant(
        cls, mode: Literal["selected", "library"], documents: Sequence[ScopedDocument], index_generation_id: UUID,
    ) -> "ResearchScope":
        normalized = tuple(sorted(documents))
        if not normalized:
            raise ScopeError("scope_empty")
        if len({item.document_id for item in normalized}) != len(normalized):
            raise ScopeError("scope_duplicate_document")
        payload = _assistant_snapshot_payload(mode, normalized, index_generation_id)
        return cls(mode, None, normalized, index_generation_id, _hash_payload(payload))

    def to_snapshot(self) -> dict:
        if self.topic_id is None:
            return _assistant_snapshot_payload(self.mode, self.documents, self.index_generation_id)
        return _snapshot_payload(self.mode, self.topic_id, self.documents, self.index_generation_id)

    def verify_hash(self) -> None:
        if self.scope_hash != _hash_payload(self.to_snapshot()):
            raise ScopeError("scope_snapshot_hash_mismatch")

    @classmethod
    def from_snapshot(cls, snapshot: object, expected_hash: str) -> "ResearchScope":
        if not isinstance(snapshot, dict):
            raise ScopeError("scope_snapshot_invalid")
        try:
            version = snapshot.get("schema_version")
            if version == 2 and set(snapshot) == {"schema_version", "mode", "documents", "index_generation_id"}:
                mode = snapshot["mode"]
                if mode not in {"selected", "library"}:
                    raise ScopeError("scope_mode_invalid")
                generation_id = UUID(snapshot["index_generation_id"])
                rows = snapshot["documents"]
                if not isinstance(rows, list):
                    raise ScopeError("scope_snapshot_invalid")
                documents = []
                for row in rows:
                    if not isinstance(row, dict) or set(row) != {"document_id", "version_id"}:
                        raise ScopeError("scope_snapshot_invalid")
                    documents.append(ScopedDocument(UUID(row["document_id"]), UUID(row["version_id"])))
                scope = cls.create_assistant(mode, documents, generation_id)
                if not expected_hash or scope.scope_hash != expected_hash:
                    raise ScopeError("scope_snapshot_hash_mismatch")
                return scope
            if set(snapshot) != {"schema_version", "mode", "topic_id", "documents", "index_generation_id"}:
                raise ScopeError("scope_snapshot_invalid")
            if version != 1:
                raise ScopeError("scope_snapshot_version_unsupported")
            mode = snapshot["mode"]
            if mode not in {"topic", "selected", "library"}:
                raise ScopeError("scope_mode_invalid")
            topic_id = UUID(snapshot["topic_id"])
            generation_id = UUID(snapshot["index_generation_id"])
            rows = snapshot["documents"]
            if not isinstance(rows, list):
                raise ScopeError("scope_snapshot_invalid")
            documents = []
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"document_id", "version_id"}:
                    raise ScopeError("scope_snapshot_invalid")
                documents.append(ScopedDocument(UUID(row["document_id"]), UUID(row["version_id"])))
            scope = cls.create(mode, topic_id, documents, generation_id)
        except ScopeError:
            raise
        except (TypeError, ValueError, AttributeError):
            raise ScopeError("scope_snapshot_invalid") from None
        if not expected_hash or scope.scope_hash != expected_hash:
            raise ScopeError("scope_snapshot_hash_mismatch")
        return scope

    def require_reference(
        self,
        document_id: UUID,
        version_id: UUID,
        index_generation_id: UUID,
    ) -> ScopedDocument:
        self.verify_hash()
        if index_generation_id != self.index_generation_id:
            raise ScopeForbidden("evidence_generation_outside_scope")
        return self.require_document(document_id, version_id)

    def require_document(self, document_id: UUID, version_id: UUID | None = None) -> ScopedDocument:
        self.verify_hash()
        for item in self.documents:
            if item.document_id == document_id:
                if version_id is not None and item.version_id != version_id:
                    raise ScopeForbidden("evidence_version_outside_scope")
                return item
        raise ScopeForbidden("evidence_document_outside_scope")


def _snapshot_payload(
    mode: ScopeMode,
    topic_id: UUID,
    documents: Sequence[ScopedDocument],
    index_generation_id: UUID,
) -> dict:
    return {
        "schema_version": 1,
        "mode": mode,
        "topic_id": str(topic_id),
        "documents": [
            {"document_id": str(item.document_id), "version_id": str(item.version_id)}
            for item in sorted(documents)
        ],
        "index_generation_id": str(index_generation_id),
    }


def _assistant_snapshot_payload(mode: str, documents: Sequence[ScopedDocument], index_generation_id: UUID) -> dict:
    return {
        "schema_version": 2,
        "mode": mode,
        "documents": [
            {"document_id": str(item.document_id), "version_id": str(item.version_id)}
            for item in sorted(documents)
        ],
        "index_generation_id": str(index_generation_id),
    }


def _hash_payload(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_scope(
    session: Session,
    topic_id: UUID,
    mode: ScopeMode,
    selected_document_ids: Sequence[UUID] | None = None,
) -> ResearchScope:
    """Resolve and lock the exact document versions and index a run may use."""
    if mode not in {"topic", "selected", "library"}:
        raise ScopeError("scope_mode_invalid")

    topic = session.scalar(
        select(Topic)
        .where(Topic.id == topic_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if topic is None or topic.archived_at is not None:
        raise ScopeError("topic_unavailable")

    selected_ids = set(selected_document_ids or ())
    if mode == "topic" and selected_document_ids is not None:
        raise ScopeError("scope_selection_not_allowed")
    if mode == "selected" and not selected_ids:
        raise ScopeError("scope_selection_required")
    if mode == "library" and selected_document_ids is not None:
        raise ScopeError("scope_selection_not_allowed")

    if mode == "topic":
        candidate_ids = set(session.scalars(
            select(TopicDocument.document_id)
            .where(TopicDocument.topic_id == topic_id)
            .order_by(TopicDocument.document_id)
            .with_for_update()
        ).all())
    elif mode == "selected":
        linked_ids = set(session.scalars(
            select(TopicDocument.document_id)
            .where(TopicDocument.topic_id == topic_id, TopicDocument.document_id.in_(selected_ids))
            .order_by(TopicDocument.document_id)
            .with_for_update()
        ).all())
        if linked_ids != selected_ids:
            raise ScopeForbidden("selected_document_outside_topic")
        candidate_ids = selected_ids
    else:
        candidate_ids = None

    query = select(Document).where(Document.is_deleted.is_(False)).order_by(Document.id).with_for_update()
    if candidate_ids is not None:
        if not candidate_ids:
            raise ScopeError("scope_empty")
        query = query.where(Document.id.in_(candidate_ids))
    documents = session.scalars(query.execution_options(populate_existing=True)).all()
    if mode == "selected" and len(documents) != len(selected_ids):
        raise ScopeError("selected_document_unavailable")

    version_ids = {document.active_version_id for document in documents if document.active_version_id is not None}
    versions = session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.id.in_(version_ids))
        .order_by(DocumentVersion.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all() if version_ids else []
    versions_by_id = {version.id: version for version in versions}

    ready_documents = []
    for document in documents:
        version = versions_by_id.get(document.active_version_id)
        is_ready = (
            document.status == "ready"
            and version is not None
            and version.document_id == document.id
            and version.status == "ready"
        )
        if is_ready:
            ready_documents.append(ScopedDocument(document.id, version.id))
        elif mode == "selected":
            raise ScopeError("selected_document_unavailable")

    if not ready_documents:
        raise ScopeError("scope_empty")

    generation = session.scalar(
        select(IndexGeneration)
        .where(IndexGeneration.status == "active")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if generation is None:
        raise ScopeError("active_index_generation_missing")

    return ResearchScope.create(mode, topic_id, ready_documents, generation.id)


def resolve_assistant_scope(
    session: Session, mode: Literal["selected", "library"], selected_document_ids: Sequence[UUID] | None = None,
) -> ResearchScope:
    """Resolve a global library scope without consulting topic membership."""
    if mode not in {"selected", "library"}:
        raise ScopeError("scope_mode_invalid")
    ids = set(selected_document_ids or ())
    if mode == "selected" and not ids:
        raise ScopeError("scope_selection_required")
    if mode == "library" and selected_document_ids is not None:
        raise ScopeError("scope_selection_not_allowed")
    query = select(Document).where(Document.is_deleted.is_(False)).order_by(Document.id).with_for_update()
    if mode == "selected":
        query = query.where(Document.id.in_(ids))
    documents = session.scalars(query.execution_options(populate_existing=True)).all()
    if mode == "selected" and len(documents) != len(ids):
        raise ScopeError("selected_document_unavailable")
    ready = []
    for document in documents:
        version = session.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
        if document.status == "ready" and version is not None and version.status == "ready" and version.document_id == document.id:
            ready.append(ScopedDocument(document.id, version.id))
        elif mode == "selected":
            raise ScopeError("selected_document_unavailable")
    generation = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active").with_for_update())
    if generation is None:
        raise ScopeError("active_index_generation_missing")
    return ResearchScope.create_assistant(mode, ready, generation.id)


def validate_scope_current(session: Session, scope: ResearchScope) -> None:
    """Fail closed if a run's selected papers, versions, or pinned index expired."""
    scope.verify_hash()
    if scope.topic_id is not None:
        topic = session.scalar(
        select(Topic)
        .where(Topic.id == scope.topic_id)
        .with_for_update()
        .execution_options(populate_existing=True)
        )
        if topic is None or topic.archived_at is not None:
            raise ScopeStale("scope_topic_unavailable")

    expected = {item.document_id: item.version_id for item in scope.documents}
    documents = session.scalars(
        select(Document)
        .where(Document.id.in_(expected))
        .order_by(Document.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if len(documents) != len(expected):
        raise ScopeStale("scope_document_unavailable")
    if any(
        document.is_deleted
        or document.status != "ready"
        or document.active_version_id != expected.get(document.id)
        for document in documents
    ):
        raise ScopeStale("scope_document_changed")

    version_ids = set(expected.values())
    versions = session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.id.in_(version_ids))
        .order_by(DocumentVersion.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if len(version_ids) != len(expected) or len(versions) != len(version_ids) or any(
        version.status != "ready" or version.document_id != document_id
        for version in versions
        for document_id, version_id in expected.items()
        if version.id == version_id
    ):
        raise ScopeStale("scope_version_unavailable")

    generation = session.scalar(
        select(IndexGeneration)
        .where(IndexGeneration.id == scope.index_generation_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if generation is None or generation.status not in {"active", "retired"}:
        raise ScopeStale("scope_index_unavailable")
