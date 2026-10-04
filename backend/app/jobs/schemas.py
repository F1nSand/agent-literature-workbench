from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    task_type: Literal["system.probe"] = "system.probe"
    payload: dict = Field(default_factory=dict)


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_type: str
    status: str
    result: dict | None
    attempt_count: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    last_error_code: str | None
