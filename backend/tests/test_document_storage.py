from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import documents
from app.documents.storage import resolve_document_pdf_path


def test_document_pdf_path_is_confined_to_library_directory(tmp_path: Path) -> None:
    storage_key = "a" * 32

    path = resolve_document_pdf_path(tmp_path, storage_key)

    assert path == (tmp_path.resolve() / "documents" / f"{storage_key}.pdf")
    assert path.parent == (tmp_path.resolve() / "documents")


@pytest.mark.parametrize(
    "storage_key",
    [
        "../outside",
        "..\\outside",
        "a" * 32 + "/../../outside",
        "a" * 31,
        "a" * 32 + "\x00",
    ],
)
def test_document_pdf_path_rejects_untrusted_or_traversal_keys(tmp_path: Path, storage_key: str) -> None:
    with pytest.raises(ValueError, match="invalid_document_storage_key"):
        resolve_document_pdf_path(tmp_path, storage_key)


def test_document_pdf_path_rejects_symlinked_library_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "documents").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="invalid_document_storage_directory"):
        resolve_document_pdf_path(tmp_path, "a" * 32)


def test_document_pdf_path_rejects_pdf_symlink_outside_library(tmp_path: Path) -> None:
    document_dir = tmp_path / "documents"
    document_dir.mkdir()
    outside_pdf = tmp_path / "outside.pdf"
    outside_pdf.write_bytes(b"private file")
    (document_dir / f"{'a' * 32}.pdf").symlink_to(outside_pdf)

    with pytest.raises(ValueError, match="invalid_document_storage_path"):
        resolve_document_pdf_path(tmp_path, "a" * 32)


def test_document_route_hides_invalid_storage_key_as_unavailable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(documents, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path)))

    with pytest.raises(HTTPException) as error:
        documents._storage_path("../outside")

    assert error.value.status_code == 404
    assert error.value.detail == "PDF file is unavailable"
