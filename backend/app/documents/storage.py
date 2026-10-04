from __future__ import annotations

import re
from pathlib import Path


_STORAGE_KEY_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


def resolve_document_source_path(data_dir: str | Path, storage_key: str, file_format: str = "pdf") -> Path:
    """Confine immutable originals and reading renditions to library storage."""
    if file_format not in {"pdf", "epub", "docx", "md", "txt"}:
        raise ValueError("unsupported_document_format")
    if not isinstance(storage_key, str) or _STORAGE_KEY_PATTERN.fullmatch(storage_key) is None:
        raise ValueError("invalid_document_storage_key")

    root = Path(data_dir).expanduser().resolve()
    expected_document_dir = root / "documents"
    document_dir = expected_document_dir.resolve()
    if document_dir != expected_document_dir:
        raise ValueError("invalid_document_storage_directory")

    pdf_path = (document_dir / f"{storage_key}.{file_format}").resolve()
    if pdf_path.parent != document_dir:
        raise ValueError("invalid_document_storage_path")
    return pdf_path


def resolve_document_pdf_path(data_dir: str | Path, storage_key: str) -> Path:
    return resolve_document_source_path(data_dir, storage_key, "pdf")
