from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.jobs.models import BackgroundTask
from app.jobs.schemas import TaskCreate, TaskRead
from app.jobs.store import enqueue_task

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=TaskRead, status_code=202)
def create_task(request: TaskCreate) -> TaskRead:
    try:
        with Session(get_engine()) as session:
            task = enqueue_task(session, request.task_type, request.payload)
            return TaskRead.model_validate(task)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="task queue unavailable") from None


@router.get("/{task_id}", response_model=TaskRead)
def read_task(task_id: UUID) -> TaskRead:
    try:
        with Session(get_engine()) as session:
            task = session.scalar(select(BackgroundTask).where(BackgroundTask.id == task_id))
            if task is None:
                raise HTTPException(status_code=404, detail="task not found")
            return TaskRead.model_validate(task)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="task queue unavailable") from None
