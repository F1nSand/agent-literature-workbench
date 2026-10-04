import asyncio
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.agent.budget import default_budget_snapshot
from app.db.session import get_engine
from app.documents.models import DocumentPage
from app.jobs.models import BackgroundTask
from app.runs.events import append_run_event
from app.runs.models import Evidence, Message, Run, RunEvent, UIAction
from app.topics.models import Conversation
from app.topics.schemas import (
    ConversationRunList,
    ConversationRunSummary,
    RunCreate,
    RunDetail,
    RunEventRead,
    RunEvidenceRead,
    RunMessageRead,
    RunRead,
    UIActionAck,
    UIActionAckRead,
    UIActionRead,
)
from app.topics.scope import ResearchScope, ScopeError, ScopeForbidden, resolve_scope, resolve_assistant_scope, validate_scope_current
from app.workspace.context import PageContextError, validate_page_context

router = APIRouter(tags=["runs"])


@router.post("/runs/{run_id}/actions/{action_id}/ack", response_model=UIActionAckRead)
def acknowledge_ui_action(run_id: UUID, action_id: str, payload: UIActionAck) -> UIActionAckRead:
    if len(action_id) != 32 or any(char not in "0123456789abcdef" for char in action_id):
        raise HTTPException(status_code=422, detail={"code": "action_id_invalid"})
    try:
        with Session(get_engine()) as session:
            with session.begin():
                run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
                if run is None:
                    raise HTTPException(status_code=404, detail="运行不存在")
                action = session.scalar(select(UIAction).where(
                    UIAction.run_id == run_id, UIAction.action_id == action_id,
                ).with_for_update())
                if action is None:
                    raise HTTPException(status_code=404, detail={"code": "ui_action_not_found"})
                if action.action_type not in {"open_document", "show_document_choices", "show_note_candidate"}:
                    raise HTTPException(status_code=409, detail={"code": "ui_action_type_not_allowed"})
                if action.action_type == "show_document_choices":
                    if payload.status == "succeeded":
                        selected = (str(payload.selected_document_id), str(payload.selected_version_id))
                        candidates = action.payload.get("candidates", [])
                        if not any((item.get("document_id"), item.get("version_id")) == selected
                                   for item in candidates if isinstance(item, dict)):
                            raise HTTPException(status_code=422, detail={"code": "ui_action_candidate_invalid"})
                    elif payload.selected_document_id is not None:
                        raise HTTPException(status_code=422, detail={"code": "ui_action_selection_invalid"})
                elif payload.selected_document_id is not None:
                    raise HTTPException(status_code=422, detail={"code": "ui_action_selection_unexpected"})
                ack_value = payload.model_dump(mode="json")
                if action.status != "pending":
                    if action.acknowledgement != ack_value:
                        raise HTTPException(status_code=409, detail={"code": "ui_action_already_acknowledged"})
                    return UIActionAckRead(action_id=action_id, status=payload.status, acknowledged=False)
                action.status = payload.status
                action.error = payload.error
                action.acknowledgement = ack_value
                action.acknowledged_at = datetime.now(timezone.utc)
                append_run_event(session, run_id, "ui_action_ack", {
                    "action_id": action_id, **ack_value,
                })
                return UIActionAckRead(action_id=action_id, status=payload.status, acknowledged=True)
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail={"code": "run_store_unavailable"}) from None


def _scope_error(error: ScopeError) -> HTTPException:
    if isinstance(error, PageContextError) and error.code.endswith("invalid"):
        return HTTPException(status_code=422, detail={"code": error.code, "message": "当前阅读位置格式无效"})
    if error.code == "page_context_document_outside_scope":
        return HTTPException(status_code=403, detail={
            "code": error.code,
            "message": "当前文献不在本轮范围内，请改用全库范围或将文献加入所选范围。",
        })
    if isinstance(error, ScopeForbidden):
        return HTTPException(status_code=403, detail={"code": error.code, "message": "指定的研究范围不可用"})
    if error.code in {"scope_empty", "scope_selection_required", "selected_document_unavailable"}:
        return HTTPException(status_code=422, detail={"code": error.code, "message": "当前范围没有可用文献"})
    if error.code == "active_index_generation_missing":
        return HTTPException(status_code=503, detail={"code": error.code, "message": "文献索引暂不可用"})
    return HTTPException(status_code=409, detail={"code": error.code, "message": "专题或文献状态已变化，请刷新后重试"})


def _to_detail(session: Session, run: Run) -> RunDetail:
    messages = session.scalars(
        select(Message).where(Message.run_id == run.id).order_by(Message.sequence_number, Message.id)
    ).all()
    evidence = session.scalars(
        select(Evidence).where(Evidence.run_id == run.id).order_by(Evidence.created_at, Evidence.id)
    ).all()
    page_rows = session.execute(select(DocumentPage).where(DocumentPage.version_id.in_({row.version_id for row in evidence}))).scalars().all() if evidence else []
    locations = {(row.version_id, row.page_number): row for row in page_rows}
    events = session.scalars(
        select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.sequence_number)
    ).all()
    ui_actions = session.scalars(select(UIAction).where(UIAction.run_id == run.id).order_by(UIAction.created_at, UIAction.action_id)).all()
    return RunDetail(
        **RunRead.model_validate(run).model_dump(),
        messages=[RunMessageRead.model_validate(row, from_attributes=True) for row in messages],
        evidence=[RunEvidenceRead(
            id=row.id,
            document_id=row.document_id,
            version_id=row.version_id,
            page_number=row.page_number,
            locator=(locations.get((row.version_id, row.page_number)).locator if locations.get((row.version_id, row.page_number)) else {"kind": "page", "page": row.page_number}),
            location_label=(locations.get((row.version_id, row.page_number)).location_label if locations.get((row.version_id, row.page_number)) else f"第 {row.page_number} 页"),
            content_unit_id=row.content_unit_id,
            content_type=row.content_type,
            source=row.source,
            is_derived=row.is_derived,
            quote_eligible=row.quote_eligible,
            content_sha256=row.content_sha256,
            excerpt=row.excerpt,
            is_validated=row.validated_at is not None,
            is_invalidated=row.invalidated_at is not None,
        ) for row in evidence],
        events=[RunEventRead(
            sequence_number=row.sequence_number,
            event_type=row.event_type,
            payload=row.payload,
            created_at=row.created_at,
        ) for row in events],
        ui_actions=[UIActionRead.model_validate(row) for row in ui_actions],
    )


def _existing_run_matches(
    session: Session,
    run: Run,
    payload: RunCreate,
) -> bool:
    first_message = session.scalar(
        select(Message).where(Message.run_id == run.id, Message.sequence_number == 1)
    )
    mode = payload.scope.mode if payload.scope else payload.scope_mode
    if payload.scope is None and mode == "topic" and run.scope_snapshot.get("schema_version") == 2:
        mode = "library"
    if first_message is None or first_message.content != payload.question or run.scope_mode != mode:
        return False
    if payload.scope is not None:
        if payload.scope.mode == "selected":
            stored_scope = ResearchScope.from_snapshot(run.scope_snapshot, run.scope_hash)
            if {item.document_id for item in stored_scope.documents} != set(payload.scope.document_ids or ()):
                return False
        expected_context = payload.page_context.model_dump(mode="json") if payload.page_context else None
        return run.page_context == expected_context
    if payload.scope_mode == "selected":
        stored_scope = ResearchScope.from_snapshot(run.scope_snapshot, run.scope_hash)
        if {item.document_id for item in stored_scope.documents} != set(payload.document_ids or ()):
            return False
    expected_context = payload.page_context.model_dump(mode="json") if payload.page_context else None
    return run.page_context == expected_context


@router.post(
    "/conversations/{conversation_id}/runs",
    response_model=RunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_run(
    conversation_id: UUID,
    payload: RunCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128),
) -> RunRead:
    if idempotency_key and payload.idempotency_key and idempotency_key.strip() != payload.idempotency_key.strip():
        raise HTTPException(status_code=409, detail={"code": "idempotency_key_conflict"})
    idempotency_key = (idempotency_key or payload.idempotency_key or "").strip()
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="幂等键不能为空")
    try:
        with Session(get_engine()) as session:
            with session.begin():
                conversation = session.scalar(
                    select(Conversation)
                    .where(Conversation.id == conversation_id)
                    .with_for_update()
                )
                if conversation is None:
                    raise HTTPException(status_code=404, detail="会话不存在")
                if conversation.archived_at is not None:
                    raise HTTPException(status_code=409, detail={"code": "conversation_archived"})

                existing = session.scalar(
                    select(Run).where(
                        Run.conversation_id == conversation_id,
                        Run.idempotency_key == idempotency_key,
                    ).with_for_update()
                )
                if existing is not None:
                    if not _existing_run_matches(session, existing, payload):
                        raise HTTPException(status_code=409, detail={
                            "code": "idempotency_key_conflict",
                            "message": "此请求标识已用于另一条问题",
                        })
                    result = existing
                else:
                    try:
                        if payload.scope is not None:
                            scope = resolve_assistant_scope(session, payload.scope.mode, payload.scope.document_ids)
                            requested_mode = payload.scope.mode
                        elif conversation.topic_id is not None:
                            scope = resolve_scope(session, conversation.topic_id, payload.scope_mode, payload.document_ids)
                            requested_mode = payload.scope_mode
                        else:
                            requested_mode = payload.scope_mode if payload.scope_mode in {"selected", "library"} else "library"
                            scope = resolve_assistant_scope(session, requested_mode, payload.document_ids)
                    except ScopeError as error:
                        raise _scope_error(error) from None

                    try:
                        page_context = validate_page_context(
                            session,
                            payload.page_context.model_dump(mode="json") if payload.page_context else None,
                            scope,
                        )
                    except ScopeError as error:
                        raise _scope_error(error) from None

                    run_id = uuid4()
                    run = Run(
                        id=run_id,
                        conversation_id=conversation_id,
                        status="queued",
                        scope_mode=requested_mode,
                        scope_snapshot=scope.to_snapshot(),
                        scope_hash=scope.scope_hash,
                        page_context=page_context,
                        budget_snapshot=default_budget_snapshot(),
                        idempotency_key=idempotency_key,
                    )
                    task = BackgroundTask(
                        id=uuid4(),
                        task_type="research.run",
                        payload={"run_id": str(run_id), "resume": False},
                        max_attempts=1,
                    )
                    session.add(run)
                    session.flush()
                    session.add_all([
                        Message(
                            run_id=run_id,
                            sequence_number=1,
                            role="user",
                            content=payload.question,
                            metadata_json={"scope_mode": scope.mode, "scope_hash": scope.scope_hash},
                        ),
                        task,
                    ])
                    append_run_event(session, run_id, "queued", {"status": "queued"})
                    session.flush()
                    result = run
            return RunRead.model_validate(result)
    except HTTPException:
        raise
    except IntegrityError as error:
        # Conversation row locking normally serializes this path; retain a safe idempotent fallback.
        with Session(get_engine()) as session:
            existing = session.scalar(select(Run).where(
                Run.conversation_id == conversation_id,
                Run.idempotency_key == idempotency_key,
            ))
            if existing is not None and _existing_run_matches(session, existing, payload):
                return RunRead.model_validate(existing)
        constraint = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if constraint == "uq_runs_conversation_idempotency":
            raise HTTPException(status_code=409, detail={"code": "run_create_conflict", "message": "任务创建冲突，请重试"}) from None
        raise HTTPException(status_code=503, detail={"code": "run_store_unavailable", "message": "暂时无法创建研究任务"}) from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail={"code": "run_store_unavailable", "message": "暂时无法创建研究任务"}) from None


@router.get("/runs/{run_id}", response_model=RunDetail)
def read_run(run_id: UUID) -> RunDetail:
    try:
        with Session(get_engine()) as session:
            run = session.get(Run, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="研究任务不存在")
            return _to_detail(session, run)
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail={"code": "run_store_unavailable", "message": "暂时无法读取研究任务"}) from None


def _sse_frame(sequence_number: int, event_type: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {sequence_number}\nevent: {event_type}\ndata: {data}\n\n"


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: UUID,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    try:
        cursor = int(last_event_id) if last_event_id is not None else 0
    except (TypeError, ValueError):
        cursor = -1
    try:
        with Session(get_engine()) as session:
            if session.get(Run, run_id) is None:
                raise HTTPException(status_code=404, detail="研究任务不存在")
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail={"code": "run_store_unavailable", "message": "暂时无法读取研究任务"}) from None

    terminal = {"completed", "failed", "cancelled", "interrupted", "awaiting_user"}

    async def event_stream():
        nonlocal cursor
        while not await request.is_disconnected():
            try:
                with Session(get_engine()) as session:
                    run = session.get(Run, run_id)
                    if run is None:
                        return
                    oldest = session.scalar(select(func.min(RunEvent.sequence_number)).where(RunEvent.run_id == run_id))
                    latest = session.scalar(select(func.max(RunEvent.sequence_number)).where(RunEvent.run_id == run_id)) or 0
                    status_value = run.status
                    if cursor < 0 or cursor > latest or (oldest is not None and cursor < oldest - 1):
                        snapshot = _to_detail(session, run).model_dump(mode="json")
                        cursor = latest
                        should_snapshot = True
                        events = []
                    else:
                        should_snapshot = False
                        events = session.scalars(
                            select(RunEvent)
                            .where(RunEvent.run_id == run_id, RunEvent.sequence_number > cursor)
                            .order_by(RunEvent.sequence_number)
                        ).all()
                if should_snapshot:
                    yield _sse_frame(cursor, "snapshot", {"sequence_number": cursor, "run": snapshot})
                    if status_value in terminal:
                        return
                    continue
                if events:
                    for event in events:
                        cursor = event.sequence_number
                        payload = {
                            "sequence_number": event.sequence_number,
                            "event_type": event.event_type,
                            "payload": event.payload,
                            "created_at": event.created_at.isoformat(),
                        }
                        yield _sse_frame(event.sequence_number, event.event_type, payload)
                        if event.event_type in terminal:
                            return
                    continue
                if status_value in terminal:
                    return
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)
            except SQLAlchemyError:
                # End the stream cleanly; the client reconnects from its last acknowledged sequence.
                return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/conversations/{conversation_id}/runs", response_model=ConversationRunList)
def list_conversation_runs(conversation_id: UUID) -> ConversationRunList:
    try:
        with Session(get_engine()) as session:
            if session.get(Conversation, conversation_id) is None:
                raise HTTPException(status_code=404, detail="会话不存在")
            runs = session.scalars(
                select(Run)
                .where(Run.conversation_id == conversation_id)
                .order_by(Run.created_at.desc(), Run.id.desc())
                .limit(50)
            ).all()
            items = []
            for run in runs:
                messages = session.scalars(
                    select(Message)
                    .where(Message.run_id == run.id)
                    .order_by(Message.sequence_number.desc())
                    .limit(2)
                ).all()
                by_role = {message.role: message.content for message in messages}
                items.append(ConversationRunSummary(
                    id=run.id,
                    status=run.status,
                    scope_mode=run.scope_mode,
                    created_at=run.created_at,
                    question=by_role.get("user", ""),
                    answer=by_role.get("assistant"),
                ))
            return ConversationRunList(items=items)
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail={"code": "run_store_unavailable", "message": "暂时无法读取会话记录"}) from None


@router.post("/runs/{run_id}/stop", response_model=RunDetail)
def stop_run(run_id: UUID) -> RunDetail:
    try:
        with Session(get_engine()) as session:
            with session.begin():
                run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
                if run is None:
                    raise HTTPException(status_code=404, detail="研究任务不存在")
                if run.status == "cancelled":
                    pass
                elif run.status in {"completed", "failed"}:
                    raise HTTPException(status_code=409, detail="已结束的研究任务不能停止")
                elif run.status in {"queued", "interrupted", "awaiting_user"}:
                    tasks = session.scalars(
                        select(BackgroundTask).where(
                            BackgroundTask.task_type == "research.run",
                            BackgroundTask.payload["run_id"].as_string() == str(run.id),
                        ).with_for_update()
                    ).all()
                    for task in tasks:
                        if task.payload.get("run_id") == str(run.id) and task.status == "queued":
                            task.status = "failed"
                            task.last_error_code = "run_cancelled"
                    run.status = "cancelled"
                    run.cancel_requested_at = datetime.now(timezone.utc)
                    run.finished_at = datetime.now(timezone.utc)
                    append_run_event(session, run.id, "cancelled", {"status": "cancelled"})
                else:
                    run.cancel_requested_at = datetime.now(timezone.utc)
                    append_run_event(session, run.id, "stop_requested", {"status": run.status})
            return _to_detail(session, run)
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail={"code": "run_store_unavailable", "message": "暂时无法停止研究任务"}) from None


@router.post("/runs/{run_id}/continue", response_model=RunRead, status_code=status.HTTP_202_ACCEPTED)
def continue_run(
    run_id: UUID,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
) -> RunRead:
    idempotency_key = idempotency_key.strip()
    if not idempotency_key:
        raise HTTPException(status_code=422, detail="幂等键不能为空")
    try:
        with Session(get_engine()) as session:
            with session.begin():
                run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
                if run is None:
                    raise HTTPException(status_code=404, detail="研究任务不存在")
                tasks = session.scalars(
                    select(BackgroundTask).where(
                        BackgroundTask.task_type == "research.run",
                        BackgroundTask.payload["run_id"].as_string() == str(run_id),
                    ).with_for_update()
                ).all()
                existing = next((task for task in tasks if (
                    task.payload.get("run_id") == str(run_id)
                    and task.payload.get("continue_key") == idempotency_key
                )), None)
                if existing is not None:
                    return RunRead.model_validate(run)
                if run.status not in {"interrupted", "awaiting_user"}:
                    raise HTTPException(status_code=409, detail="当前研究任务无需继续")
                scope = ResearchScope.from_snapshot(run.scope_snapshot, run.scope_hash)
                try:
                    validate_scope_current(session, scope)
                except ScopeError as error:
                    raise _scope_error(error) from None
                task = BackgroundTask(
                    id=uuid4(),
                    task_type="research.run",
                    payload={"run_id": str(run.id), "resume": True, "continue_key": idempotency_key},
                    max_attempts=1,
                )
                session.add(task)
                run.status = "queued"
                run.error_code = None
                run.cancel_requested_at = None
                append_run_event(session, run.id, "continued", {"status": "queued"})
            return RunRead.model_validate(run)
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail={"code": "run_store_unavailable", "message": "暂时无法继续研究任务"}) from None
