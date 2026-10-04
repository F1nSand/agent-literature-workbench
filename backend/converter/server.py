"""Small private HTTP sidecar for bounded DOCX-to-PDF conversion."""
from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
CONVERSION_TIMEOUT_SECONDS = 90
MAX_CONCURRENT_CONVERSIONS = 1
MAX_REQUEST_THREADS = 8
_conversion_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CONVERSIONS)
_request_slots = threading.BoundedSemaphore(MAX_REQUEST_THREADS)


def convert_docx(payload: bytes) -> bytes:
    if not payload or len(payload) > MAX_UPLOAD_BYTES:
        raise ValueError("invalid upload size")
    with tempfile.TemporaryDirectory(prefix="docx-convert-", dir=os.getenv("TMPDIR", "/tmp")) as directory:
        root = Path(directory)
        source = root / "document.docx"
        output_dir = root / "output"
        profile = root / "profile"
        output_dir.mkdir()
        source.write_bytes(payload)
        try:
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                if (len(entries) > 10000 or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES
                        or any(item.flag_bits & 1 for item in entries)
                        or "[Content_Types].xml" not in archive.namelist()
                        or "word/document.xml" not in archive.namelist()):
                    raise ValueError("invalid document package")
        except (zipfile.BadZipFile, OSError) as exc:
            raise ValueError("invalid document package") from exc
        result = subprocess.run(
            [
                "/usr/bin/libreoffice",
                f"-env:UserInstallation={profile.as_uri()}",
                "--headless",
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                str(output_dir),
                str(source),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=CONVERSION_TIMEOUT_SECONDS,
            check=False,
            close_fds=True,
        )
        output = output_dir / "document.pdf"
        if result.returncode or not output.is_file() or output.stat().st_size > MAX_PDF_BYTES:
            raise ValueError("conversion failed")
        pdf = output.read_bytes()
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("conversion failed")
        return pdf


class Handler(BaseHTTPRequestHandler):
    server_version = "DocxConverter"
    sys_version = ""

    def do_GET(self) -> None:
        if self.path != "/health":
            self._reply(404, b"not found", "text/plain")
            return
        self._reply(200, b"ok", "text/plain")

    def do_POST(self) -> None:
        if self.path != "/convert":
            self._reply(404, b"not found", "text/plain")
            return
        if self.headers.get("Transfer-Encoding"):
            self._reply(400, b"invalid request", "text/plain")
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._reply(411, b"content length required", "text/plain")
            return
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            self._reply(413, b"upload too large", "text/plain")
            return
        if self.headers.get_content_type() != "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            self._reply(415, b"unsupported media type", "text/plain")
            return
        if not _conversion_slots.acquire(blocking=False):
            self._reply(503, b"converter busy", "text/plain")
            return
        try:
            try:
                payload = self.rfile.read(size)
            except (TimeoutError, socket.timeout):
                self.close_connection = True
                return
            if len(payload) != size:
                self._reply(400, b"incomplete request", "text/plain")
                return
            try:
                pdf = convert_docx(payload)
            except (OSError, subprocess.SubprocessError, ValueError):
                self._reply(422, b"conversion failed", "text/plain")
                return
            self._reply(200, pdf, "application/pdf")
        finally:
            _conversion_slots.release()

    def log_message(self, format: str, *args) -> None:
        # Avoid logging request data or user-supplied filenames.
        return

    def _reply(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True


class LimitedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(20)
        return request, client_address

    def process_request(self, request, client_address):
        if not _request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            _request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except (TimeoutError, socket.timeout, ConnectionError):
            # Slow, incomplete, or disconnected requests are dropped without traceback output.
            pass
        except Exception:
            self.handle_error(request, client_address)
        finally:
            try:
                self.shutdown_request(request)
            finally:
                _request_slots.release()


if __name__ == "__main__":
    server = LimitedHTTPServer(("0.0.0.0", 8080), Handler)
    server.serve_forever(poll_interval=0.5)
