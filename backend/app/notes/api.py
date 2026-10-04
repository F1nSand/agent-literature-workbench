from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_engine
from app.notes.models import NoteVersion
from app.notes.schemas import CandidateCreate, NoteAdopt, NoteCreate, NoteList, NotePreview, NoteRead, NoteUpdate, NoteVersionList, NoteVersionRead
from app.notes.service import (
    NoteServiceError, adopt_candidate, create_candidate, create_note, delete_note, derive_title,
    get_note, list_notes, list_versions, note_read, render_markdown, update_note,
)

router = APIRouter(prefix="/notes", tags=["notes"])


def _error(exc: NoteServiceError) -> HTTPException:
    detail = {"code": exc.code, "message": exc.message}
    if exc.current_revision is not None:
        detail["current_revision"] = exc.current_revision
    return HTTPException(status_code=exc.status_code, detail=detail)


@router.get("", response_model=NoteList)
def get_notes(limit: int = Query(default=50, ge=1, le=100), cursor: str | None = None,
              include_archived: bool = False) -> NoteList:
    try:
        offset = int(cursor) if cursor is not None else 0
        if offset < 0 or offset > 2_000_000:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="分页游标无效") from None
    try:
        with Session(get_engine()) as session:
            rows, more = list_notes(session, limit=limit, offset=offset, include_archived=include_archived)
            items = [NoteRead.model_validate(note_read(session, row)) for row in rows]
            return NoteList(items=items, next_cursor=str(offset + len(rows)) if more else None)
    except NoteServiceError as exc:
        raise _error(exc) from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法读取笔记") from None


@router.post("", response_model=NoteRead, status_code=status.HTTP_201_CREATED)
def post_note(payload: NoteCreate) -> NoteRead:
    try:
        with Session(get_engine()) as session, session.begin():
            note, _ = create_note(session, title=payload.title, content=payload.content, idempotency_key=payload.idempotency_key)
            result = note_read(session, note)
        return NoteRead.model_validate(result)
    except NoteServiceError as exc:
        raise _error(exc) from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法保存笔记") from None


@router.get("/{note_id}", response_model=NoteRead)
def read_note(note_id: UUID) -> NoteRead:
    try:
        with Session(get_engine()) as session:
            return NoteRead.model_validate(note_read(session, get_note(session, note_id)))
    except NoteServiceError as exc:
        raise _error(exc) from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法读取笔记") from None


@router.patch("/{note_id}", response_model=NoteRead)
def patch_note(note_id: UUID, payload: NoteUpdate) -> NoteRead:
    try:
        with Session(get_engine()) as session, session.begin():
            note = update_note(session, note_id, expected_revision=payload.expected_revision,
                               title=payload.title if "title" in payload.model_fields_set else None,
                               content=payload.content if "content" in payload.model_fields_set else None)
            result = note_read(session, note)
        return NoteRead.model_validate(result)
    except NoteServiceError as exc:
        raise _error(exc) from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法保存笔记") from None


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note_route(note_id: UUID) -> Response:
    try:
        with Session(get_engine()) as session, session.begin():
            delete_note(session, note_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except NoteServiceError as exc:
        raise _error(exc) from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法删除笔记") from None


@router.get("/{note_id}/versions", response_model=NoteVersionList)
def get_versions(note_id: UUID) -> NoteVersionList:
    try:
        with Session(get_engine()) as session:
            return NoteVersionList(items=[NoteVersionRead.model_validate(row) for row in list_versions(session, note_id)])
    except NoteServiceError as exc:
        raise _error(exc) from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法读取笔记版本") from None


@router.post("/{note_id}/candidates", response_model=NotePreview, status_code=status.HTTP_201_CREATED)
def post_candidate(note_id: UUID, payload: CandidateCreate) -> NotePreview:
    try:
        with Session(get_engine()) as session, session.begin():
            note, candidate, _ = create_candidate(session, note_id, expected_revision=payload.expected_revision,
                content=payload.content, source_run_id=payload.source_run_id, idempotency_key=payload.idempotency_key)
            result = NotePreview(note_id=note.id, base_revision=candidate.base_revision or payload.expected_revision,
                                 candidate=NoteVersionRead.model_validate(candidate))
        return result
    except NoteServiceError as exc:
        raise _error(exc) from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法保存笔记候选") from None


@router.post("/{note_id}/adopt", response_model=NoteRead)
def post_adopt(note_id: UUID, payload: NoteAdopt) -> NoteRead:
    try:
        with Session(get_engine()) as session, session.begin():
            note = adopt_candidate(session, note_id, expected_revision=payload.expected_revision, version_id=payload.version_id)
            result = note_read(session, note)
        return NoteRead.model_validate(result)
    except NoteServiceError as exc:
        raise _error(exc) from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法采纳笔记候选") from None


@router.get("/{note_id}/export")
def export_note(note_id: UUID, format: str = Query(default="markdown", pattern="^markdown$")) -> Response:
    try:
        with Session(get_engine()) as session:
            note = get_note(session, note_id)
            version = session.get(NoteVersion, note.current_version_id)
            if version is None or version.status != "current":
                raise NoteServiceError(409, "current_version_unavailable", "笔记当前版本不可用", note.revision)
            content = render_markdown(derive_title(version.content), version)
            return Response(content, media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": f'attachment; filename="note-{note.id}.md"',
                                     "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except NoteServiceError as exc:
        raise _error(exc) from None
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="暂时无法导出笔记") from None
