from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.artifacts.models import Artifact, ArtifactVersion
from app.documents.models import ContentUnit, Document, DocumentPage, DocumentVersion, ProviderCall
from app.documents.formats import file_format
from app.documents.storage import resolve_document_pdf_path, resolve_document_source_path
from app.jobs.models import BackgroundTask
from app.memory.models import MemoryItem
from app.notes.models import Note, NoteVersion
from app.runs.models import Evidence, Message, Run, RunEvent, ToolCall
from app.topics.models import Conversation, Topic, TopicDocument


class RestoreVerificationError(RuntimeError):
    """Raised when the restored database and file volume disagree."""


def verify_version_files(versions: Iterable[Any], data_dir: str | Path) -> int:
    verified, _, _ = verify_version_assets(versions, data_dir)
    return verified


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pdf_page_count(path: Path) -> int:
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise RestoreVerificationError("document PDF rendition is encrypted")
        return len(reader.pages)
    except (OSError, PdfReadError, ValueError) as error:
        raise RestoreVerificationError("document PDF rendition is invalid") from error


def verify_version_assets(versions: Iterable[Any], data_dir: str | Path) -> tuple[int, int, list[dict[str, Any]]]:
    verified = 0
    verified_pdf_files = 0
    docx_renditions: list[dict[str, Any]] = []
    for version in versions:
        storage_key = getattr(version, "storage_key", None)
        expected_hash = getattr(version, "sha256", None)
        if not isinstance(storage_key, str) or re.fullmatch(r"[0-9a-f]{32}", storage_key) is None:
            raise RestoreVerificationError("document version storage key is invalid")
        if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
            raise RestoreVerificationError("document version checksum is invalid")
        filename = getattr(version, "filename", None)
        if not isinstance(filename, str) or not filename:
            # Older direct callers supplied only a key/hash; retain their PDF default.
            kind = "pdf"
        else:
            kind = file_format(filename)
        if kind not in {"pdf", "epub", "docx", "md", "txt"}:
            raise RestoreVerificationError("document version filename format is invalid")
        try:
            source_path = resolve_document_source_path(data_dir, storage_key, kind)
        except ValueError as error:
            raise RestoreVerificationError("document version filename format is invalid") from error
        if not source_path.is_file():
            raise RestoreVerificationError("document version original file is missing")
        actual_hash = _sha256(source_path)
        if actual_hash != expected_hash:
            raise RestoreVerificationError("document version original file checksum mismatch")
        verified += 1
        status = getattr(version, "status", None)
        expected_pages = getattr(version, "page_count", None)
        if kind == "pdf":
            verified_pdf_files += 1
            if status == "ready" and _pdf_page_count(source_path) != expected_pages:
                raise RestoreVerificationError("document PDF page count mismatch")
        elif kind == "docx":
            rendition = resolve_document_pdf_path(data_dir, storage_key)
            exists = rendition.is_file()
            if status == "ready" and not exists:
                raise RestoreVerificationError("DOCX PDF rendition is missing")
            rendition_hash = _sha256(rendition) if exists else None
            rendition_pages = _pdf_page_count(rendition) if exists else None
            if exists:
                verified_pdf_files += 1
                if status == "ready" and rendition_pages != expected_pages:
                    raise RestoreVerificationError("DOCX PDF rendition page count mismatch")
            docx_renditions.append({
                "version_id": str(getattr(version, "id", "")),
                "storage_key": storage_key,
                "exists": exists,
                "sha256": rendition_hash,
                "page_count": rendition_pages,
            })
    return verified, verified_pdf_files, docx_renditions


def verify_restored_library(engine: Engine, data_dir: str | Path) -> dict[str, Any]:
    available_tables = set(inspect(engine).get_table_names())
    with Session(engine) as session:
        try:
            schema_revision = session.scalar(text("SELECT version_num FROM alembic_version"))
        except SQLAlchemyError as error:
            raise RestoreVerificationError("database schema revision is unavailable") from error
        if not isinstance(schema_revision, str) or not schema_revision:
            raise RestoreVerificationError("database schema revision is unavailable")

        versions = session.scalars(select(DocumentVersion).order_by(DocumentVersion.id)).all()
        verified_original_count, verified_pdf_count, docx_renditions = verify_version_assets(versions, data_dir)
        versions_by_id = {version.id: version for version in versions}

        ready_versions = [version for version in versions if version.status == "ready"]
        page_counts = dict(session.execute(
            select(DocumentPage.version_id, func.count())
            .group_by(DocumentPage.version_id)
        ).all())
        for version in ready_versions:
            if page_counts.get(version.id, 0) != version.page_count:
                raise RestoreVerificationError("ready document version page count mismatch")
            unit_count = session.scalar(
                select(func.count()).select_from(ContentUnit).where(ContentUnit.version_id == version.id)
            ) or 0
            if unit_count < 1:
                raise RestoreVerificationError("ready document version has no content units")

        documents = session.scalars(select(Document)).all()
        for document in documents:
            if document.active_version_id is None:
                continue
            active_version = versions_by_id.get(document.active_version_id)
            if (
                active_version is None
                or active_version.document_id != document.id
                or active_version.status != "ready"
                or document.status != "ready"
            ):
                raise RestoreVerificationError("active document version relationship is invalid")

        return {
            "status": "verified",
            "schema_revision": schema_revision,
            "documents_total": len(documents),
            "documents_active": sum(not document.is_deleted for document in documents),
            "versions": len(versions),
            "ready_versions": len(ready_versions),
            "verified_original_files": verified_original_count,
            "verified_pdf_files": verified_pdf_count,
            "docx_renditions": docx_renditions,
            "verified_docx_renditions": sum(1 for rendition in docx_renditions if rendition["exists"]),
            "pages": sum(page_counts.values()),
            "content_units": session.scalar(select(func.count()).select_from(ContentUnit)) or 0,
            "topics": session.scalar(select(func.count()).select_from(Topic)) or 0,
            "topic_document_links": session.scalar(select(func.count()).select_from(TopicDocument)) or 0,
            "conversations": session.scalar(select(func.count()).select_from(Conversation)) or 0,
            "runs": session.scalar(select(func.count()).select_from(Run)) or 0,
            "messages": session.scalar(select(func.count()).select_from(Message)) or 0,
            "evidence": session.scalar(select(func.count()).select_from(Evidence)) or 0,
            "tool_calls": session.scalar(select(func.count()).select_from(ToolCall)) or 0,
            "run_events": session.scalar(select(func.count()).select_from(RunEvent)) or 0,
            "memory_items": session.scalar(select(func.count()).select_from(MemoryItem)) or 0,
            "artifacts": session.scalar(select(func.count()).select_from(Artifact)) or 0,
            "artifact_versions": session.scalar(select(func.count()).select_from(ArtifactVersion)) or 0,
            "notes": (session.scalar(select(func.count()).select_from(Note)) or 0) if "notes" in available_tables else None,
            "note_versions": (session.scalar(select(func.count()).select_from(NoteVersion)) or 0) if "note_versions" in available_tables else None,
            "background_tasks": session.scalar(select(func.count()).select_from(BackgroundTask)) or 0,
            "provider_calls": session.scalar(select(func.count()).select_from(ProviderCall)) or 0,
        }
