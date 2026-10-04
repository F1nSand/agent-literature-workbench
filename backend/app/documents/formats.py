"""Local, bounded format adapters. Screen pagination is never an evidence locator."""
from __future__ import annotations

import posixpath
import re
import shutil
import subprocess
import tempfile
import zipfile
import os
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET
from urllib.parse import unquote, urlsplit

FORMATS = {"pdf", "epub", "docx", "md", "txt"}
MEDIA_TYPES = {"pdf": "application/pdf", "epub": "application/epub+zip", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "md": "text/plain; charset=utf-8", "txt": "text/plain; charset=utf-8"}
MAX_EXPANDED_BYTES = 200 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000


@dataclass(frozen=True)
class SourceSection:
    number: int
    label: str
    text: str
    locator: dict


def file_format(filename: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    return "md" if suffix == "markdown" else suffix


def _bounded_zip(path: Path) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(path)
    entries = archive.infolist()
    if len(entries) > 10000 or sum(x.file_size for x in entries) > MAX_EXPANDED_BYTES:
        archive.close()
        raise ValueError("压缩文档展开后过大。")
    if any(x.flag_bits & 1 for x in entries):
        archive.close()
        raise ValueError("不支持加密文档。")
    return archive


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocked = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.blocked += 1
        if not self.blocked and tag in {"p", "div", "br", "h1", "h2", "h3", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.blocked = max(0, self.blocked - 1)
        elif not self.blocked and tag in {"p", "div", "h1", "h2", "h3", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.blocked:
            self.parts.append(data)


def read_sections(path: Path, kind: str) -> list[SourceSection]:
    if kind in {"md", "txt"}:
        raw = path.read_bytes()
        if b"\x00" in raw:
            raise ValueError("文本包含二进制数据，请使用 UTF-8 或 GB18030 编码。")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gb18030")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError("文本过长。")
        paragraphs = [x.strip() for x in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if x.strip()]
        # Split very long unbroken paragraphs without tying anchors to the viewport.
        chunks = [p[i:i + 12000] for p in paragraphs for i in range(0, len(p), 12000)]
        return [SourceSection(i, re.sub(r"^#+\s*", "", p.splitlines()[0])[:100], p,
                              {"kind": "text", "anchor": f"p-{i}", "section": i})
                for i, p in enumerate(chunks, 1)]
    if kind != "epub":
        raise ValueError("此格式没有章节适配器。")
    with _bounded_zip(path) as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        if rootfile is None:
            raise ValueError("EPUB 缺少包目录。")
        package_path = rootfile.attrib["full-path"]
        package = ET.fromstring(archive.read(package_path))
        base = posixpath.dirname(package_path)
        manifest = {item.attrib["id"]: item.attrib.get("href", "") for item in package.findall(".//{*}manifest/{*}item")}
        sections = []
        total = 0
        for item in package.findall(".//{*}spine/{*}itemref"):
            href = manifest.get(item.attrib.get("idref"))
            if not href:
                continue
            href_url = urlsplit(href)
            if href_url.scheme or href_url.netloc:
                raise ValueError("EPUB 章节路径无效。")
            chapter = posixpath.normpath(posixpath.join(base, unquote(href_url.path)))
            if chapter.startswith("../") or chapter.startswith("/"):
                raise ValueError("EPUB 章节路径无效。")
            html = archive.read(chapter).decode("utf-8-sig")
            parser = _TextParser()
            parser.feed(html)
            text = re.sub(r"\n[ \t]*\n+", "\n\n", "".join(parser.parts)).strip()
            total += len(text)
            if total > MAX_TEXT_CHARS:
                raise ValueError("EPUB 正文过长。")
            if not text:
                continue
            number = len(sections) + 1
            sections.append(SourceSection(number, text.splitlines()[0][:100], text,
                                          {"kind": "epub", "chapter": chapter, "section": number}))
        if not sections:
            raise ValueError("EPUB 没有可读取章节。")
        return sections


def validate_source(path: Path, kind: str, max_pages: int) -> int:
    if kind == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            raise ValueError("暂不支持有密码的 PDF，请先解密。")
        count = len(reader.pages)
        if not 1 <= count <= max_pages:
            raise ValueError(f"PDF 需包含 1–{max_pages} 页。")
        return count
    if kind == "docx":
        with _bounded_zip(path) as archive:
            ET.fromstring(archive.read("word/document.xml"))
        return 0  # Physical pages become known only after local conversion.
    count = len(read_sections(path, kind))
    if not 1 <= count <= 5000:
        raise ValueError("文献需包含 1–5000 个可读取段落或章节。")
    return count


def convert_docx_to_pdf(source: Path, target: Path) -> Path:
    if target.is_file():
        return target
    converter_url = os.getenv("DOCX_CONVERTER_URL", "").strip()
    if converter_url:
        import httpx

        max_input_bytes = 100 * 1024 * 1024
        max_output_bytes = 200 * 1024 * 1024
        try:
            with source.open("rb") as stream:
                payload = stream.read(max_input_bytes + 1)
            if len(payload) > max_input_bytes:
                raise ValueError("docx_conversion_failed")
            response = httpx.post(
                converter_url.rstrip("/") + "/convert",
                content=payload,
                headers={"Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
                timeout=httpx.Timeout(110.0, connect=5.0, write=15.0, pool=5.0),
                follow_redirects=False,
            )
            response.raise_for_status()
            pdf_bytes = response.content
            if not pdf_bytes.startswith(b"%PDF-") or len(pdf_bytes) > max_output_bytes:
                raise ValueError("docx_conversion_failed")
        except (OSError, httpx.HTTPError) as exc:
            raise ValueError("docx_conversion_failed") from exc
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
                                             delete=False) as stream:
                temporary_path = Path(stream.name)
                stream.write(pdf_bytes)
            temporary_path.replace(target)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return target
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if executable is None:
        raise ValueError("docx_converter_unavailable")
    with tempfile.TemporaryDirectory(prefix="reading-docx-") as temp:
        temp_path = Path(temp)
        result = subprocess.run([executable, f"-env:UserInstallation={(temp_path / 'profile').as_uri()}",
                                 "--headless", "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(temp_path), str(source)],
                                capture_output=True, timeout=120, check=False)
        output = temp_path / f"{source.stem}.pdf"
        if result.returncode or not output.is_file():
            raise ValueError("docx_conversion_failed")
        shutil.copyfile(output, target.with_suffix(".pdf.tmp"))
        target.with_suffix(".pdf.tmp").replace(target)
    return target
