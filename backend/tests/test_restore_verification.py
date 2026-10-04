import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from app.operations.restore_verification import (
    RestoreVerificationError,
    verify_version_assets,
    verify_version_files,
)


def _pdf_bytes() -> bytes:
    import io

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_restore_verifier_accepts_matching_version_file_hash(tmp_path: Path) -> None:
    storage_key = "a" * 32
    content = _pdf_bytes()
    pdf_path = tmp_path / "documents" / f"{storage_key}.pdf"
    pdf_path.parent.mkdir()
    pdf_path.write_bytes(content)
    version = SimpleNamespace(storage_key=storage_key, filename="paper.pdf", status="ready", page_count=1,
                              sha256=hashlib.sha256(content).hexdigest())

    assert verify_version_files([version], tmp_path) == 1


@pytest.mark.parametrize(
    ("storage_key", "expected_hash", "expected_error"),
    [
        ("b" * 32, "0" * 64, "document version original file is missing"),
        ("../outside", "0" * 64, "document version storage key is invalid"),
    ],
)
def test_restore_verifier_rejects_missing_or_unsafe_files(
    tmp_path: Path, storage_key: str, expected_hash: str, expected_error: str
) -> None:
    version = SimpleNamespace(storage_key=storage_key, sha256=expected_hash)

    with pytest.raises(RestoreVerificationError, match=expected_error):
        verify_version_files([version], tmp_path)


def test_restore_verifier_rejects_checksum_mismatch(tmp_path: Path) -> None:
    storage_key = "c" * 32
    pdf_path = tmp_path / "documents" / f"{storage_key}.pdf"
    pdf_path.parent.mkdir()
    pdf_path.write_bytes(b"wrong PDF contents")
    version = SimpleNamespace(storage_key=storage_key, filename="paper.pdf", sha256="d" * 64)

    with pytest.raises(RestoreVerificationError, match="document version original file checksum mismatch"):
        verify_version_files([version], tmp_path)


def test_restore_verifier_uses_immutable_original_format_and_checks_docx_rendition(tmp_path: Path) -> None:
    storage_key = "e" * 32
    original = b"immutable docx bytes"
    root = tmp_path / "documents"
    root.mkdir()
    (root / f"{storage_key}.docx").write_bytes(original)
    rendition = _pdf_bytes()
    (root / f"{storage_key}.pdf").write_bytes(rendition)
    version = SimpleNamespace(
        id="version-1", storage_key=storage_key, filename="paper.docx", status="ready", page_count=1,
        sha256=hashlib.sha256(original).hexdigest(),
    )

    verified, pdf_files, docx_renditions = verify_version_assets([version], tmp_path)

    assert verified == 1
    assert pdf_files == 1
    assert docx_renditions == [{
        "version_id": "version-1", "storage_key": storage_key, "exists": True,
        "sha256": hashlib.sha256(rendition).hexdigest(), "page_count": 1,
    }]


def test_restore_verifier_rejects_missing_docx_rendition_and_wrong_page_count(tmp_path: Path) -> None:
    storage_key = "f" * 32
    original = b"immutable docx bytes"
    root = tmp_path / "documents"
    root.mkdir()
    (root / f"{storage_key}.docx").write_bytes(original)
    version = SimpleNamespace(storage_key=storage_key, filename="paper.docx", status="ready", page_count=1,
                              sha256=hashlib.sha256(original).hexdigest())
    with pytest.raises(RestoreVerificationError, match="DOCX PDF rendition is missing"):
        verify_version_assets([version], tmp_path)

    (root / f"{storage_key}.pdf").write_bytes(_pdf_bytes())
    version.page_count = 2
    with pytest.raises(RestoreVerificationError, match="DOCX PDF rendition page count mismatch"):
        verify_version_assets([version], tmp_path)


def test_restore_verifier_checks_markdown_original_extension(tmp_path: Path) -> None:
    storage_key = "1" * 32
    content = b"# Offline document"
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / f"{storage_key}.md").write_bytes(content)
    version = SimpleNamespace(storage_key=storage_key, filename="paper.md", status="ready",
                              sha256=hashlib.sha256(content).hexdigest())

    assert verify_version_files([version], tmp_path) == 1
