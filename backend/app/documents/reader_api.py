from uuid import UUID
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.db.session import get_engine
from app.documents.formats import file_format, MEDIA_TYPES
from app.documents.models import Document, DocumentVersion, DocumentPage, ReadingState, ReadingAnnotation
from app.documents.locations import validate_document_locator
from app.documents.storage import resolve_document_source_path
from app.workspace.context import PageContextError, validate_locator_shape

router = APIRouter(prefix="/documents", tags=["reading"])


class PositionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: UUID
    locator: dict = Field(max_length=8)


class AnnotationWrite(PositionWrite):
    quote: str = Field(default="", max_length=12000)
    comment: str = Field(default="", max_length=12000)
    color: str = Field(default="yellow", pattern="^(yellow|green|blue|pink|bookmark)$")


class MetadataWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=500)
    authors: list[str] | None = Field(default=None, max_length=100)
    year: int | None = Field(default=None, ge=1000, le=2200)
    doi: str | None = Field(default=None, max_length=250)
    tags: list[str] | None = Field(default=None, max_length=40)
    favorite: bool | None = None
    reading_status: str | None = Field(default=None, pattern="^(unread|reading|read)$")

    @model_validator(mode="after")
    def required_values(self):
        for name in self.model_fields_set - {"year", "doi"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} 不能为 null。")
        return self

    @field_validator("title")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("文献标题不能为空。")
        return value.strip() if value is not None else value

    @field_validator("tags", "authors")
    @classmethod
    def list_values(cls, value):
        if value is not None and any(not x.strip() or len(x) > 200 for x in value):
            raise ValueError("条目需为 1–200 字。")
        return list(dict.fromkeys(x.strip() for x in value)) if value is not None else value


def _version(session, document_id, version_id=None):
    doc = session.get(Document, document_id)
    if not doc or doc.is_deleted:
        raise HTTPException(404, "文献不存在。")
    ver = session.get(DocumentVersion, version_id or doc.active_version_id) if (version_id or doc.active_version_id) else None
    if not ver or ver.document_id != doc.id or ver.status != "ready":
        raise HTTPException(409, "文献尚未整理完成。")
    return doc, ver


def _check(session, doc, ver, locator):
    try:
        # Bound every locator string as well as the number of fields.
        if any(isinstance(v, str) and len(v) > 2000 for v in locator.values()):
            raise ValueError("阅读位置过长。")
        validate_locator_shape(locator)
        return validate_document_locator(session, doc, ver, locator)
    except PageContextError:
        raise HTTPException(422, "阅读位置格式无效。") from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def _annotation(row):
    return {"id": str(row.id), "locator": row.locator, "quote": row.quote, "comment": row.comment, "color": row.color, "created_at": row.created_at}


@router.get("/{document_id}/reader")
def reader_manifest(document_id: UUID, version_id: UUID | None = None):
    with Session(get_engine()) as session:
        doc, ver = _version(session, document_id, version_id)
        kind = file_format(ver.filename)
        pages = session.scalars(select(DocumentPage).where(DocumentPage.version_id == ver.id).order_by(DocumentPage.page_number)).all()
        state = session.get(ReadingState, (doc.id, ver.id))
        annotations = session.scalars(select(ReadingAnnotation).where(ReadingAnnotation.version_id == ver.id).order_by(ReadingAnnotation.created_at)).all()
        suffix = f"?version_id={ver.id}"
        return {"document_id": str(doc.id), "version_id": str(ver.id), "title": doc.title, "file_format": kind,
                "page_count": ver.page_count, "location_kind": "page" if kind in {"pdf", "docx"} else "epub" if kind == "epub" else "text",
                "file_url": f"/api/v1/documents/{doc.id}/file{suffix}", "render_url": f"/api/v1/documents/{doc.id}/rendition{suffix}",
                "sections": [{"number": p.page_number, "label": p.location_label or f"第 {p.page_number} 页", "locator": p.locator or {"kind": "page", "page": p.page_number}, "text": p.text if kind in {"md", "txt"} else ""} for p in pages],
                "reading_state": {"locator": state.locator} if state else None,
                "annotations": [_annotation(a) for a in annotations]}


@router.get("/{document_id}/rendition")
def rendition(document_id: UUID, version_id: UUID | None = None):
    with Session(get_engine()) as session:
        _, ver = _version(session, document_id, version_id)
        kind = file_format(ver.filename)
        rendition_kind = "pdf" if kind == "docx" else kind
        path = resolve_document_source_path(get_settings().data_dir, ver.storage_key, rendition_kind)
        if not path.is_file():
            raise HTTPException(404, "阅读文件暂不可用。")
        return FileResponse(path, media_type=MEDIA_TYPES[rendition_kind], content_disposition_type="inline", headers={"X-Content-Type-Options": "nosniff"})


@router.put("/{document_id}/reading-state")
def save_reading_state(document_id: UUID, payload: PositionWrite):
    with Session(get_engine()) as session:
        doc, ver = _version(session, document_id, payload.version_id)
        _check(session, doc, ver, payload.locator)
        from sqlalchemy.dialects.postgresql import insert
        stmt = insert(ReadingState).values(document_id=doc.id, version_id=ver.id, locator=payload.locator)
        session.execute(stmt.on_conflict_do_update(index_elements=[ReadingState.document_id, ReadingState.version_id], set_={"locator": payload.locator, "updated_at": func.now()}))
        if doc.reading_status == "unread":
            doc.reading_status = "reading"
        session.commit()
        return {"locator": payload.locator}


@router.post("/{document_id}/annotations", status_code=201)
def create_annotation(document_id: UUID, payload: AnnotationWrite):
    with Session(get_engine()) as session:
        doc, ver = _version(session, document_id, payload.version_id)
        _check(session, doc, ver, payload.locator)
        row = ReadingAnnotation(document_id=doc.id, **payload.model_dump())
        session.add(row)
        session.commit()
        session.refresh(row)
        return _annotation(row)


@router.delete("/{document_id}/annotations/{annotation_id}", status_code=204)
def delete_annotation(document_id: UUID, annotation_id: UUID):
    with Session(get_engine()) as session:
        row = session.get(ReadingAnnotation, annotation_id)
        if not row or row.document_id != document_id:
            raise HTTPException(404, "批注不存在。")
        session.delete(row)
        session.commit()


@router.patch("/{document_id}/metadata")
def edit_metadata(document_id: UUID, payload: MetadataWrite):
    with Session(get_engine()) as session:
        doc = session.get(Document, document_id)
        if not doc or doc.is_deleted:
            raise HTTPException(404, "文献不存在。")
        for key in payload.model_fields_set:
            value = getattr(payload, key)
            if value is None and key not in {"year", "doi"}:
                raise HTTPException(422, "字段不能置空。")
            setattr(doc, key, value)
        session.commit()
        from app.api.v1.documents import _summary
        return _summary(doc)
