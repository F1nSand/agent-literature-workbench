from __future__ import annotations

import subprocess
import io
import socket
import threading
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from app.documents import formats
from converter import server


PDF = b"%PDF-1.7\nminimal test pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


@pytest.fixture
def converter_url():
    http_server = server.LimitedHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http_server.server_address[1]}"
    http_server.shutdown()
    http_server.server_close()
    thread.join(timeout=3)


def test_remote_converter_adapter_posts_bounded_bytes_and_atomically_saves_pdf(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.docx"
    source.write_bytes(b"test docx")
    target = tmp_path / "converted.pdf"
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, content=PDF, request=httpx.Request("POST", url))

    monkeypatch.setenv("DOCX_CONVERTER_URL", "http://docx-converter:8080/")
    monkeypatch.setattr(httpx, "post", fake_post)

    assert formats.convert_docx_to_pdf(source, target) == target
    assert target.read_bytes() == PDF
    assert not target.with_suffix(".pdf.tmp").exists()
    url, kwargs = calls[0]
    assert url == "http://docx-converter:8080/convert"
    assert kwargs["content"] == b"test docx"
    assert kwargs["headers"]["Content-Type"] == DOCX_MIME
    assert kwargs["follow_redirects"] is False
    assert kwargs["timeout"].read == 110.0


def test_remote_converter_rejects_bad_pdf_and_oversized_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.docx"
    target = tmp_path / "converted.pdf"
    source.write_bytes(b"test docx")
    monkeypatch.setenv("DOCX_CONVERTER_URL", "http://docx-converter:8080")
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: httpx.Response(
        200, content=b"not pdf", request=httpx.Request("POST", url)))
    with pytest.raises(ValueError, match="docx_conversion_failed"):
        formats.convert_docx_to_pdf(source, target)
    assert not target.exists()

    with source.open("wb") as stream:
        stream.truncate(100 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="docx_conversion_failed"):
        formats.convert_docx_to_pdf(source, target)


def test_converter_runs_isolated_libreoffice_with_fixed_filenames_and_timeout(tmp_path: Path, monkeypatch) -> None:
    observed = {}
    payload = _docx_bytes()

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        source = Path(command[-1])
        assert source.name == "document.docx"
        assert source.read_bytes() == payload
        output_dir = Path(command[command.index("--outdir") + 1])
        (output_dir / "document.pdf").write_bytes(PDF)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server.convert_docx(payload) == PDF
    assert observed["command"][0] == "/usr/bin/libreoffice"
    assert observed["kwargs"]["timeout"] == server.CONVERSION_TIMEOUT_SECONDS
    assert observed["kwargs"]["stdin"] == subprocess.DEVNULL


def test_converter_rejects_oversized_upload_without_spawning_libreoffice(monkeypatch) -> None:
    monkeypatch.setattr(server.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must reject before spawn"))
    with pytest.raises(ValueError, match="invalid upload size"):
        server.convert_docx(b"x" * (server.MAX_UPLOAD_BYTES + 1))


def test_converter_rejects_invalid_or_expanding_docx_before_spawning(monkeypatch):
    monkeypatch.setattr(server.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must reject before spawn"))
    with pytest.raises(ValueError, match="invalid document package"):
        server.convert_docx(b"not a zip")


def test_converter_http_endpoint_returns_pdf_from_mocked_libreoffice(converter_url, monkeypatch):
    payload = _docx_bytes()

    def fake_run(command, **kwargs):
        output_dir = Path(command[command.index("--outdir") + 1])
        (output_dir / "document.pdf").write_bytes(PDF)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    response = httpx.post(converter_url + "/convert", content=payload, headers={"Content-Type": DOCX_MIME})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == PDF


def test_converter_http_endpoint_rejects_oversized_content_length(converter_url):
    host, port = "127.0.0.1", int(converter_url.rsplit(":", 1)[1])
    with socket.create_connection((host, port), timeout=3) as client:
        client.sendall((f"POST /convert HTTP/1.0\r\nHost: {host}\r\n"
                        f"Content-Length: {server.MAX_UPLOAD_BYTES + 1}\r\n"
                        f"Content-Type: {DOCX_MIME}\r\n\r\n").encode("ascii"))
        response = client.recv(1024)
    assert b"413" in response


def test_converter_http_endpoint_returns_busy_during_active_conversion(converter_url, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    payload = _docx_bytes()

    def blocking_conversion(_payload):
        started.set()
        assert release.wait(3)
        return PDF

    monkeypatch.setattr(server, "convert_docx", blocking_conversion)
    result = {}

    def first_request():
        result["first"] = httpx.post(converter_url + "/convert", content=payload,
                                     headers={"Content-Type": DOCX_MIME})

    thread = threading.Thread(target=first_request)
    thread.start()
    try:
        assert started.wait(3)
        busy = httpx.post(converter_url + "/convert", content=payload, headers={"Content-Type": DOCX_MIME})
        assert busy.status_code == 503
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert result["first"].status_code == 200


def test_converter_http_server_caps_request_threads(converter_url):
    host, port = "127.0.0.1", int(converter_url.rsplit(":", 1)[1])
    sockets = []
    try:
        for _ in range(server.MAX_REQUEST_THREADS):
            client = socket.create_connection((host, port), timeout=3)
            client.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n")
            sockets.append(client)
        deadline = time.monotonic() + 2
        while server._request_slots._value and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server._request_slots._value == 0
        with socket.create_connection((host, port), timeout=3) as extra:
            extra.settimeout(2)
            extra.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
            try:
                assert extra.recv(64) == b""
            except ConnectionError:
                pass  # Some platforms report an immediate close as a reset.
    finally:
        for client in sockets:
            client.close()
