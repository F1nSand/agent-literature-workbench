from uuid import uuid4

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.documents.models import ContentUnit, ContentUnitEmbedding, Document, DocumentVersion, IndexGeneration
from app.jobs.models import BackgroundTask
from app.jobs.schemas import TaskRead

router = APIRouter(prefix="/index", tags=["index"])


def _ready_content_unit_count(session: Session) -> int:
    return session.scalar(
        select(func.count(ContentUnit.id))
        .join(DocumentVersion, DocumentVersion.id == ContentUnit.version_id)
        .join(Document, Document.id == ContentUnit.document_id)
        .where(Document.is_deleted.is_(False), DocumentVersion.status == "ready")
    ) or 0


@router.post("/rebuild", response_model=TaskRead, status_code=202)
def rebuild_index() -> TaskRead:
    try:
        with Session(get_engine()) as session:
            active = session.scalar(
                select(IndexGeneration)
                .where(IndexGeneration.status == "active")
                .with_for_update()
            )
            if active is None:
                raise HTTPException(status_code=409, detail="文献检索暂不可用，请稍后重试。")

            tasks = session.scalars(
                select(BackgroundTask)
                .where(
                    BackgroundTask.task_type == "index.rebuild",
                    BackgroundTask.status.in_(["queued", "running"]),
                )
                .order_by(BackgroundTask.created_at, BackgroundTask.id)
                .with_for_update()
            ).all()
            if tasks:
                return TaskRead.model_validate(tasks[0])

            if _ready_content_unit_count(session) == 0:
                raise HTTPException(status_code=409, detail="还没有可更新的文献内容。")

            for generation in session.scalars(
                select(IndexGeneration).where(IndexGeneration.status == "building").with_for_update()
            ).all():
                generation.status = "failed"
                generation.error_code = "index_rebuild_worker_interrupted"

            generation_id = uuid4()
            generation = IndexGeneration(
                id=generation_id,
                provider=active.provider,
                model=active.model,
                dimensions=active.dimensions,
                config_hash=active.config_hash,
                status="building",
            )
            task = BackgroundTask(
                task_type="index.rebuild",
                payload={
                    "source_generation_id": str(active.id),
                    "generation_id": str(generation_id),
                },
                max_attempts=1,
            )
            session.add_all([generation, task])
            session.commit()
            session.refresh(task)
            return TaskRead.model_validate(task)
    except HTTPException:
        raise
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法更新文献检索，请稍后重试。") from None
