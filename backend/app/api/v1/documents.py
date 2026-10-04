from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import fitz
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_engine
from app.documents.models import ContentUnit, Document, DocumentPage, DocumentVersion
from app.documents.storage import resolve_document_pdf_path, resolve_document_source_path
from app.documents.formats import FORMATS, MEDIA_TYPES, file_format, validate_source
from app.documents.schemas import (
    ContentUnitRead,
    DocumentBatchUpload,
    DocumentList,
    DocumentPageRead,
    DocumentSummary,
    DocumentUploadOutcome,
    DocumentVersionList,
    DocumentVersionSummary,
    QuestionCreate,
    QuestionQueued,
)
from app.jobs.models import BackgroundTask

router = APIRouter(prefix="/documents", tags=["documents"])

STATUS_MESSAGES = {
    "queued": None,
    "processing": None,
    "ready": None,
    "failed": "处理未完成，请检查 PDF 后重试。",
}
MAX_INGESTION_ATTEMPTS = 3


@dataclass(frozen=True)
class StagedPdf:
    filename: str
    sha256: str
    page_count: int
    storage_key: str
    final_path: Path


def _summary(document: Document) -> DocumentSummary:
    return DocumentSummary(
        id=document.id,
        title=document.title,
        filename=document.filename,
        status=document.status,
        page_count=document.page_count,
        created_at=document.created_at,
        error_message=STATUS_MESSAGES.get(document.status),
        active_version_id=document.active_version_id,
        file_format=file_format(document.filename),
        authors=document.authors, year=document.year, doi=document.doi, tags=document.tags,
        favorite=document.favorite, reading_status=document.reading_status,
    )


def _storage_path(storage_key: str) -> Path:
    settings = get_settings()
    try:
        return resolve_document_pdf_path(settings.data_dir, storage_key)
    except ValueError:
        raise HTTPException(status_code=404, detail="PDF file is unavailable") from None


def _processing_config_hash() -> str:
    settings = get_settings()
    config = {
        "pipeline": "document-ingestion-v4",
        "formats": sorted(FORMATS),
        "locator_schema": 1,
        "parser": "pymupdf-structure-v1",
        "ocr_model": settings.siliconflow_ocr_model,
        "embedding_model": settings.siliconflow_embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
        "chunk_chars": 2200,
    }
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()


async def _stage_pdf(file: UploadFile) -> StagedPdf:
    settings = get_settings()
    if not file.filename:
        raise HTTPException(status_code=400, detail="a document filename is required")
    filename = file.filename.replace("\\", "/").split("/")[-1][:500]
    kind = file_format(filename)
    if kind not in FORMATS:
        raise HTTPException(status_code=415, detail="支持 PDF、EPUB、DOCX、Markdown 和纯文本。")

    root = Path(settings.data_dir).expanduser().resolve()
    document_dir = root / "documents"
    temp_dir = root / "tmp"
    document_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{uuid4().hex}.upload"
    digest = hashlib.sha256()
    size = 0
    first_bytes = b""
    try:
        with temp_path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                if not first_bytes:
                    first_bytes = chunk[:5]
                size += len(chunk)
                if size > settings.max_pdf_bytes:
                    raise HTTPException(status_code=413, detail="文献超过 100 MB 上传限制。")
                digest.update(chunk)
                output.write(chunk)
        if kind == "pdf" and first_bytes != b"%PDF-":
            raise HTTPException(status_code=415, detail="uploaded file is not a readable PDF")

        try:
            page_count = validate_source(temp_path, kind, settings.max_pdf_pages)
        except HTTPException:
            raise
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except Exception:
            raise HTTPException(status_code=422, detail="文件格式无效或无法读取。") from None

        storage_key = uuid4().hex
        try:
            final_path = resolve_document_source_path(root, storage_key, kind)
        except ValueError:
            raise HTTPException(status_code=500, detail="document storage is unavailable") from None
        os.replace(temp_path, final_path)
        return StagedPdf(filename, digest.hexdigest(), page_count, storage_key, final_path)
    finally:
        temp_path.unlink(missing_ok=True)
        await file.close()


def _activate_version(document: Document, version: DocumentVersion) -> None:
    document.active_version_id = version.id
    document.filename = version.filename
    document.storage_key = version.storage_key
    document.sha256 = version.sha256
    document.status = "ready"
    document.page_count = version.page_count
    document.error_code = None


async def _accept_upload(
    file: UploadFile,
    replace_document_id: UUID | None = None,
) -> tuple[DocumentSummary, bool]:
    staged = await _stage_pdf(file)
    keep_file = False
    try:
        with Session(get_engine()) as session:
            duplicate = session.scalar(select(DocumentVersion).where(DocumentVersion.sha256 == staged.sha256))
            if duplicate is not None:
                document = session.get(Document, duplicate.document_id)
                if document is None or document.is_deleted:
                    raise HTTPException(status_code=409, detail="this PDF is already in the library")
                if replace_document_id is not None and document.id != replace_document_id:
                    raise HTTPException(status_code=409, detail="this PDF belongs to another library entry")
                if replace_document_id is not None and duplicate.status == "ready":
                    _activate_version(document, duplicate)
                    session.commit()
                return _summary(document), True

            if replace_document_id is None:
                document_id = uuid4()
                document = Document(
                    id=document_id,
                    title=Path(staged.filename).stem[:500] or "未命名文献",
                    filename=staged.filename,
                    storage_key=staged.storage_key,
                    sha256=staged.sha256,
                    status="queued",
                    page_count=staged.page_count,
                    active_version_id=None,
                    is_deleted=False,
                )
                version_number = 1
            else:
                document = session.get(Document, replace_document_id)
                if document is None or document.is_deleted:
                    raise HTTPException(status_code=404, detail="document not found")
                document_id = document.id
                version_number = session.scalar(
                    select(func.max(DocumentVersion.version_number)).where(DocumentVersion.document_id == document_id)
                ) or 0
                version_number += 1

            version_id = uuid4()
            version = DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_number=version_number,
                filename=staged.filename,
                storage_key=staged.storage_key,
                sha256=staged.sha256,
                status="queued",
                page_count=staged.page_count,
                processing_config_hash=_processing_config_hash(),
            )
            task = BackgroundTask(
                task_type="documents.ingest",
                payload={"document_id": str(document_id), "version_id": str(version_id)},
                max_attempts=1,
            )
            session.add_all([version, task])
            if replace_document_id is None:
                session.add(document)
            session.commit()
            session.refresh(document)
            keep_file = True
            return _summary(document), False
    except IntegrityError:
        with Session(get_engine()) as session:
            duplicate = session.scalar(select(DocumentVersion).where(DocumentVersion.sha256 == staged.sha256))
            if duplicate is not None:
                document = session.get(Document, duplicate.document_id)
                if document is not None and not document.is_deleted:
                    return _summary(document), True
        raise HTTPException(status_code=409, detail="document was uploaded concurrently") from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="document store unavailable") from None
    finally:
        if not keep_file:
            staged.final_path.unlink(missing_ok=True)


@router.get("", response_model=DocumentList)
def list_documents() -> DocumentList:
    try:
        with Session(get_engine()) as session:
            rows = session.scalars(
                select(Document).where(Document.is_deleted.is_(False)).order_by(Document.created_at.desc(), Document.id)
            ).all()
            return DocumentList(items=[_summary(row) for row in rows])
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="document store unavailable") from None


@router.post("", response_model=DocumentSummary, status_code=202)
async def upload_document(file: UploadFile = File(...)) -> DocumentSummary:
    summary, _ = await _accept_upload(file)
    return summary


@router.post("/batch", response_model=DocumentBatchUpload, status_code=202)
async def upload_documents(files: list[UploadFile] = File(...)) -> DocumentBatchUpload:
    if not files or len(files) > 30:
        raise HTTPException(status_code=413, detail="choose between 1 and 30 PDF files")
    outcomes: list[DocumentUploadOutcome] = []
    for file in files:
        filename = (file.filename or "").replace("\\", "/").split("/")[-1]
        try:
            document, duplicate = await _accept_upload(file)
            outcomes.append(DocumentUploadOutcome(
                filename=filename,
                status="duplicate" if duplicate else "queued",
                document=document,
            ))
        except HTTPException as error:
            outcomes.append(DocumentUploadOutcome(
                filename=filename,
                status="rejected",
                error_message=str(error.detail),
            ))
    return DocumentBatchUpload(items=outcomes)


@router.get("/{document_id}/versions", response_model=DocumentVersionList)
def list_document_versions(document_id: UUID) -> DocumentVersionList:
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        if document is None or document.is_deleted:
            raise HTTPException(status_code=404, detail="document not found")
        versions = session.scalars(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version_number.desc())
        ).all()
        return DocumentVersionList(items=[DocumentVersionSummary(
            id=version.id,
            version_number=version.version_number,
            filename=version.filename,
            status=version.status,
            page_count=version.page_count,
            created_at=version.created_at,
            is_active=version.id == document.active_version_id,
        ) for version in versions])


@router.post("/{document_id}/versions", response_model=DocumentSummary, status_code=202)
async def replace_document_version(document_id: UUID, file: UploadFile = File(...)) -> DocumentSummary:
    summary, _ = await _accept_upload(file, replace_document_id=document_id)
    return summary


@router.post("/{document_id}/versions/{version_id}/activate", response_model=DocumentSummary)
def activate_document_version(document_id: UUID, version_id: UUID) -> DocumentSummary:
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        version = session.get(DocumentVersion, version_id)
        if document is None or document.is_deleted or version is None or version.document_id != document_id:
            raise HTTPException(status_code=404, detail="document version not found")
        if version.status != "ready":
            raise HTTPException(status_code=409, detail="document version is not ready")
        _activate_version(document, version)
        session.commit()
        session.refresh(document)
        return _summary(document)


@router.post(
    "/{document_id}/versions/{version_id}/retry",
    response_model=DocumentVersionSummary,
    status_code=202,
)
def retry_document_version(document_id: UUID, version_id: UUID) -> DocumentVersionSummary:
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        version = session.scalar(
            select(DocumentVersion)
            .where(DocumentVersion.id == version_id, DocumentVersion.document_id == document_id)
            .with_for_update()
        )
        if document is None or document.is_deleted or version is None:
            raise HTTPException(status_code=404, detail="document version not found")
        if document.active_version_id == version.id:
            raise HTTPException(status_code=409, detail="the current reading version cannot be retried")
        if version.status in {"queued", "processing"}:
            return DocumentVersionSummary(
                id=version.id,
                version_number=version.version_number,
                filename=version.filename,
                status=version.status,
                page_count=version.page_count,
                created_at=version.created_at,
                is_active=False,
            )
        if version.status != "failed":
            raise HTTPException(status_code=409, detail="only an incomplete version can be retried")

        task = session.scalar(
            select(BackgroundTask)
            .where(
                BackgroundTask.task_type == "documents.ingest",
                BackgroundTask.payload["document_id"].as_string() == str(document_id),
                BackgroundTask.payload["version_id"].as_string() == str(version_id),
            )
            .order_by(BackgroundTask.created_at.desc())
            .with_for_update()
            .limit(1)
        )
        if task is None or task.status != "failed":
            raise HTTPException(status_code=409, detail="the processing task is unavailable for retry")
        if task.attempt_count >= MAX_INGESTION_ATTEMPTS:
            raise HTTPException(status_code=409, detail="retry limit reached; add the PDF again as a new version")

        version.status = "queued"
        version.error_code = None
        if document.active_version_id is None:
            document.status = "queued"
            document.error_code = None
        task.status = "queued"
        task.max_attempts = task.attempt_count + 1
        task.last_error_code = None
        task.lease_owner = None
        task.lease_expires_at = None
        session.commit()
        session.refresh(version)
        return DocumentVersionSummary(
            id=version.id,
            version_number=version.version_number,
            filename=version.filename,
            status=version.status,
            page_count=version.page_count,
            created_at=version.created_at,
            is_active=False,
        )


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: UUID) -> Response:
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        if document is None or document.is_deleted:
            raise HTTPException(status_code=404, detail="document not found")
        document.is_deleted = True
        document.deleted_at = func.now()
        session.commit()
    return Response(status_code=204)


@router.get("/{document_id}", response_model=DocumentSummary)
def read_document(document_id: UUID) -> DocumentSummary:
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        if document is None or document.is_deleted:
            raise HTTPException(status_code=404, detail="document not found")
        return _summary(document)


@router.get("/{document_id}/file")
def read_pdf(document_id: UUID, version_id: UUID | None = None) -> FileResponse:
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        if document is None or document.is_deleted or document.active_version_id is None:
            raise HTTPException(status_code=404, detail="document not found")
        version = session.get(DocumentVersion, version_id or document.active_version_id)
        if version is None or version.document_id != document_id or version.status != "ready":
            raise HTTPException(status_code=404, detail="document version is unavailable")
        kind = file_format(version.filename)
        path = resolve_document_source_path(get_settings().data_dir, version.storage_key, kind)
        filename = version.filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="PDF file is unavailable")
    return FileResponse(path, media_type=MEDIA_TYPES[kind], filename=filename, content_disposition_type="inline", headers={"X-Content-Type-Options": "nosniff"})


@router.get("/{document_id}/pages/{page_number}", response_model=DocumentPageRead)
def read_page(document_id: UUID, page_number: int) -> DocumentPageRead:
    if page_number < 1:
        raise HTTPException(status_code=422, detail="page number must be positive")
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        if document is None or document.is_deleted or document.active_version_id is None:
            raise HTTPException(status_code=404, detail="document not found")
        version_id = document.active_version_id
        page = session.get(DocumentPage, (version_id, page_number))
        if page is None:
            raise HTTPException(status_code=404, detail="page is not available yet")
        units = session.scalars(
            select(ContentUnit).where(
                ContentUnit.document_id == document_id,
                ContentUnit.version_id == version_id,
                ContentUnit.page_number == page_number,
            ).order_by(ContentUnit.ordinal)
        ).all()
        return DocumentPageRead(
            document_id=document_id,
            version_id=version_id,
            page_number=page_number,
            text=page.text,
            locator=page.locator, location_label=page.location_label,
            extraction_method=page.extraction_method,
            is_derived=page.extraction_method == "cloud_ocr",
            content_units=[ContentUnitRead(
                id=unit.id,
                text=unit.text,
                content_type=unit.content_type,
                source=(unit.extra or {}).get("source", "pdf_text"),
                is_derived=bool((unit.extra or {}).get("is_derived", (unit.extra or {}).get("source", "pdf_text") != "pdf_text")),
                quote_eligible=bool((unit.extra or {}).get("quote_eligible", (unit.extra or {}).get("source", "pdf_text") == "pdf_text")),
                bbox=(unit.extra or {}).get("bbox"),
            ) for unit in units],
        )


@router.get("/{document_id}/pages/{page_number}/preview")
def read_page_preview(document_id: UUID, page_number: int, version_id: UUID | None = None) -> Response:
    if page_number < 1:
        raise HTTPException(status_code=422, detail="页码必须大于零。")
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        if document is None or document.is_deleted or document.active_version_id is None:
            raise HTTPException(status_code=404, detail="文献不存在。")
        requested_version_id = version_id or document.active_version_id
        version = session.get(DocumentVersion, requested_version_id)
        page = session.get(DocumentPage, (requested_version_id, page_number))
        if version is None or version.document_id != document_id or version.status != "ready" or page is None:
            raise HTTPException(status_code=404, detail="原文页面暂不可用。")
        if file_format(version.filename) not in {"pdf", "docx"}:
            raise HTTPException(status_code=422, detail="此文献使用章节或段落导航。")
        pdf_path = _storage_path(version.storage_key)
    if not pdf_path.is_file():
        raise HTTPException(status_code=404, detail="PDF 原文暂不可用。")

    try:
        with fitz.open(pdf_path) as pdf:
            if page_number > pdf.page_count:
                raise HTTPException(status_code=404, detail="原文页面暂不可用。")
            pdf_page = pdf[page_number - 1]
            longest_edge = max(1.0, pdf_page.rect.width, pdf_page.rect.height)
            scale = min(2.0, 3_000 / longest_edge)
            image = pdf_page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=422, detail="PDF 页面暂时无法显示，请重新载入。") from None
    return Response(
        content=image,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.post("/{document_id}/questions", response_model=QuestionQueued, status_code=202)
def ask_document_question(document_id: UUID, request: QuestionCreate) -> QuestionQueued:
    question = " ".join(request.question.split())
    if not question:
        raise HTTPException(status_code=422, detail="question is required")
    settings = get_settings()
    if settings.siliconflow_api_key is None or not settings.siliconflow_api_key.get_secret_value():
        raise HTTPException(status_code=503, detail="cloud model credentials are not configured")
    with Session(get_engine()) as session:
        document = session.get(Document, document_id)
        if document is None or document.is_deleted:
            raise HTTPException(status_code=404, detail="document not found")
        version = session.get(DocumentVersion, document.active_version_id) if document.active_version_id else None
        if document.status != "ready" or version is None or version.status != "ready":
            raise HTTPException(status_code=409, detail="document is not ready for questions")
        task = BackgroundTask(
            task_type="documents.answer",
            payload={"document_id": str(document_id), "version_id": str(version.id), "question": question},
            max_attempts=1,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return QuestionQueued(task_id=task.id, status=task.status)
