from datetime import datetime, timezone
from uuid import UUID
from urllib.parse import quote

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.documents.models import Document, DocumentVersion
from app.notes.content import plain_text, validate_content
from app.notes.models import Note, NoteVersion
from app.runs.models import Evidence, Run


class NoteServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, current_revision: int | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.current_revision = current_revision
        super().__init__(message)


def _require_note(session: Session, note_id: UUID, *, lock: bool = False) -> Note:
    query = select(Note).where(Note.id == note_id)
    if lock:
        query = query.with_for_update()
    row = session.scalar(query)
    if row is None:
        raise NoteServiceError(404, "note_not_found", "笔记不存在")
    return row


def _version_number(session: Session, note_id: UUID) -> int:
    return (session.scalar(select(func.max(NoteVersion.version_number)).where(NoteVersion.note_id == note_id)) or 0) + 1


def _new_version(session: Session, note: Note, content: dict, *, origin: str, status: str,
                 parent_version_id: UUID | None, source_run_id: UUID | None = None,
                 idempotency_key: str | None = None, base_revision: int | None = None) -> NoteVersion:
    content = validate_content(content)
    version = NoteVersion(
        note_id=note.id, version_number=_version_number(session, note.id),
        parent_version_id=parent_version_id, origin=origin, status=status,
        content=content, plain_text=plain_text(content), source_run_id=source_run_id,
        idempotency_key=idempotency_key,
        base_revision=base_revision,
        adopted_at=datetime.now(timezone.utc) if status == "current" else None,
    )
    session.add(version)
    session.flush()
    return version


def note_read(session: Session, note: Note) -> dict:
    version = session.get(NoteVersion, note.current_version_id) if note.current_version_id else None
    if version is None:
        raise NoteServiceError(409, "current_version_unavailable", "笔记当前版本不可用", note.revision)
    return {
        "id": note.id, "title": derive_title(version.content), "revision": note.revision,
        "current_version_id": version.id, "content": version.content,
        "plain_text": version.plain_text, "archived_at": note.archived_at,
        "created_at": note.created_at, "updated_at": note.updated_at,
    }


def derive_title(content: dict) -> str:
    """Use the first non-empty line as the display title without editing the body."""
    lines = plain_text(content).splitlines()
    first = lines[0].strip() if lines else ""
    return first[:500] if first else "未命名笔记"


def create_note(session: Session, *, title: str | None = None, content: dict, idempotency_key: str | None = None) -> tuple[Note, bool]:
    content = validate_content(content)
    title = derive_title(content)
    if idempotency_key:
        inserted = session.execute(pg_insert(Note).values(title=title, revision=1, idempotency_key=idempotency_key)
                                   .on_conflict_do_nothing(index_elements=[Note.idempotency_key]).returning(Note.id)).scalar_one_or_none()
        if inserted is None:
            existing = session.scalar(select(Note).where(Note.idempotency_key == idempotency_key).with_for_update())
            if existing is not None:
                return existing, False
            raise NoteServiceError(409, "idempotency_conflict", "请求幂等键已被使用")
        note = session.get(Note, inserted)
    else:
        note = Note(title=title, revision=1)
        session.add(note)
        session.flush()
    version = _new_version(session, note, content, origin="user", status="current", parent_version_id=None)
    note.current_version_id = version.id
    session.flush()
    return note, True


def list_notes(session: Session, *, limit: int, offset: int, include_archived: bool = False) -> tuple[list[Note], bool]:
    query = select(Note)
    if not include_archived:
        query = query.where(Note.archived_at.is_(None))
    rows = session.scalars(query.order_by(Note.updated_at.desc(), Note.id).offset(offset).limit(limit + 1)).all()
    return rows[:limit], len(rows) > limit


def get_note(session: Session, note_id: UUID) -> Note:
    return _require_note(session, note_id)


def update_note(session: Session, note_id: UUID, *, expected_revision: int, title: str | None = None,
                content: dict | None = None) -> Note:
    note = _require_note(session, note_id, lock=True)
    if note.archived_at is not None:
        raise NoteServiceError(409, "note_archived", "已归档的笔记不能修改", note.revision)
    if note.revision != expected_revision:
        raise NoteServiceError(409, "revision_conflict", "笔记已更新，请刷新后重试", note.revision)
    if content is not None:
        current = session.get(NoteVersion, note.current_version_id) if note.current_version_id else None
        if current is None:
            raise NoteServiceError(409, "current_version_unavailable", "笔记当前版本不可用", note.revision)
        current.status = "superseded"
        version = _new_version(session, note, content, origin="user", status="current", parent_version_id=current.id)
        note.current_version_id = version.id
        note.title = derive_title(version.content)
    elif title is not None:
        # Title-only updates from older clients cannot desynchronize the derived title.
        current = session.get(NoteVersion, note.current_version_id) if note.current_version_id else None
        if current is None:
            raise NoteServiceError(409, "current_version_unavailable", "笔记当前版本不可用", note.revision)
        note.title = derive_title(current.content)
    note.revision += 1
    session.flush()
    return note


def delete_note(session: Session, note_id: UUID) -> None:
    """Permanently remove a note and its immutable versions after serializing writers."""
    note = _require_note(session, note_id, lock=True)
    # These FKs are intentionally cyclic/owner-scoped. Detach pointers before
    # deleting versions; the surrounding transaction makes the operation atomic.
    note.current_version_id = None
    session.flush()
    session.execute(update(NoteVersion).where(NoteVersion.note_id == note_id).values(parent_version_id=None))
    session.flush()
    session.execute(NoteVersion.__table__.delete().where(NoteVersion.note_id == note_id))
    session.flush()
    session.delete(note)
    session.flush()


def list_versions(session: Session, note_id: UUID) -> list[NoteVersion]:
    _require_note(session, note_id)
    return session.scalars(select(NoteVersion).where(NoteVersion.note_id == note_id).order_by(NoteVersion.version_number.desc())).all()


def _validate_source_run(session: Session, run_id: UUID, *, allow_running_source: bool = False) -> None:
    run = session.get(Run, run_id)
    if run is None:
        raise NoteServiceError(422, "invalid_source_run", "来源运行不存在")
    if allow_running_source and run.status == "running":
        # Internal agent callers must validate the active lease and page context
        # before using this path. Public HTTP requests never receive this flag.
        return
    if run.status != "completed":
        raise NoteServiceError(422, "invalid_source_run", "候选来源运行尚未正常完成")
    evidence = session.scalar(
        select(Evidence.id).join(Document, Document.id == Evidence.document_id)
        .join(DocumentVersion, DocumentVersion.id == Evidence.version_id)
        .where(Evidence.run_id == run_id, Evidence.read_at.is_not(None), Evidence.validated_at.is_not(None),
               Evidence.invalidated_at.is_(None), Document.is_deleted.is_(False),
               DocumentVersion.document_id == Evidence.document_id, DocumentVersion.status == "ready")
        .limit(1)
    )
    if evidence is None:
        raise NoteServiceError(422, "invalid_source_run", "来源运行没有有效的文献证据")


def create_candidate(session: Session, note_id: UUID, *, expected_revision: int, content: dict,
                     source_run_id: UUID | None = None, idempotency_key: str | None = None,
                     allow_running_source: bool = False) -> tuple[Note, NoteVersion, bool]:
    note = _require_note(session, note_id, lock=True)
    if idempotency_key:
        existing = session.scalar(select(NoteVersion).where(NoteVersion.note_id == note_id,
                                         NoteVersion.idempotency_key == idempotency_key))
        if existing is not None:
            return note, existing, False
    if note.archived_at is not None:
        raise NoteServiceError(409, "note_archived", "已归档的笔记不能生成候选", note.revision)
    if note.revision != expected_revision:
        raise NoteServiceError(409, "revision_conflict", "笔记已更新，请刷新后重试", note.revision)
    if source_run_id is not None:
        _validate_source_run(session, source_run_id, allow_running_source=allow_running_source)
    version = _new_version(session, note, content, origin="generated", status="candidate",
                            parent_version_id=note.current_version_id, source_run_id=source_run_id,
                            idempotency_key=idempotency_key, base_revision=expected_revision)
    return note, version, True


def adopt_candidate(session: Session, note_id: UUID, *, expected_revision: int, version_id: UUID) -> Note:
    note = _require_note(session, note_id, lock=True)
    if note.archived_at is not None:
        raise NoteServiceError(409, "note_archived", "已归档的笔记不能采纳候选", note.revision)
    if note.revision != expected_revision:
        raise NoteServiceError(409, "revision_conflict", "笔记已更新，请刷新后重试", note.revision)
    candidate = session.scalar(select(NoteVersion).where(NoteVersion.id == version_id, NoteVersion.note_id == note_id).with_for_update())
    if candidate is None or candidate.status != "candidate":
        raise NoteServiceError(409, "candidate_unavailable", "候选版本已变化或不属于此笔记", note.revision)
    if candidate.base_revision is None:
        raise NoteServiceError(409, "candidate_requires_preview", "历史候选没有可验证的基础版本，请先重新生成候选预览", note.revision)
    if candidate.base_revision != expected_revision or candidate.parent_version_id != note.current_version_id:
        raise NoteServiceError(409, "candidate_base_changed", "当前版本已变化，请重新生成候选", note.revision)
    current = session.get(NoteVersion, note.current_version_id) if note.current_version_id else None
    if current is not None:
        current.status = "superseded"
    candidate.status = "current"
    candidate.adopted_at = datetime.now(timezone.utc)
    note.current_version_id = candidate.id
    note.title = derive_title(candidate.content)
    note.revision += 1
    session.flush()
    return note


def render_markdown(title: str, version: NoteVersion) -> str:
    def inline(node: dict) -> str:
        if node["type"] == "hardBreak":
            return "  \n"
        value = node.get("text", "")
        for mark in node.get("marks", []):
            kind = mark["type"]
            if kind == "bold": value = f"**{value}**"
            elif kind == "italic": value = f"*{value}*"
            elif kind == "strike": value = f"~~{value}~~"
            elif kind == "code": value = f"`{value}`"
            elif kind == "link":
                safe_url = quote(mark["attrs"]["href"], safe=":/?#[]@!$&'*,;=%+")
                value = f"[{value}](<{safe_url}>)"
        return value
    def block(node: dict, depth: int = 0) -> str:
        kind = node["type"]
        children = node.get("content", [])
        if kind in {"paragraph", "heading", "codeBlock"}:
            value = "".join(inline(child) for child in children)
            if kind == "heading": return "#" * node.get("attrs", {}).get("level", 1) + " " + value
            if kind == "codeBlock": return "```" + node.get("attrs", {}).get("language", "") + "\n" + value + "\n```"
            return value
        if kind == "horizontalRule": return "---"
        if kind == "blockquote": return "\n".join("> " + block(child, depth) for child in children)
        if kind in {"bulletList", "orderedList"}:
            start = node.get("attrs", {}).get("start", 1)
            items = []
            for index, child in enumerate(children):
                prefix = "- " if kind == "bulletList" else f"{start + index}. "
                rendered = "\n".join(block(part, depth + 1) for part in child.get("content", []))
                items.append("  " * depth + prefix + rendered.replace("\n", "\n" + "  " * (depth + 1)))
            return "\n".join(items)
        if kind == "listItem": return "\n".join(block(child, depth) for child in children)
        return ""
    body = "\n\n".join(block(item) for item in version.content.get("content", []))
    return f"# {title}\n\n{body}\n"
