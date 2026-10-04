from uuid import UUID
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.documents.models import Document, DocumentPage, DocumentVersion
from app.documents.formats import file_format


def validate_document_locator(session: Session, document: Document | UUID, version: DocumentVersion | UUID, locator: dict) -> DocumentPage:
    doc = document if isinstance(document, Document) else session.get(Document, document)
    ver = version if isinstance(version, DocumentVersion) else session.get(DocumentVersion, version)
    if doc is None or doc.is_deleted or ver is None or ver.document_id != doc.id or ver.status != "ready":
        raise ValueError("文献或阅读版本暂不可用。")
    if not isinstance(locator, dict):
        raise ValueError("阅读位置无效。")
    kind = file_format(ver.filename)
    rows = session.scalars(select(DocumentPage).where(DocumentPage.version_id == ver.id)).all()
    if kind in {"pdf", "docx"} and locator.get("kind") == "page":
        n = locator.get("page", locator.get("page_number"))
        if isinstance(n, int) and not isinstance(n, bool):
            row = next((p for p in rows if p.page_number == n), None)
            if row:
                return row
    elif kind == "epub" and locator.get("kind") == "epub":
        chapter = locator.get("chapter")
        row = next((p for p in rows if p.locator.get("chapter") == chapter), None)
        if row and ("section" not in locator or locator["section"] == row.page_number):
            cfi = locator.get("cfi")
            if cfi is not None and (not isinstance(cfi, str) or not cfi.startswith("epubcfi(") or len(cfi) > 2000):
                raise ValueError("EPUB CFI 无效。")
            return row
    elif kind in {"md", "txt"} and locator.get("kind") == "text":
        anchor = locator.get("anchor")
        row = next((p for p in rows if p.locator.get("anchor") == anchor), None)
        if row and ("section" not in locator or locator["section"] == row.page_number):
            return row
    raise ValueError("文献位置不存在或不匹配当前格式。")
