"""Scan saved Docker image layers for common private-data leakage patterns."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

try:
    from .release_audit import CREDENTIAL_PATTERNS, PERSONAL_PATH_PATTERNS, TEXT_EXTENSIONS, TEXT_FILENAMES
except ImportError:  # Running this file directly from the scripts directory.
    from release_audit import CREDENTIAL_PATTERNS, PERSONAL_PATH_PATTERNS, TEXT_EXTENSIONS, TEXT_FILENAMES


MAX_TEXT_FILE_BYTES = 50 * 1024 * 1024
MAX_METADATA_BYTES = 50 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
PATTERN_OVERLAP_BYTES = 4096
SENSITIVE_NAMES = {"credentials", "id_ed25519", "id_rsa", "secrets"}
SENSITIVE_EXTENSIONS = {".jks", ".key", ".p8", ".p12", ".pfx", ".ppk"}
OPAQUE_EXTENSIONS = {".7z", ".bak", ".db", ".dump", ".pdf", ".pgdump", ".rar", ".sqlite", ".sqlite3", ".tar", ".zip"}
OCI_INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
OCI_MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
OCI_CONFIG_MEDIA_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
    "application/vnd.oci.empty.v1+json",
}


def _bytes_pattern(pattern: re.Pattern[str]) -> re.Pattern[bytes]:
    return re.compile(pattern.pattern.encode("ascii"), pattern.flags & ~re.UNICODE)


BYTE_PATTERNS = tuple(
    (category, _bytes_pattern(pattern))
    for category, pattern in (
        *CREDENTIAL_PATTERNS,
        *(("personal_absolute_path", pattern) for pattern in PERSONAL_PATH_PATTERNS),
    )
)
PATTERN_BY_CATEGORY = {category: pattern for category, pattern in CREDENTIAL_PATTERNS}
POSTGRES_DOCUMENTATION_URL = re.compile(
    r"(?i)postgres(?:ql)?://postgres:postgres@(?:localhost|127\.0\.0\.1)(?=[:/])"
)
REVIEWED_PATH_ALLOWLIST = Path(__file__).with_name("docker_image_layer_audit_allowlist.json")


def _safe_archive_path(name: str) -> str:
    if not name or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name) or "\x00" in name:
        raise ValueError("unsafe archive path")
    normalized = name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError("unsafe archive path")
    return "/".join(parts) or "."


def _blob_path(descriptor: dict[str, Any]) -> str:
    digest = descriptor.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("unsupported image blob digest")
    return f"blobs/sha256/{digest.partition(':')[2]}"


def _read_metadata(archive: tarfile.TarFile, path: str, members: dict[str, tarfile.TarInfo]) -> bytes:
    info = members.get(path)
    if info is None or not info.isfile():
        raise ValueError("image archive is missing referenced metadata")
    if info.size > MAX_METADATA_BYTES:
        raise ValueError("image metadata exceeds the size limit")
    stream = archive.extractfile(info)
    if stream is None:
        raise ValueError("image metadata could not be read")
    with stream:
        return stream.read(MAX_METADATA_BYTES + 1)


def _add_finding(
    findings: set[tuple[str, tuple[str, ...], str, str, str]],
    category: str,
    images: set[str],
    layer: str,
    path: str,
    file_sha256: str = "",
) -> None:
    findings.add((category, tuple(sorted(images)), layer, path, file_sha256))


def _python_rhs_is_identifier_reference(text: str, match: re.Match[str]) -> bool:
    """A bare Python name is a code reference, not an embedded credential value."""
    separator = re.search(r"[:=]\s*", match.group(0))
    if separator is None:
        return False
    rhs = match.group(0)[separator.end():]
    rhs_identifier = re.match(r"([A-Za-z_]\w*)", rhs)
    if rhs_identifier is None or rhs.startswith(("'", '"')):
        return False
    rhs_start = match.start() + separator.end()
    line = text.count("\n", 0, rhs_start) + 1
    line_start = text.rfind("\n", 0, rhs_start) + 1
    column = rhs_start - line_start
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    has_expression = any(
        isinstance(node, (ast.Name, ast.Attribute))
        and getattr(node, "lineno", -1) == line
        and getattr(node, "col_offset", -1) == column
        for node in ast.walk(tree)
    )
    if not has_expression:
        return False
    identifier = rhs_identifier.group(1)
    return any(
        (isinstance(node, ast.arg) and node.arg == identifier)
        or (isinstance(node, ast.Name) and node.id == identifier and isinstance(node.ctx, ast.Store))
        or (isinstance(node, ast.alias) and (node.asname or node.name).split(".")[0] == identifier)
        or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == identifier)
        for node in ast.walk(tree)
    )


def _safe_documentation_database_urls(text: str) -> bool:
    matches = list(PATTERN_BY_CATEGORY["database_url_password"].finditer(text))
    if not matches:
        return False
    examples = list(POSTGRES_DOCUMENTATION_URL.finditer(text))
    return all(any(example.start() <= match.start() and example.end() >= match.end() for example in examples) for match in matches)


def _load_allowlist(path: Path | None = None) -> dict[tuple[str, str, str], str]:
    path = path or REVIEWED_PATH_ALLOWLIST
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("image audit review allowlist is missing or invalid") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("unsupported image audit review allowlist schema")
    records = document.get("reviewed_upstream_findings")
    if not isinstance(records, list):
        raise ValueError("invalid image audit review allowlist entries")
    allowed: dict[tuple[str, str, str], str] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("invalid image audit review allowlist entry")
        category, member_path, digest, basis = (
            record.get("category"), record.get("path"), record.get("sha256"), record.get("basis")
        )
        if (
            category not in {"personal_absolute_path", "credential_assignment"}
            or not isinstance(member_path, str)
            or _safe_archive_path(member_path) != member_path
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(basis, str)
            or not basis.strip()
        ):
            raise ValueError("invalid image audit review allowlist entry")
        key = (category, member_path, digest)
        if key in allowed:
            raise ValueError("duplicate image audit review allowlist entry")
        allowed[key] = basis
    return allowed


def _scan_content(
    stream: BinaryIO,
    size: int,
    path: str,
    images: set[str],
    layer: str,
    findings: set[tuple[str, tuple[str, ...], str, str, str]],
    stats: dict[str, int],
) -> None:
    stats["files_scanned"] += 1
    stats["bytes_scanned"] += size
    name = PurePosixPath(path).name.casefold()
    suffix = PurePosixPath(path).suffix.casefold()

    if name == ".env" or name.endswith(".env") or (name.startswith(".env.") and name != ".env.example") or name in SENSITIVE_NAMES:
        _add_finding(findings, "credential_file", images, layer, path)
        return
    if suffix in SENSITIVE_EXTENSIONS:
        _add_finding(findings, "credential_file", images, layer, path)
        return
    if suffix in OPAQUE_EXTENSIONS:
        _add_finding(findings, "private_or_opaque_archive", images, layer, path)
        return

    is_text = suffix in TEXT_EXTENSIONS or name in TEXT_FILENAMES or name == ".env.example"
    if is_text and size > MAX_TEXT_FILE_BYTES:
        _add_finding(findings, "oversized_text_file", images, layer, path)
        return

    candidates: set[str] = set()
    overlap = b""
    capture_text = size <= MAX_TEXT_FILE_BYTES and (suffix == ".py" or name == "metadata")
    captured = bytearray() if capture_text else None
    file_digest = hashlib.sha256()
    while True:
        chunk = stream.read(CHUNK_BYTES)
        if not chunk:
            break
        file_digest.update(chunk)
        if captured is not None:
            captured.extend(chunk)
        data = overlap + chunk
        for category, pattern in BYTE_PATTERNS:
            if pattern.search(data):
                candidates.add(category)
        overlap = data[-PATTERN_OVERLAP_BYTES:]
    text = captured.decode("utf-8", errors="replace") if captured is not None else ""
    for category in candidates:
        if category == "credential_assignment" and suffix == ".py":
            matches = list(PATTERN_BY_CATEGORY[category].finditer(text))
            if matches and all(_python_rhs_is_identifier_reference(text, match) for match in matches):
                continue
        if category == "database_url_password" and captured is not None and _safe_documentation_database_urls(text):
            continue
        _add_finding(findings, category, images, layer, path, file_digest.hexdigest())


def _scan_layer(
    archive: tarfile.TarFile,
    layer_path: str,
    references: set[tuple[str, str]],
    members: dict[str, tarfile.TarInfo],
    findings: set[tuple[str, tuple[str, ...], str, str, str]],
    stats: dict[str, int],
) -> None:
    info = members.get(layer_path)
    if info is None or not info.isfile():
        raise ValueError("image archive is missing a referenced layer")
    compressed = archive.extractfile(info)
    if compressed is None:
        raise ValueError("image layer could not be read")
    images = {image for image, _ in references}
    stats["layers_scanned"] += 1
    try:
        with compressed:
            with tarfile.open(fileobj=compressed, mode="r|*") as layer_archive:
                for member in layer_archive:
                    member_name = _safe_archive_path(member.name)
                    if not member.isfile():
                        continue
                    content = layer_archive.extractfile(member)
                    if content is None:
                        raise ValueError("image layer member could not be read")
                    with content:
                        _scan_content(
                            content,
                            member.size,
                            member_name,
                            images,
                            layer_path,
                            findings,
                            stats,
                        )
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise ValueError("image layer is corrupt or unreadable") from exc


def _scan_blob(
    archive: tarfile.TarFile,
    path: str,
    images: set[str],
    layer: str,
    members: dict[str, tarfile.TarInfo],
    findings: set[tuple[str, tuple[str, ...], str, str, str]],
    stats: dict[str, int],
) -> None:
    info = members.get(path)
    if info is None or not info.isfile():
        raise ValueError("image archive is missing a referenced blob")
    if info.size > MAX_METADATA_BYTES:
        _add_finding(findings, "oversized_image_metadata", images, layer, path)
        return
    stream = archive.extractfile(info)
    if stream is None:
        raise ValueError("image blob could not be read")
    with stream:
        _scan_content(stream, info.size, path, images, layer, findings, stats)


def _image_label(descriptor: dict[str, Any], fallback: str) -> str:
    annotations = descriptor.get("annotations")
    if isinstance(annotations, dict):
        name = annotations.get("io.containerd.image.name")
        if isinstance(name, str) and name:
            return name.removeprefix("docker.io/library/")
    return fallback


def audit_saved_archive(archive_path: Path, report_path: Path | None = None) -> dict[str, Any]:
    """Audit every layer and metadata blob in a Docker save archive."""
    archive_path = archive_path.expanduser().resolve(strict=True)
    if not archive_path.is_file():
        raise ValueError("saved image archive must be a file")
    if report_path is not None:
        report_path = report_path.expanduser()
        if report_path.exists() or report_path.is_symlink():
            raise ValueError("audit report already exists; choose a new path")
        if report_path.resolve(strict=False) == archive_path:
            raise ValueError("audit report cannot replace the image archive")

    findings: set[tuple[str, tuple[str, ...], str, str, str]] = set()
    layer_references: dict[str, set[tuple[str, str]]] = {}
    metadata_references: dict[str, set[str]] = {}
    scanned_blobs: set[str] = set()
    image_labels: set[str] = set()
    stats = {"layers_scanned": 0, "files_scanned": 0, "bytes_scanned": 0}

    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            members: dict[str, tarfile.TarInfo] = {}
            for member in archive.getmembers():
                name = _safe_archive_path(member.name)
                if name in members:
                    raise ValueError("duplicate path in image archive")
                members[name] = member

            manifest_info = members.get("manifest.json")
            if manifest_info is not None:
                manifest_bytes = _read_metadata(archive, "manifest.json", members)
                manifest = json.loads(manifest_bytes)
                if not isinstance(manifest, list) or not manifest:
                    raise ValueError("invalid Docker image manifest")
                metadata_references.setdefault("manifest.json", set()).add("saved-image-archive")
                for image_index, item in enumerate(manifest):
                    if not isinstance(item, dict):
                        raise ValueError("invalid Docker image manifest entry")
                    tags = item.get("RepoTags") or [f"image-{image_index + 1}"]
                    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                        raise ValueError("invalid Docker image tag list")
                    for tag in tags:
                        image_labels.add(tag)
                        config_path = _safe_archive_path(item.get("Config", ""))
                        metadata_references.setdefault(config_path, set()).add(tag)
                        layers = item.get("Layers")
                        if not isinstance(layers, list) or not layers:
                            raise ValueError("Docker image has no layers")
                        for layer_index, layer in enumerate(layers):
                            layer_path = _safe_archive_path(layer)
                            layer_references.setdefault(layer_path, set()).add((tag, str(layer_index)))

            index_info = members.get("index.json")
            if index_info is not None:
                metadata_references.setdefault("index.json", set()).add("saved-image-archive")
                index = json.loads(_read_metadata(archive, "index.json", members))
                if not isinstance(index, dict) or not isinstance(index.get("manifests"), list):
                    raise ValueError("invalid OCI image index")

                def visit_descriptor(
                    descriptor: dict[str, Any], fallback: str, depth: int, ancestors: frozenset[str]
                ) -> None:
                    if depth > 16:
                        raise ValueError("image metadata nesting exceeds the limit")
                    path = _blob_path(descriptor)
                    if path in ancestors:
                        raise ValueError("cyclic OCI image metadata")
                    label = _image_label(descriptor, fallback)
                    image_labels.add(label)
                    metadata_references.setdefault(path, set()).add(label)
                    media_type = descriptor.get("mediaType", "")
                    if not isinstance(media_type, str):
                        raise ValueError("invalid OCI media type")
                    if media_type not in OCI_INDEX_MEDIA_TYPES | OCI_MANIFEST_MEDIA_TYPES:
                        raise ValueError("unsupported OCI image descriptor media type")

                    payload = _read_metadata(archive, path, members)
                    try:
                        document = json.loads(payload)
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise ValueError("invalid OCI image metadata") from exc
                    if not isinstance(document, dict):
                        raise ValueError("invalid OCI image metadata")

                    chain = ancestors | {path}
                    children = document.get("manifests")
                    if media_type in OCI_INDEX_MEDIA_TYPES:
                        if not isinstance(children, list):
                            raise ValueError("invalid OCI index descriptors")
                        for child in children:
                            if not isinstance(child, dict):
                                raise ValueError("invalid OCI image descriptor")
                            visit_descriptor(child, label, depth + 1, chain)
                        return

                    config = document.get("config")
                    layers = document.get("layers")
                    if not isinstance(config, dict) or not isinstance(layers, list):
                        raise ValueError("invalid OCI image manifest")
                    config_type = config.get("mediaType")
                    if config_type not in OCI_CONFIG_MEDIA_TYPES:
                        raise ValueError("unsupported OCI image config media type")
                    config_path = _blob_path(config)
                    metadata_references.setdefault(config_path, set()).add(label)
                    for layer_index, layer in enumerate(layers):
                        if not isinstance(layer, dict):
                            raise ValueError("invalid OCI layer descriptor")
                        layer_path = _blob_path(layer)
                        layer_type = layer.get("mediaType", "")
                        if not isinstance(layer_type, str):
                            raise ValueError("invalid OCI layer media type")
                        if "tar" in layer_type:
                            layer_references.setdefault(layer_path, set()).add((label, str(layer_index)))
                        elif layer_type == "application/vnd.in-toto+json":
                            metadata_references.setdefault(layer_path, set()).add(label)
                        else:
                            raise ValueError("unsupported OCI image layer media type")

                for index_item, descriptor in enumerate(index["manifests"]):
                    if not isinstance(descriptor, dict):
                        raise ValueError("invalid OCI image descriptor")
                    visit_descriptor(descriptor, f"image-{index_item + 1}", 0, frozenset())

            if not manifest_info and not index_info:
                raise ValueError("image archive has no supported manifest")

            for path, refs in sorted(metadata_references.items()):
                _scan_blob(archive, path, refs, "metadata", members, findings, stats)
                scanned_blobs.add(path)
            for path, refs in sorted(layer_references.items()):
                _scan_layer(archive, path, refs, members, findings, stats)
                scanned_blobs.add(path)

            for path, member in members.items():
                if path.startswith("blobs/sha256/") and member.isfile() and path not in scanned_blobs:
                    _add_finding(findings, "unscanned_image_blob", set(), "archive", path)
                elif path in {"repositories", "oci-layout"} and member.isfile():
                    _scan_blob(
                        archive,
                        path,
                        image_labels or {"saved-image-archive"},
                        "metadata",
                        members,
                        findings,
                        stats,
                    )
                    scanned_blobs.add(path)

    except (OSError, EOFError, json.JSONDecodeError, tarfile.TarError) as exc:
        raise ValueError("saved image archive is corrupt or unreadable") from exc

    allowlist = _load_allowlist()
    serialized_findings: list[dict[str, Any]] = []
    reviewed_findings: list[dict[str, Any]] = []
    for category, images, layer, path, file_sha256 in sorted(findings):
        finding = {"category": category, "images": list(images), "layer": layer, "path": path}
        if file_sha256:
            finding["file_sha256"] = file_sha256
        review_basis = allowlist.get((category, path, file_sha256)) if file_sha256 else None
        if review_basis is None:
            serialized_findings.append(finding)
        else:
            reviewed_findings.append({**finding, "review_basis": review_basis})
    result: dict[str, Any] = {
        "schema_version": 2,
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "failed" if serialized_findings else "passed",
        "images_scanned": sorted(image_labels),
        "layers_scanned": stats["layers_scanned"],
        "files_scanned": stats["files_scanned"],
        "bytes_scanned": stats["bytes_scanned"],
        "findings": serialized_findings,
        "reviewed_findings": reviewed_findings,
        "scope_note": (
            "Scans Docker-save metadata and every referenced compressed or uncompressed layer for common credential and personal-path patterns, "
            "environment/credential files, and PDF or backup archives. Reviewed upstream findings are accepted only for exact category, path, and file SHA-256 matches; "
            "all other findings fail the audit. It does not assess vulnerabilities, licenses, malware behavior, or image provenance."
        ),
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def audit_images(image_refs: list[str], report_path: Path | None = None) -> dict[str, Any]:
    if not image_refs or any(not image or image.startswith("-") or any(ch.isspace() for ch in image) for image in image_refs):
        raise ValueError("provide one or more local Docker image references")
    if report_path is not None and (report_path.exists() or report_path.is_symlink()):
        raise ValueError("audit report already exists; choose a new path")

    try:
        with tempfile.TemporaryDirectory(prefix="docker-image-layer-audit-") as temporary:
            archive_path = Path(temporary) / "images.tar"
            subprocess.run(
                ["docker", "image", "save", "--output", str(archive_path), *image_refs],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            return audit_saved_archive(archive_path, report_path)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ValueError("Docker could not save the requested local images") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan all saved Docker image layers for common private-data leakage patterns."
    )
    parser.add_argument("images", nargs="+", help="local Docker image references to inspect")
    parser.add_argument("--report", type=Path, help="optional JSON report path; must be new")
    args = parser.parse_args(argv)

    try:
        result = audit_images(args.images, args.report)
    except (OSError, ValueError):
        print("Docker image layer audit could not complete; check the local images and report path.", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "status": result["status"],
                "image_count": len(result["images_scanned"]),
                "layers_scanned": result["layers_scanned"],
                "files_scanned": result["files_scanned"],
                "finding_count": len(result["findings"]),
                "reviewed_finding_count": len(result["reviewed_findings"]),
                "report_written": args.report is not None,
                "findings": result["findings"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
