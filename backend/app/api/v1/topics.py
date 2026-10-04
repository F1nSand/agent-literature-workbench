from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.documents.models import Document
from app.documents.schemas import DocumentSummary
from app.topics.models import Conversation, Topic, TopicDocument
from app.topics.schemas import (
    ConversationCreate,
    ConversationList,
    ConversationRead,
    TopicCreate,
    TopicDocumentItem,
    TopicDocumentList,
    TopicList,
    TopicSummary,
    TopicUpdate,
)

router = APIRouter(prefix="/topics", tags=["topics"])


def _document_summary(document: Document) -> DocumentSummary:
    return DocumentSummary(
        id=document.id,
        title=document.title,
        filename=document.filename,
        status=document.status,
        page_count=document.page_count,
        created_at=document.created_at,
        error_message="处理未完成，请检查 PDF 后重试。" if document.status == "failed" else None,
    )


def _topic_summary(session: Session, topic: Topic) -> TopicSummary:
    document_count = session.scalar(
        select(func.count())
        .select_from(TopicDocument)
        .join(Document, Document.id == TopicDocument.document_id)
        .where(TopicDocument.topic_id == topic.id, Document.is_deleted.is_(False))
    ) or 0
    conversation_count = session.scalar(
        select(func.count()).select_from(Conversation).where(Conversation.topic_id == topic.id)
    ) or 0
    return TopicSummary(
        id=topic.id,
        title=topic.title,
        goal=topic.goal,
        archived_at=topic.archived_at,
        created_at=topic.created_at,
        updated_at=topic.updated_at,
        document_count=document_count,
        conversation_count=conversation_count,
    )


def _require_topic(session: Session, topic_id: UUID, *, active: bool = False) -> Topic:
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="专题不存在")
    if active and topic.archived_at is not None:
        raise HTTPException(status_code=409, detail="已归档的专题不能继续修改")
    return topic


@router.post("", response_model=TopicSummary, status_code=status.HTTP_201_CREATED)
def create_topic(payload: TopicCreate) -> TopicSummary:
    try:
        with Session(get_engine()) as session:
            topic = Topic(title=payload.title, goal=payload.goal)
            session.add(topic)
            session.commit()
            session.refresh(topic)
            return _topic_summary(session, topic)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法保存专题") from None


@router.get("", response_model=TopicList)
def list_topics(include_archived: bool = Query(default=False)) -> TopicList:
    try:
        with Session(get_engine()) as session:
            query = select(Topic)
            if not include_archived:
                query = query.where(Topic.archived_at.is_(None))
            topics = session.scalars(query.order_by(Topic.updated_at.desc(), Topic.id)).all()
            return TopicList(items=[_topic_summary(session, topic) for topic in topics])
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法读取专题") from None


@router.get("/{topic_id}", response_model=TopicSummary)
def read_topic(topic_id: UUID) -> TopicSummary:
    try:
        with Session(get_engine()) as session:
            return _topic_summary(session, _require_topic(session, topic_id))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法读取专题") from None


@router.patch("/{topic_id}", response_model=TopicSummary)
def update_topic(topic_id: UUID, payload: TopicUpdate) -> TopicSummary:
    try:
        with Session(get_engine()) as session:
            topic = _require_topic(session, topic_id, active=True)
            fields = payload.model_dump(exclude_unset=True)
            for field, value in fields.items():
                setattr(topic, field, value)
            session.commit()
            session.refresh(topic)
            return _topic_summary(session, topic)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法保存专题") from None


@router.delete("/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_topic(topic_id: UUID) -> Response:
    try:
        with Session(get_engine()) as session:
            topic = _require_topic(session, topic_id)
            if topic.archived_at is None:
                topic.archived_at = func.now()
                session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法归档专题") from None


@router.post("/{topic_id}/restore", response_model=TopicSummary)
def restore_topic(topic_id: UUID) -> TopicSummary:
    try:
        with Session(get_engine()) as session:
            topic = _require_topic(session, topic_id)
            if topic.archived_at is not None:
                topic.archived_at = None
                session.commit()
                session.refresh(topic)
            return _topic_summary(session, topic)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法恢复专题") from None


@router.get("/{topic_id}/documents", response_model=TopicDocumentList)
def list_topic_documents(topic_id: UUID) -> TopicDocumentList:
    try:
        with Session(get_engine()) as session:
            _require_topic(session, topic_id)
            rows = session.execute(
                select(TopicDocument, Document)
                .join(Document, Document.id == TopicDocument.document_id)
                .where(TopicDocument.topic_id == topic_id, Document.is_deleted.is_(False))
                .order_by(TopicDocument.created_at, Document.title, Document.id)
            ).all()
            return TopicDocumentList(items=[
                TopicDocumentItem(linked_at=link.created_at, document=_document_summary(document))
                for link, document in rows
            ])
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法读取专题文献") from None


@router.put("/{topic_id}/documents/{document_id}", response_model=TopicDocumentItem, status_code=status.HTTP_200_OK)
def link_topic_document(topic_id: UUID, document_id: UUID) -> TopicDocumentItem:
    try:
        with Session(get_engine()) as session:
            _require_topic(session, topic_id, active=True)
            document = session.get(Document, document_id)
            if document is None or document.is_deleted:
                raise HTTPException(status_code=404, detail="文献不存在")
            session.execute(
                pg_insert(TopicDocument)
                .values(topic_id=topic_id, document_id=document_id)
                .on_conflict_do_nothing(index_elements=[TopicDocument.topic_id, TopicDocument.document_id])
            )
            link = session.get(TopicDocument, (topic_id, document_id))
            session.commit()
            return TopicDocumentItem(linked_at=link.created_at, document=_document_summary(document))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法关联文献") from None


@router.delete("/{topic_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def unlink_topic_document(topic_id: UUID, document_id: UUID) -> Response:
    try:
        with Session(get_engine()) as session:
            _require_topic(session, topic_id)
            link = session.get(TopicDocument, (topic_id, document_id))
            if link is None:
                raise HTTPException(status_code=404, detail="专题文献关联不存在")
            session.delete(link)
            session.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法移除专题文献") from None


@router.get("/{topic_id}/conversations", response_model=ConversationList)
def list_conversations(topic_id: UUID) -> ConversationList:
    try:
        with Session(get_engine()) as session:
            _require_topic(session, topic_id)
            rows = session.scalars(
                select(Conversation)
                .where(Conversation.topic_id == topic_id)
                .order_by(Conversation.updated_at.desc(), Conversation.id)
            ).all()
            return ConversationList(items=[ConversationRead.model_validate(row) for row in rows])
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法读取会话") from None


@router.post(
    "/{topic_id}/conversations",
    response_model=ConversationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(topic_id: UUID, payload: ConversationCreate) -> ConversationRead:
    try:
        with Session(get_engine()) as session:
            _require_topic(session, topic_id, active=True)
            conversation = Conversation(topic_id=topic_id, title=payload.title)
            session.add(conversation)
            session.commit()
            session.refresh(conversation)
            return ConversationRead.model_validate(conversation)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法创建会话") from None
