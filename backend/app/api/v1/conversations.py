from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.runs.models import Message, Run
from app.topics.models import Conversation
from app.topics.schemas import (
    ConversationCreate, ConversationList, ConversationMessages, ConversationRead,
    ConversationUpdate, RunMessageRead,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("", response_model=ConversationList)
def list_conversations(include_archived: bool = Query(default=False)) -> ConversationList:
    try:
        with Session(get_engine()) as session:
            query = select(Conversation)
            if not include_archived:
                query = query.where(Conversation.archived_at.is_(None))
            rows = session.scalars(query.order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(200)).all()
            return ConversationList(items=[ConversationRead.model_validate(row) for row in rows])
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "conversation_store_unavailable"}) from None


@router.post("", response_model=ConversationRead, status_code=201)
def create_conversation(payload: ConversationCreate) -> ConversationRead:
    try:
        with Session(get_engine()) as session:
            row = Conversation(title=payload.title)
            session.add(row)
            session.commit()
            session.refresh(row)
            return ConversationRead.model_validate(row)
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "conversation_store_unavailable"}) from None


@router.get("/{conversation_id}", response_model=ConversationRead)
def read_conversation(conversation_id: UUID) -> ConversationRead:
    with Session(get_engine()) as session:
        row = session.get(Conversation, conversation_id)
        if row is None or row.archived_at is not None:
            raise HTTPException(404, detail="会话不存在")
        return ConversationRead.model_validate(row)


@router.patch("/{conversation_id}", response_model=ConversationRead)
def rename_conversation(conversation_id: UUID, payload: ConversationUpdate) -> ConversationRead:
    try:
        with Session(get_engine()) as session:
            row = session.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
            if row is None or row.archived_at is not None:
                raise HTTPException(404, detail="会话不存在")
            row.title = payload.title
            row.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(row)
            return ConversationRead.model_validate(row)
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "conversation_store_unavailable"}) from None


@router.delete("/{conversation_id}", response_model=ConversationRead)
def archive_conversation(conversation_id: UUID) -> ConversationRead:
    try:
        with Session(get_engine()) as session:
            row = session.scalar(select(Conversation).where(Conversation.id == conversation_id).with_for_update())
            if row is None:
                raise HTTPException(404, detail="会话不存在")
            if row.archived_at is None:
                row.archived_at = datetime.now(timezone.utc)
                row.updated_at = row.archived_at
                session.commit()
            return ConversationRead.model_validate(row)
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "conversation_store_unavailable"}) from None


@router.get("/{conversation_id}/messages", response_model=ConversationMessages)
def list_messages(conversation_id: UUID) -> ConversationMessages:
    try:
        with Session(get_engine()) as session:
            conversation = session.get(Conversation, conversation_id)
            if conversation is None:
                raise HTTPException(404, detail="会话不存在")
            rows = session.execute(
                select(Message).join(Run, Run.id == Message.run_id)
                .where(Run.conversation_id == conversation_id)
                .order_by(Run.created_at, Run.id, Message.sequence_number).limit(2000)
            ).scalars().all()
            return ConversationMessages(
                conversation_id=conversation_id,
                messages=[RunMessageRead.model_validate(row, from_attributes=True) for row in rows],
            )
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "conversation_store_unavailable"}) from None
