from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.documents.models import Document, DocumentVersion, IndexGeneration
from app.memory.models import MemoryItem
from app.memory.schemas import MemoryCreate, MemoryList, MemoryRead, MemoryUpdate
from app.runs.models import Evidence, Run
from app.topics.access import validate_scope_reference
from app.topics.models import Conversation, Topic
from app.topics.scope import ResearchScope, ScopeError, validate_scope_current

router = APIRouter(tags=["memories"])


def _topic(session: Session, topic_id: UUID, *, active: bool = False) -> Topic:
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="专题不存在")
    if active and topic.archived_at is not None:
        raise HTTPException(status_code=409, detail="已归档的专题不能修改记忆")
    return topic


def _snapshot_for_evidence(session: Session, topic_id: UUID, run_id: UUID, evidence_ids: list[UUID]) -> list[dict]:
    run = session.get(Run, run_id)
    conversation = session.get(Conversation, run.conversation_id) if run else None
    if run is None or run.status != "completed" or conversation is None or conversation.topic_id != topic_id:
        raise HTTPException(status_code=422, detail="研读来源不可用于此结论")
    try:
        scope = ResearchScope.from_snapshot(run.scope_snapshot, run.scope_hash)
        if scope.topic_id != topic_id:
            raise ScopeError("scope_topic_mismatch")
        validate_scope_current(session, scope)
    except ScopeError:
        raise HTTPException(status_code=409, detail="来源范围或文献版本已变化，请重新核对") from None

    rows = session.scalars(
        select(Evidence).where(Evidence.run_id == run_id, Evidence.id.in_(evidence_ids))
    ).all()
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(evidence_ids):
        raise HTTPException(status_code=422, detail="所选依据不属于该次研读")
    snapshot = []
    for evidence_id in evidence_ids:
        row = by_id[evidence_id]
        if row.read_at is None or row.validated_at is None or row.invalidated_at is not None:
            raise HTTPException(status_code=422, detail="结论只能引用已核对且有效的依据")
        try:
            ref = validate_scope_reference(
                session,
                scope,
                document_id=row.document_id,
                version_id=row.version_id,
                index_generation_id=scope.index_generation_id,
                page_number=row.page_number,
                content_unit_id=row.content_unit_id,
                excerpt=row.excerpt if row.quote_eligible else None,
            )
        except ScopeError:
            raise HTTPException(status_code=409, detail="所选依据已失效，请重新核对原文") from None
        snapshot.append({
            "evidence_id": str(row.id),
            "document_id": str(ref.document_id),
            "version_id": str(ref.version_id),
            "page_number": ref.page_number,
            "content_unit_id": str(ref.content_unit_id) if ref.content_unit_id else None,
            "content_sha256": ref.content_sha256,
        })
    return snapshot


def _memory_sources_still_valid(session: Session, memory: MemoryItem) -> bool:
    if not memory.source_run_id or not memory.evidence_snapshot:
        return False
    run = session.get(Run, memory.source_run_id)
    if run is None:
        return False
    conversation = session.get(Conversation, run.conversation_id)
    if conversation is None or conversation.topic_id != memory.topic_id:
        return False
    try:
        scope = ResearchScope.from_snapshot(run.scope_snapshot, run.scope_hash)
        topic = session.get(Topic, memory.topic_id)
        if scope.topic_id != memory.topic_id or topic is None:
            return False
    except ScopeError:
        return False
    expected = {item.document_id: item.version_id for item in scope.documents}
    documents = session.scalars(select(Document).where(Document.id.in_(expected))).all()
    if len(documents) != len(expected) or any(
        document.is_deleted or document.status != "ready" or document.active_version_id != expected[document.id]
        for document in documents
    ):
        return False
    versions = session.scalars(select(DocumentVersion).where(DocumentVersion.id.in_(expected.values()))).all()
    if len(versions) != len(expected) or any(
        version.status != "ready" or version.document_id not in expected or expected[version.document_id] != version.id
        for version in versions
    ):
        return False
    generation = session.get(IndexGeneration, scope.index_generation_id)
    if generation is None or generation.status not in {"active", "retired"}:
        return False
    for item in memory.evidence_snapshot:
        try:
            evidence = session.get(Evidence, UUID(item["evidence_id"]))
            if (
                evidence is None or evidence.run_id != run.id or evidence.invalidated_at is not None
                or evidence.validated_at is None or evidence.read_at is None
                or str(evidence.document_id) != item["document_id"]
                or str(evidence.version_id) != item["version_id"]
                or evidence.page_number != int(item["page_number"])
                or (str(evidence.content_unit_id) if evidence.content_unit_id else None) != item.get("content_unit_id")
            ):
                return False
            ref = validate_scope_reference(
                session,
                scope,
                document_id=UUID(item["document_id"]),
                version_id=UUID(item["version_id"]),
                index_generation_id=scope.index_generation_id,
                page_number=int(item["page_number"]),
                content_unit_id=UUID(item["content_unit_id"]) if item.get("content_unit_id") else None,
            )
            if ref.content_sha256 != item["content_sha256"]:
                return False
        except (ScopeError, KeyError, TypeError, ValueError):
            return False
    return True


@router.get("/topics/{topic_id}/memories", response_model=MemoryList)
def list_memories(topic_id: UUID) -> MemoryList:
    try:
        with Session(get_engine()) as session:
            with session.begin():
                _topic(session, topic_id)
                items = session.scalars(
                    select(MemoryItem).where(MemoryItem.topic_id == topic_id)
                    .order_by(MemoryItem.updated_at.desc(), MemoryItem.id).with_for_update()
                ).all()
                now = datetime.now(timezone.utc)
                for row in items:
                    if row.memory_type == "research_conclusion" and row.status in {"pending", "confirmed"}:
                        if not _memory_sources_still_valid(session, row):
                            row.status = "invalidated"
                            row.invalidated_at = now
                            row.revision += 1
                session.flush()
                result = MemoryList(items=[MemoryRead.model_validate(row) for row in items])
            return result
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法读取专题记忆") from None


@router.post("/topics/{topic_id}/memories", response_model=MemoryRead, status_code=status.HTTP_201_CREATED)
def create_memory(topic_id: UUID, payload: MemoryCreate) -> MemoryRead:
    try:
        with Session(get_engine()) as session:
            with session.begin():
                _topic(session, topic_id, active=True)
                is_preference = payload.memory_type == "preference"
                snapshot = [] if is_preference else _snapshot_for_evidence(
                    session, topic_id, payload.source_run_id, payload.evidence_ids
                )
                row = MemoryItem(
                    topic_id=topic_id,
                    memory_type=payload.memory_type,
                    status="active" if is_preference else "pending",
                    content=payload.content,
                    source_run_id=payload.source_run_id,
                    evidence_snapshot=snapshot,
                )
                session.add(row)
                session.flush()
                result = MemoryRead.model_validate(row)
            return result
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法保存专题记忆") from None


@router.patch("/memories/{memory_id}", response_model=MemoryRead)
def update_memory(memory_id: UUID, payload: MemoryUpdate) -> MemoryRead:
    invalidation_notice = False
    try:
        with Session(get_engine()) as session:
            with session.begin():
                row = session.scalar(select(MemoryItem).where(MemoryItem.id == memory_id).with_for_update())
                if row is None:
                    raise HTTPException(status_code=404, detail="记忆不存在")
                _topic(session, row.topic_id, active=True)
                if row.revision != payload.expected_revision:
                    raise HTTPException(status_code=409, detail="记忆已更新，请刷新后重试")
                now = datetime.now(timezone.utc)
                if payload.action == "edit":
                    if row.status in {"archived", "invalidated"}:
                        raise HTTPException(status_code=409, detail="此记忆当前不能编辑")
                    if row.memory_type == "research_conclusion" and not _memory_sources_still_valid(session, row):
                        raise HTTPException(status_code=409, detail="依据已失效，请基于新的研读重新提出结论")
                    row.content = payload.content or ""
                    if row.memory_type == "research_conclusion":
                        row.status = "pending"
                        row.confirmed_at = None
                elif payload.action == "confirm":
                    if row.memory_type != "research_conclusion" or row.status != "pending":
                        raise HTTPException(status_code=409, detail="只有待确认的研究结论可以确认")
                    if not _memory_sources_still_valid(session, row):
                        row.status = "invalidated"
                        row.invalidated_at = now
                        row.revision += 1
                        invalidation_notice = True
                    else:
                        row.status = "confirmed"
                        row.confirmed_at = now
                        row.invalidated_at = None
                elif payload.action == "reject":
                    if row.memory_type != "research_conclusion" or row.status != "pending":
                        raise HTTPException(status_code=409, detail="只有待确认的研究结论可以拒绝")
                    row.status = "rejected"
                else:
                    if row.status == "archived":
                        raise HTTPException(status_code=409, detail="记忆已归档")
                    row.status = "archived"
                if not invalidation_notice:
                    row.revision += 1
                session.flush()
                result = MemoryRead.model_validate(row)
        if invalidation_notice:
            raise HTTPException(status_code=409, detail="依据已失效，结论未确认")
        return result
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法更新专题记忆") from None
