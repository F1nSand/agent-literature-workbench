from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    filename: str
    status: str
    page_count: int
    created_at: datetime
    error_message: str | None = None
    active_version_id: UUID | None = None
    file_format: str = "pdf"
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    tags: list[str] = Field(default_factory=list)
    favorite: bool = False
    reading_status: str = "unread"


class DocumentList(BaseModel):
    items: list[DocumentSummary]


class DocumentVersionSummary(BaseModel):
    id: UUID
    version_number: int
    filename: str
    status: str
    page_count: int
    created_at: datetime
    is_active: bool


class DocumentVersionList(BaseModel):
    items: list[DocumentVersionSummary]


class DocumentUploadOutcome(BaseModel):
    filename: str
    status: str
    document: DocumentSummary | None = None
    error_message: str | None = None


class DocumentBatchUpload(BaseModel):
    items: list[DocumentUploadOutcome]


class ContentUnitRead(BaseModel):
    id: UUID
    text: str
    content_type: str = "text"
    source: str = "pdf_text"
    is_derived: bool = False
    quote_eligible: bool = True
    bbox: list[float] | None = None


class DocumentPageRead(BaseModel):
    document_id: UUID
    version_id: UUID
    page_number: int
    text: str
    extraction_method: str = "pdf_text"
    is_derived: bool = False
    content_units: list[ContentUnitRead]
    locator: dict = Field(default_factory=dict)
    location_label: str = ""


class QuestionCreate(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class QuestionQueued(BaseModel):
    task_id: UUID
    status: str
