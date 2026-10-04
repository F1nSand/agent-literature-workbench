import difflib
import hashlib
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.artifacts.export import ExportSource, render_csv, render_markdown
from app.artifacts.models import Artifact, ArtifactVersion
from app.artifacts.schemas import (
    ArtifactAdopt,
    ArtifactCandidateCreate,
    ArtifactCompare,
    ArtifactCreate,
    ArtifactFromRunCreate,
    ArtifactList,
    ArtifactRead,
    ArtifactUpdate,
    ArtifactVersionList,
    ArtifactVersionRead,
)
from app.db.session import get_engine
from app.documents.models import Document, DocumentVersion
from app.runs.models import Evidence, Message, Run
from app.topics.access import validate_scope_reference
from app.topics.models import Conversation, Topic
from app.topics.scope import ResearchScope, ScopeError, validate_scope_current

router = APIRouter(tags=["artifacts"])


def _topic(session: Session, topic_id: UUID, *, active: bool = False) -> Topic:
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="专题不存在")
    if active and topic.archived_at is not None:
        raise HTTPException(status_code=409, detail="已归档的专题不能修改成果")
    return topic


def _artifact(session: Session, artifact_id: UUID, *, lock: bool = False) -> Artifact:
    query = select(Artifact).where(Artifact.id == artifact_id)
    if lock:
        query = query.with_for_update()
    artifact = session.scalar(query)
    if artifact is None:
        raise HTTPException(status_code=404, detail="成果不存在")
    _topic(session, artifact.topic_id, active=True)
    return artifact


def _version_number(session: Session, artifact_id: UUID) -> int:
    return (session.scalar(
        select(func.max(ArtifactVersion.version_number)).where(ArtifactVersion.artifact_id == artifact_id)
    ) or 0) + 1


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_run_evidence(session: Session, topic_id: UUID, run_id: UUID) -> tuple[Run, str, list[dict]]:
    run = session.get(Run, run_id)
    conversation = session.get(Conversation, run.conversation_id) if run else None
    if run is None or run.status != "completed" or conversation is None or conversation.topic_id != topic_id:
        raise HTTPException(status_code=422, detail="此研读不能用于生成成果版本")
    answer = session.scalar(
        select(Message).where(Message.run_id == run.id, Message.role == "assistant")
        .order_by(Message.sequence_number.desc()).limit(1)
    )
    if answer is None or not answer.content.strip():
        raise HTTPException(status_code=422, detail="研读中没有可保存的回答")
    raw_citations = (answer.metadata_json or {}).get("citations", [])
    if not isinstance(raw_citations, list) or not raw_citations:
        raise HTTPException(status_code=422, detail="此回答没有可保存的有效文献依据")
    try:
        scope = ResearchScope.from_snapshot(run.scope_snapshot, run.scope_hash)
        if scope.topic_id != topic_id:
            raise ScopeError("scope_topic_mismatch")
        validate_scope_current(session, scope)
    except ScopeError:
        raise HTTPException(status_code=409, detail="研读所依据的文献已变化，请重新核对") from None
    snapshot = []
    for citation in raw_citations:
        try:
            evidence_id = UUID(citation["evidence_id"])
        except (KeyError, TypeError, ValueError):
            continue
        evidence = session.scalar(select(Evidence).where(Evidence.id == evidence_id, Evidence.run_id == run.id))
        if evidence is None or evidence.read_at is None or evidence.validated_at is None or evidence.invalidated_at is not None:
            continue
        try:
            ref = validate_scope_reference(
                session,
                scope,
                document_id=evidence.document_id,
                version_id=evidence.version_id,
                index_generation_id=scope.index_generation_id,
                page_number=evidence.page_number,
                content_unit_id=evidence.content_unit_id,
                excerpt=evidence.excerpt if evidence.quote_eligible else None,
            )
        except ScopeError:
            continue
        snapshot.append({
            "evidence_id": str(evidence.id),
            "document_id": str(ref.document_id),
            "version_id": str(ref.version_id),
            "page_number": ref.page_number,
            "content_unit_id": str(ref.content_unit_id) if ref.content_unit_id else None,
            "content_sha256": ref.content_sha256,
        })
    if not snapshot:
        raise HTTPException(status_code=409, detail="回答的文献依据已失效，请重新核对原文")
    return run, answer.content.strip(), snapshot


def _new_version(
    session: Session,
    artifact: Artifact,
    *,
    body_text: str,
    origin: str,
    state: str,
    parent_version_id: UUID | None,
    source_run_id: UUID | None = None,
    evidence_snapshot: list[dict] | None = None,
) -> ArtifactVersion:
    row = ArtifactVersion(
        artifact_id=artifact.id,
        version_number=_version_number(session, artifact.id),
        parent_version_id=parent_version_id,
        origin=origin,
        status=state,
        body_text=body_text,
        content_sha256=_hash_text(body_text),
        source_run_id=source_run_id,
        evidence_snapshot=evidence_snapshot or [],
        adopted_at=datetime.now(timezone.utc) if state == "current" else None,
    )
    session.add(row)
    session.flush()
    return row


def _read(session: Session, artifact: Artifact) -> ArtifactRead:
    current = session.get(ArtifactVersion, artifact.current_version_id) if artifact.current_version_id else None
    return ArtifactRead(
        id=artifact.id,
        topic_id=artifact.topic_id,
        title=artifact.title,
        artifact_type=artifact.artifact_type,
        current_version_id=artifact.current_version_id,
        revision=artifact.revision,
        created_at=artifact.created_at,
        updated_at=artifact.updated_at,
        current_version=ArtifactVersionRead.model_validate(current) if current else None,
    )


@router.get("/topics/{topic_id}/artifacts", response_model=ArtifactList)
def list_artifacts(topic_id: UUID) -> ArtifactList:
    try:
        with Session(get_engine()) as session:
            _topic(session, topic_id)
            rows = session.scalars(select(Artifact).where(Artifact.topic_id == topic_id)
                .order_by(Artifact.updated_at.desc(), Artifact.id)).all()
            return ArtifactList(items=[_read(session, row) for row in rows])
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法读取学习成果") from None


@router.post("/topics/{topic_id}/artifacts", response_model=ArtifactRead, status_code=status.HTTP_201_CREATED)
def create_artifact(topic_id: UUID, payload: ArtifactCreate) -> ArtifactRead:
    try:
        with Session(get_engine()) as session:
            with session.begin():
                _topic(session, topic_id, active=True)
                artifact = Artifact(topic_id=topic_id, title=payload.title, artifact_type=payload.artifact_type, revision=1)
                session.add(artifact)
                session.flush()
                version = _new_version(session, artifact, body_text=payload.body_text, origin="user", state="current", parent_version_id=None)
                artifact.current_version_id = version.id
                session.flush()
                result = _read(session, artifact)
            return result
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法保存学习成果") from None


@router.post("/topics/{topic_id}/artifacts/from-run", response_model=ArtifactRead, status_code=status.HTTP_201_CREATED)
def create_artifact_from_run(topic_id: UUID, payload: ArtifactFromRunCreate) -> ArtifactRead:
    try:
        with Session(get_engine()) as session:
            with session.begin():
                _topic(session, topic_id, active=True)
                session.scalar(select(Run).where(Run.id == payload.run_id).with_for_update())
                existing = session.scalar(
                    select(Artifact)
                    .join(ArtifactVersion, ArtifactVersion.artifact_id == Artifact.id)
                    .where(
                        Artifact.topic_id == topic_id,
                        ArtifactVersion.source_run_id == payload.run_id,
                        ArtifactVersion.origin == "generated",
                        ArtifactVersion.parent_version_id.is_(None),
                    )
                    .with_for_update(of=Artifact)
                )
                if existing is not None:
                    result = _read(session, existing)
                else:
                    run, answer, evidence = _check_run_evidence(session, topic_id, payload.run_id)
                    artifact = Artifact(topic_id=topic_id, title=payload.title, artifact_type=payload.artifact_type, revision=1)
                    session.add(artifact)
                    session.flush()
                    version = _new_version(
                        session, artifact, body_text=answer, origin="generated", state="current",
                        parent_version_id=None, source_run_id=run.id, evidence_snapshot=evidence,
                    )
                    artifact.current_version_id = version.id
                    session.flush()
                    result = _read(session, artifact)
            return result
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法保存研读成果") from None


@router.get("/artifacts/{artifact_id}", response_model=ArtifactRead)
def read_artifact(artifact_id: UUID) -> ArtifactRead:
    try:
        with Session(get_engine()) as session:
            return _read(session, _artifact(session, artifact_id))
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法读取学习成果") from None


@router.get("/artifacts/{artifact_id}/export")
def export_artifact(
    artifact_id: UUID,
    format: Literal["markdown", "csv"] = Query(),
) -> Response:
    try:
        with Session(get_engine()) as session:
            artifact = _artifact(session, artifact_id)
            if artifact.current_version_id is None:
                raise HTTPException(status_code=409, detail="成果当前版本不可用")
            version = session.get(ArtifactVersion, artifact.current_version_id)
            if version is None or version.artifact_id != artifact.id or version.status != "current":
                raise HTTPException(status_code=409, detail="成果当前版本不可用")

            snapshots = version.evidence_snapshot if isinstance(version.evidence_snapshot, list) else []
            source_refs = []
            for item in snapshots:
                if not isinstance(item, dict):
                    continue
                try:
                    document_id = UUID(str(item["document_id"]))
                    version_id = UUID(str(item["version_id"]))
                    page_number = int(item["page_number"])
                except (KeyError, TypeError, ValueError):
                    continue
                if page_number < 1:
                    continue
                try:
                    content_unit_id = str(UUID(str(item["content_unit_id"]))) if item.get("content_unit_id") else None
                except (TypeError, ValueError):
                    content_unit_id = None
                content_sha256 = item.get("content_sha256")
                if not isinstance(content_sha256, str) or len(content_sha256) != 64:
                    content_sha256 = None
                source_refs.append((document_id, version_id, page_number, content_unit_id, content_sha256))

            document_ids = {item[0] for item in source_refs}
            version_ids = {item[1] for item in source_refs}
            documents = {
                row.id: row for row in session.scalars(select(Document).where(Document.id.in_(document_ids)))
            } if document_ids else {}
            source_versions = {
                (row.document_id, row.id): row
                for row in session.scalars(select(DocumentVersion).where(DocumentVersion.id.in_(version_ids)))
            } if version_ids else {}
            sources = [
                ExportSource(
                    document_title=documents[item[0]].title if item[0] in documents else "来源文献不可用",
                    document_id=str(item[0]),
                    version_id=str(item[1]),
                    version_number=(source_versions[(item[0], item[1])].version_number
                                    if (item[0], item[1]) in source_versions else None),
                    page_number=item[2],
                    content_unit_id=item[3],
                    content_sha256=item[4],
                )
                for item in source_refs
            ]
            body_text = version.body_text or ""
            if format == "markdown":
                content = render_markdown(
                    title=artifact.title,
                    artifact_type=artifact.artifact_type,
                    version_number=version.version_number,
                    body_text=body_text,
                    sources=sources,
                )
                media_type = "text/markdown; charset=utf-8"
                extension = "md"
            else:
                content = render_csv(
                    title=artifact.title,
                    artifact_type=artifact.artifact_type,
                    version_number=version.version_number,
                    body_text=body_text,
                    sources=sources,
                )
                media_type = "text/csv; charset=utf-8"
                extension = "csv"
            return Response(
                content=content,
                media_type=media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="artifact-{artifact.id}.{extension}"',
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法导出学习成果") from None


@router.patch("/artifacts/{artifact_id}", response_model=ArtifactRead)
def update_artifact(artifact_id: UUID, payload: ArtifactUpdate) -> ArtifactRead:
    try:
        with Session(get_engine()) as session:
            with session.begin():
                artifact = _artifact(session, artifact_id, lock=True)
                if artifact.revision != payload.expected_revision:
                    raise HTTPException(status_code=409, detail="成果已更新，请刷新后重试")
                fields = payload.model_dump(exclude_unset=True)
                fields.pop("expected_revision")
                if "title" in fields:
                    artifact.title = fields["title"]
                if "body_text" in fields:
                    current = session.get(ArtifactVersion, artifact.current_version_id) if artifact.current_version_id else None
                    if current is None:
                        raise HTTPException(status_code=409, detail="成果当前版本不可用")
                    current.status = "superseded"
                    version = _new_version(session, artifact, body_text=fields["body_text"], origin="user", state="current", parent_version_id=current.id)
                    artifact.current_version_id = version.id
                artifact.revision += 1
                session.flush()
                result = _read(session, artifact)
            return result
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法更新学习成果") from None


@router.get("/artifacts/{artifact_id}/versions", response_model=ArtifactVersionList)
def list_artifact_versions(artifact_id: UUID) -> ArtifactVersionList:
    try:
        with Session(get_engine()) as session:
            _artifact(session, artifact_id)
            rows = session.scalars(select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact_id)
                .order_by(ArtifactVersion.version_number.desc())).all()
            return ArtifactVersionList(items=[ArtifactVersionRead.model_validate(row) for row in rows])
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法读取成果版本") from None


@router.post("/artifacts/{artifact_id}/candidates", response_model=ArtifactVersionRead, status_code=status.HTTP_201_CREATED)
def create_artifact_candidate(artifact_id: UUID, payload: ArtifactCandidateCreate) -> ArtifactVersionRead:
    try:
        with Session(get_engine()) as session:
            with session.begin():
                artifact = _artifact(session, artifact_id, lock=True)
                existing = session.scalar(select(ArtifactVersion).where(
                    ArtifactVersion.artifact_id == artifact.id,
                    ArtifactVersion.source_run_id == payload.run_id,
                ))
                if existing is not None:
                    result = ArtifactVersionRead.model_validate(existing)
                else:
                    _, answer, evidence = _check_run_evidence(session, artifact.topic_id, payload.run_id)
                    version = _new_version(
                        session, artifact, body_text=answer, origin="generated", state="candidate",
                        parent_version_id=artifact.current_version_id, source_run_id=payload.run_id,
                        evidence_snapshot=evidence,
                    )
                    result = ArtifactVersionRead.model_validate(version)
            return result
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法生成成果候选版本") from None


@router.get("/artifacts/{artifact_id}/compare", response_model=ArtifactCompare)
def compare_artifact_versions(
    artifact_id: UUID,
    base_version_id: UUID = Query(),
    compare_version_id: UUID = Query(),
) -> ArtifactCompare:
    try:
        with Session(get_engine()) as session:
            _artifact(session, artifact_id)
            versions = session.scalars(select(ArtifactVersion).where(
                ArtifactVersion.artifact_id == artifact_id,
                ArtifactVersion.id.in_([base_version_id, compare_version_id]),
            )).all()
            by_id = {row.id: row for row in versions}
            if set(by_id) != {base_version_id, compare_version_id}:
                raise HTTPException(status_code=404, detail="成果版本不存在")
            base, compared = by_id[base_version_id], by_id[compare_version_id]
            diff = "\n".join(difflib.unified_diff(
                base.body_text.splitlines(), compared.body_text.splitlines(),
                fromfile=f"版本 {base.version_number}", tofile=f"版本 {compared.version_number}", lineterm="",
            ))
            return ArtifactCompare(
                base=ArtifactVersionRead.model_validate(base),
                compare=ArtifactVersionRead.model_validate(compared),
                unified_diff=diff,
            )
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法比较成果版本") from None


@router.post("/artifacts/{artifact_id}/adopt", response_model=ArtifactRead)
def adopt_artifact_candidate(artifact_id: UUID, payload: ArtifactAdopt) -> ArtifactRead:
    try:
        with Session(get_engine()) as session:
            with session.begin():
                artifact = _artifact(session, artifact_id, lock=True)
                if artifact.revision != payload.expected_revision:
                    raise HTTPException(status_code=409, detail="成果已更新，请刷新后重试")
                candidate = session.scalar(select(ArtifactVersion).where(
                    ArtifactVersion.id == payload.version_id,
                    ArtifactVersion.artifact_id == artifact.id,
                ).with_for_update())
                if candidate is None or candidate.status != "candidate":
                    raise HTTPException(status_code=409, detail="候选版本已变化或不属于此成果")
                if candidate.parent_version_id != artifact.current_version_id:
                    raise HTTPException(status_code=409, detail="当前版本已变化，请重新比较后采纳")
                current = session.get(ArtifactVersion, artifact.current_version_id) if artifact.current_version_id else None
                if current is not None:
                    current.status = "superseded"
                candidate.status = "current"
                candidate.adopted_at = datetime.now(timezone.utc)
                artifact.current_version_id = candidate.id
                artifact.revision += 1
                session.flush()
                result = _read(session, artifact)
            return result
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法采纳成果版本") from None
