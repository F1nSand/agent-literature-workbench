"""Real parser/reader integration coverage with provider calls replaced locally."""
import hashlib
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import fitz
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.v1 import documents as document_routes
from app.documents import pipeline
from app.documents.models import (
    ContentUnit,
    ContentUnitEmbedding,
    Document,
    DocumentPage,
    DocumentVersion,
    EmbeddingCache,
    IndexGeneration,
)
from app.main import app


EPUB_CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
 <rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
EPUB_PACKAGE = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">urn:test:book</dc:identifier><dc:title>Native chapter</dc:title></metadata>
 <manifest><item id="chapter1" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="chapter1"/></spine>
</package>"""


def _write_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", EPUB_CONTAINER)
        archive.writestr("OPS/package.opf", EPUB_PACKAGE)
        archive.writestr(
            "OPS/Text/ch1.xhtml",
            "<html xmlns='http://www.w3.org/1999/xhtml'><body><h1>Chapter One</h1>"
            "<p>EPUB native parser preserves this chapter as searchable source text.</p>"
            "</body></html>",
        )


def _write_pdf(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(fitz.Rect(50, 50, 550, 740), content, fontsize=11)
    doc.save(path)
    doc.close()


def test_multiformat_ingestion_persists_native_locators_and_active_reader_output(monkeypatch, tmp_path: Path) -> None:
    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run PostgreSQL format ingestion integration test")

    from sqlalchemy import create_engine

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(pipeline, "get_engine", lambda: engine)
    monkeypatch.setattr(document_routes, "get_engine", lambda: engine)
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path), max_pdf_pages=100))

    source_paths: dict[str, Path] = {}
    pdf_paths: dict[str, Path] = {}
    rows: list[tuple[UUID, UUID, str, str]] = []
    document_ids: list[UUID] = []
    version_ids: list[UUID] = []
    cache_keys: set[str] = set()
    client = TestClient(app)

    class OfflineProvider:
        def __init__(self, *_args, **_kwargs):
            pass

        def embed(self, texts, *, model, dimensions):
            assert model == "Qwen/Qwen3-Embedding-4B"
            assert dimensions == 2560
            return [[0.0] * dimensions for _ in texts], SimpleNamespace(prompt_tokens=0)

    monkeypatch.setattr(pipeline, "SiliconFlowClient", OfflineProvider)

    def fake_docx_converter(source: Path, target: Path) -> Path:
        assert source.suffix == ".docx" and source.is_file()
        assert target.suffix == ".pdf"
        _write_pdf(target, ("Converted DOCX page has searchable native PDF text. " * 8).strip())
        return target

    monkeypatch.setattr(pipeline, "convert_docx_to_pdf", fake_docx_converter)
    monkeypatch.setattr(
        pipeline, "resolve_document_source_path",
        lambda _data_dir, key, kind="pdf": source_paths[key],
    )
    monkeypatch.setattr(
        pipeline, "resolve_document_pdf_path",
        lambda _data_dir, key: pdf_paths[key],
    )

    fixtures = [
        ("study.md", "# Overview\nMarkdown first paragraph has native text and stable paragraph anchors.\n\nSecond paragraph remains independently addressable."),
        ("study.txt", "TXT first paragraph has native text and stable paragraph anchors.\n\nSecond text paragraph remains independently addressable."),
        ("book.epub", "epub"),
        ("converted.docx", "docx"),
    ]
    try:
        with Session(engine) as session:
            generation = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
            assert generation is not None and generation.dimensions == 2560
            for filename, body in fixtures:
                document_id, version_id = uuid4(), uuid4()
                kind = Path(filename).suffix[1:]
                storage_key = uuid4().hex
                source_path = tmp_path / "sources" / f"{storage_key}.{kind}"
                pdf_path = tmp_path / "renditions" / f"{storage_key}.pdf"
                source_path.parent.mkdir(parents=True, exist_ok=True)
                if kind == "epub":
                    _write_epub(source_path)
                elif kind == "docx":
                    source_path.write_bytes(b"mocked DOCX bytes; converter is replaced by a real PDF rendition")
                else:
                    source_path.write_text(body, encoding="utf-8")
                source_paths[storage_key] = source_path
                pdf_paths[storage_key] = pdf_path
                document = Document(
                    id=document_id, title=Path(filename).stem, filename=filename,
                    storage_key=storage_key, sha256=hashlib.sha256((storage_key + "original").encode()).hexdigest(),
                    status="queued", page_count=0, active_version_id=None, is_deleted=False,
                )
                session.add(document)
                session.flush()
                version = DocumentVersion(
                    id=version_id, document_id=document_id, version_number=1, filename=filename,
                    storage_key=storage_key, sha256=hashlib.sha256((storage_key + "version").encode()).hexdigest(),
                    status="queued", page_count=0, processing_config_hash="a" * 64,
                )
                session.add(version)
                session.flush()
                document.active_version_id = version_id
                session.flush()
                document_ids.append(document_id)
                version_ids.append(version_id)
                rows.append((document_id, version_id, filename, storage_key))
            session.commit()

        results = {}
        for document_id, version_id, filename, _storage_key in rows:
            results[filename] = pipeline.process_document_ingestion(document_id, uuid4(), version_id)
            assert results[filename]["status"] == "ready"
            assert results[filename]["ocr_page_count"] == 0

        with Session(engine) as session:
            generation = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
            assert generation is not None
            for document_id, version_id, filename, _storage_key in rows:
                document = session.get(Document, document_id)
                version = session.get(DocumentVersion, version_id)
                pages = session.scalars(select(DocumentPage).where(
                    DocumentPage.document_id == document_id, DocumentPage.version_id == version_id,
                ).order_by(DocumentPage.page_number)).all()
                units = session.scalars(select(ContentUnit).where(
                    ContentUnit.document_id == document_id, ContentUnit.version_id == version_id,
                ).order_by(ContentUnit.page_number, ContentUnit.ordinal)).all()
                assert document is not None and document.status == "ready"
                assert document.active_version_id == version_id
                assert version is not None and version.status == "ready"
                assert document.page_count == version.page_count == len(pages)
                assert pages and units
                if filename.endswith(".epub"):
                    assert pages[0].locator == {"kind": "epub", "chapter": "OPS/Text/ch1.xhtml", "section": 1}
                    assert pages[0].location_label == "Chapter One"
                    assert "EPUB native parser" in pages[0].text
                    assert units[0].extra["source"] == "epub_text"
                elif filename.endswith((".md", ".txt")):
                    assert len(pages) == 2
                    assert pages[0].locator == {"kind": "text", "anchor": "p-1", "section": 1}
                    assert pages[1].locator == {"kind": "text", "anchor": "p-2", "section": 2}
                    assert all(unit.extra["source"] == "plain_text" for unit in units)
                else:
                    assert len(pages) == 1
                    assert pages[0].locator == {"kind": "page", "page": 1}
                    assert pages[0].location_label == "第 1 页（DOCX 转换版）"
                    assert "Converted DOCX page" in pages[0].text
                    assert units[0].extra["source"] == "pdf_text"
                assert all(unit.extra["locator"] == pages[unit.page_number - 1].locator for unit in units)
                assert all(unit.extra["location_label"] == pages[unit.page_number - 1].location_label for unit in units)
                assert all(unit.extra["is_derived"] is False and unit.extra["quote_eligible"] is True for unit in units)
                cache_keys.update(
                    pipeline._embedding_cache_key(unit.text, "content", generation.model, unit.text_sha256)
                    for unit in units
                )

        # The normal active-version reader endpoints expose the parsed location and searchable source.
        for document_id, version_id, filename, _storage_key in rows:
            summary = client.get(f"/api/v1/documents/{document_id}")
            assert summary.status_code == 200, summary.text
            assert summary.json()["status"] == "ready"
            assert summary.json()["active_version_id"] == str(version_id)
            assert summary.json()["page_count"] == results[filename]["page_count"]
            page = client.get(f"/api/v1/documents/{document_id}/pages/1")
            assert page.status_code == 200, page.text
            assert page.json()["version_id"] == str(version_id)
            assert page.json()["locator"]["kind"] in {"page", "text", "epub"}
            assert page.json()["content_units"]
            assert page.json()["content_units"][0]["quote_eligible"] is True
            assert page.json()["content_units"][0]["is_derived"] is False

    finally:
        with Session(engine) as session:
            if version_ids:
                units = session.scalars(select(ContentUnit).where(ContentUnit.version_id.in_(version_ids))).all()
                generation = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
                if generation is not None:
                    cache_keys.update(
                        pipeline._embedding_cache_key(unit.text, "content", generation.model, unit.text_sha256)
                        for unit in units
                    )
                unit_ids = [unit.id for unit in units]
                if unit_ids:
                    session.execute(delete(ContentUnitEmbedding).where(ContentUnitEmbedding.content_unit_id.in_(unit_ids)))
                session.execute(delete(ContentUnit).where(ContentUnit.version_id.in_(version_ids)))
                session.execute(delete(DocumentPage).where(DocumentPage.version_id.in_(version_ids)))
                for document in session.scalars(select(Document).where(Document.id.in_(document_ids))).all():
                    document.active_version_id = None
                session.flush()
                session.execute(delete(Document).where(Document.id.in_(document_ids)))
            if cache_keys:
                session.execute(delete(EmbeddingCache).where(EmbeddingCache.cache_key.in_(cache_keys)))
            session.commit()
        engine.dispose()
