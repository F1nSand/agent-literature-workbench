from pathlib import Path
import zipfile
from xml.etree.ElementTree import ParseError
from urllib.parse import unquote

import pytest
from pypdf import PdfWriter

from app.documents import formats
from app.documents.formats import read_sections, validate_source


def _epub(path: Path, *, html: str | None = None, chapter_href: str = "Text/chapter.xhtml") -> None:
    package = '''<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <manifest><item id="chapter" href="Text/chapter.xhtml" media-type="application/xhtml+xml"/></manifest>
      <spine><itemref idref="chapter"/></spine>
    </package>'''.replace("Text/chapter.xhtml", chapter_href)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/container.xml", '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''')
        archive.writestr("OPS/package.opf", package)
        if not chapter_href.startswith("../"):
            archive.writestr(f"OPS/{unquote(chapter_href.split('#')[0])}", html or "<html><body><h1>Chapter One</h1><p>Visible chapter text.</p><script>secret()</script></body></html>")


def _set_zip_encrypted_flag(path: Path) -> None:
    data = bytearray(path.read_bytes())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        cursor = 0
        while True:
            cursor = data.find(signature, cursor)
            if cursor < 0:
                break
            flags = int.from_bytes(data[cursor + flag_offset:cursor + flag_offset + 2], "little")
            data[cursor + flag_offset:cursor + flag_offset + 2] = (flags | 1).to_bytes(2, "little")
            cursor += len(signature)
    path.write_bytes(data)


def test_generated_pdf_epub_docx_markdown_and_text_adapters(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with pdf_path.open("wb") as stream:
        writer.write(stream)
    assert validate_source(pdf_path, "pdf", max_pages=4) == 1

    epub_path = tmp_path / "sample.epub"
    _epub(epub_path)
    sections = read_sections(epub_path, "epub")
    assert len(sections) == 1
    assert sections[0].locator == {"kind": "epub", "chapter": "OPS/Text/chapter.xhtml", "section": 1}
    assert "Visible chapter text." in sections[0].text
    assert "secret()" not in sections[0].text
    assert validate_source(epub_path, "epub", max_pages=4) == 1

    docx_path = tmp_path / "sample.docx"
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "<w:document xmlns:w='urn:w'><w:body><w:p/></w:body></w:document>")
    assert validate_source(docx_path, "docx", max_pages=4) == 0  # converted pagination is determined later

    markdown = tmp_path / "sample.md"
    markdown.write_text("# First heading\n\nFirst paragraph.\n\n## Second heading\nMore text.", encoding="utf-8")
    md_sections = read_sections(markdown, "md")
    assert [section.locator["anchor"] for section in md_sections] == ["p-1", "p-2", "p-3"]
    assert [section.label for section in md_sections] == ["First heading", "First paragraph.", "Second heading"]
    assert validate_source(markdown, "md", max_pages=4) == 3

    text_file = tmp_path / "sample.txt"
    text_file.write_text("第一段\n\n第二段", encoding="utf-8")
    text_sections = read_sections(text_file, "txt")
    assert [section.locator["anchor"] for section in text_sections] == ["p-1", "p-2"]
    assert validate_source(text_file, "txt", max_pages=4) == 2


def test_encrypted_malformed_and_bounded_archives_are_rejected(tmp_path: Path, monkeypatch) -> None:
    encrypted_pdf = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    with encrypted_pdf.open("wb") as stream:
        writer.write(stream)
    with pytest.raises(ValueError, match="有密码"):
        validate_source(encrypted_pdf, "pdf", max_pages=4)

    encrypted_epub = tmp_path / "encrypted.epub"
    _epub(encrypted_epub)
    _set_zip_encrypted_flag(encrypted_epub)
    with pytest.raises(ValueError, match="加密"):
        read_sections(encrypted_epub, "epub")

    bomb = tmp_path / "expanded.epub"
    _epub(bomb)
    monkeypatch.setattr(formats, "MAX_EXPANDED_BYTES", 32)
    with pytest.raises(ValueError, match="过大"):
        read_sections(bomb, "epub")
    bomb_docx = tmp_path / "expanded.docx"
    with zipfile.ZipFile(bomb_docx, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 64)
    with pytest.raises(ValueError, match="过大"):
        validate_source(bomb_docx, "docx", max_pages=4)

    malformed = tmp_path / "malformed.epub"
    malformed.write_bytes(b"this is not a zip archive")
    with pytest.raises(zipfile.BadZipFile):
        read_sections(malformed, "epub")

    invalid_xml = tmp_path / "invalid-xml.epub"
    with zipfile.ZipFile(invalid_xml, "w") as archive:
        archive.writestr("META-INF/container.xml", "<container><rootfile")
    with pytest.raises(ParseError):
        read_sections(invalid_xml, "epub")


def test_paragraph_anchors_are_stable_and_text_binary_data_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "notes.markdown"
    source.write_bytes("A\r\n\r\nB\r\n\r\nC".encode("utf-8"))
    sections = read_sections(source, "md")
    assert [(item.number, item.locator["anchor"], item.text) for item in sections] == [
        (1, "p-1", "A"), (2, "p-2", "B"), (3, "p-3", "C"),
    ]

    source.write_bytes(b"text\x00binary")
    with pytest.raises(ValueError, match="二进制"):
        read_sections(source, "md")


def test_epub_spine_path_traversal_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "traversal.epub"
    _epub(path, chapter_href="../../../outside.xhtml")
    with pytest.raises(ValueError, match="路径无效"):
        read_sections(path, "epub")


def test_epub_uri_encoded_chinese_chapter_has_a_real_source_locator(tmp_path: Path) -> None:
    path = tmp_path / "chinese.epub"
    _epub(path, chapter_href="Text/%E7%AC%AC%E4%B8%80%E7%AB%A0.xhtml#start")
    section = read_sections(path, "epub")[0]
    assert section.locator["chapter"] == "OPS/Text/第一章.xhtml"
    assert "Visible chapter text." in section.text
