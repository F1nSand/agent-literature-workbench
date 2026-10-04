"""Scan an assembled release directory for obvious private-data leakage.

This is a release hygiene gate, not a license assessment or a replacement for
manual review of images and other opaque binary artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


TEXT_EXTENSIONS = {
    ".c",
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".dockerignore",
    ".go",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".lock",
    ".md",
    ".mdx",
    ".mako",
    ".map",
    ".mjs",
    ".ps1",
    ".properties",
    ".py",
    ".rst",
    ".sh",
    ".sql",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
TEXT_FILENAMES = {
    ".dockerignore",
    ".gitkeep",
    ".gitignore",
    "dockerfile",
    "license",
    "makefile",
    "notice",
}
MEDIA_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".webp",
}
OPAQUE_REVIEW_EXTENSIONS = {
    ".doc",
    ".docx",
    ".odt",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".woff",
    ".woff2",
}
FORBIDDEN_EXTENSIONS = {
    ".7z",
    ".bak",
    ".db",
    ".dump",
    ".gz",
    ".pdf",
    ".pgdump",
    ".rar",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".zip",
}
CREDENTIAL_FILE_EXTENSIONS = {".jks", ".key", ".p8", ".p12", ".pem", ".pfx", ".ppk"}
FORBIDDEN_DIRECTORIES = {
    ".git",
    ".playwright-mcp",
    ".pnpm-store",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "data",
    "node_modules",
    "work",
}
MAX_TEXT_FILE_BYTES = 50 * 1024 * 1024
MAX_MANUAL_REVIEW_FILE_BYTES = 1024 * 1024
MAX_MANUAL_REVIEW_ASSET_BYTES = 50 * 1024 * 1024
MAX_MANUAL_REVIEW_ASSETS = 500
MANUAL_REVIEW_MANIFEST_NAME = "RELEASE-MANUAL-REVIEW.json"
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")

CREDENTIAL_PATTERNS = (
    ("provider_key", re.compile(r"(?i)\bsk-[a-z0-9_-]{24,}\b")),
    (
        "credential_assignment",
        re.compile(
            r"(?im)\b(?:[A-Z0-9]+[_-])*(?:API[_-]?KEY|ACCESS[_-]?(?:KEY|TOKEN)|"
            r"CLIENT[_-]?SECRET|SECRET[_-]?ACCESS[_-]?KEY|PRIVATE[_-]?KEY|PASSWORD|SECRET|TOKEN)\b"
            r"\s*[:=]\s*[\"']?(?!\$|<|(?:replace|your|example|redacted|placeholder)\b)"
            r"[A-Za-z0-9/+_=-]{24,}"
        ),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    (
        "bearer_token",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{24,}"),
    ),
    (
        "database_url_password",
        re.compile(r"(?i)\bpostgres(?:ql)?://[^\s/:@]+:[^\s/@]{8,}@"),
    ),
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
)
PERSONAL_PATH_PATTERNS = (
    re.compile(
        r"(?ix)\b[A-Z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/)"
        r"(?!<|username(?:\\{1,2}|/)|yourname(?:\\{1,2}|/)|user(?:\\{1,2}|/))"
        r"[^\\/\r\n<>\"']+(?:\\{1,2}|/)"
    ),
    re.compile(
        r"(?i)(?<![\w:])/(?:Users|home)/(?!<|username/|yourname/|user/)[^/\s<>\"']+/"
    ),
)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (callable(is_junction) and is_junction())


def _raise_walk_error(error: OSError) -> None:
    raise error


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manual_asset_reviews(
    root: Path,
    files: list[Path],
    findings: list[dict[str, str]],
) -> tuple[str | None, dict[str, dict[str, str]]]:
    manifest_path = root / MANUAL_REVIEW_MANIFEST_NAME
    if not manifest_path.exists():
        return None, {}

    relative_manifest_path = MANUAL_REVIEW_MANIFEST_NAME
    try:
        if _is_link_or_junction(manifest_path) or not manifest_path.is_file():
            raise ValueError("manifest must be a regular file")
        size = manifest_path.stat().st_size
        if size > MAX_MANUAL_REVIEW_FILE_BYTES:
            raise ValueError("manifest exceeds size limit")
        payload = manifest_path.read_bytes()
        manifest_hash = hashlib.sha256(payload).hexdigest()
        decoded = payload.decode("utf-8-sig")
        manifest = json.loads(decoded, object_pairs_hook=_json_object_without_duplicates)
        if not isinstance(manifest, dict) or set(manifest) != {
            "schema_version",
            "reviewer",
            "reviewed_at_utc",
            "review_method",
            "files",
        }:
            raise ValueError("unexpected manifest schema")
        if (
            type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != 1
        ):
            raise ValueError("unsupported manifest version")
        reviewer = manifest["reviewer"]
        reviewed_at = manifest["reviewed_at_utc"]
        review_method = manifest["review_method"]
        if not all(
            isinstance(value, str) and value.strip()
            for value in (reviewer, reviewed_at, review_method)
        ):
            raise ValueError("review metadata is incomplete")
        if any(
            len(value) > 500 or "\x00" in value
            for value in (reviewer, reviewed_at, review_method)
        ):
            raise ValueError("review metadata is invalid")
        parsed_date = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        if (
            parsed_date.tzinfo is None
            or parsed_date.utcoffset() is None
            or parsed_date.utcoffset().total_seconds() != 0
        ):
            raise ValueError("review timestamp must be UTC")
        entries = manifest["files"]
        if (
            not isinstance(entries, list)
            or not 1 <= len(entries) <= MAX_MANUAL_REVIEW_ASSETS
        ):
            raise ValueError("review asset list is empty or too large")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        findings.append(
            {"category": "manual_review_manifest_invalid", "path": relative_manifest_path}
        )
        return None, {}

    targets = {_relative(path, root): path for path in files}
    reviews: dict[str, dict[str, str]] = {}
    seen_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            findings.append(
                {"category": "manual_review_manifest_invalid", "path": relative_manifest_path}
            )
            continue
        relative_path = entry["path"]
        digest = entry["sha256"]
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or "\\" in relative_path
            or ":" in relative_path
            or relative_path.startswith("/")
            or relative_path != PurePosixPath(relative_path).as_posix()
            or ".." in PurePosixPath(relative_path).parts
            or not isinstance(digest, str)
            or not SHA256_PATTERN.fullmatch(digest)
        ):
            findings.append(
                {"category": "manual_review_manifest_invalid", "path": relative_manifest_path}
            )
            continue
        if relative_path in seen_paths:
            findings.append(
                {"category": "manual_review_manifest_invalid", "path": relative_manifest_path}
            )
            continue
        seen_paths.add(relative_path)

        target = targets.get(relative_path)
        if target is None:
            findings.append(
                {"category": "manual_review_target_missing", "path": relative_path}
            )
            continue
        if target.suffix.casefold() not in MEDIA_EXTENSIONS:
            findings.append(
                {"category": "manual_review_unreviewable_target", "path": relative_path}
            )
            continue
        try:
            if target.stat().st_size > MAX_MANUAL_REVIEW_ASSET_BYTES:
                findings.append(
                    {"category": "manual_review_asset_too_large", "path": relative_path}
                )
                continue
            actual_digest = _sha256_file(target)
        except OSError:
            findings.append({"category": "unreadable_media", "path": relative_path})
            continue
        if actual_digest != digest:
            findings.append(
                {"category": "manual_review_hash_mismatch", "path": relative_path}
            )
            continue
        reviews[relative_path] = {
            "path": relative_path,
            "sha256": digest,
            "reviewer": reviewer.strip(),
            "reviewed_at_utc": parsed_date.isoformat().replace("+00:00", "Z"),
            "review_method": review_method.strip(),
        }

    if not seen_paths:
        findings.append(
            {"category": "manual_review_manifest_invalid", "path": relative_manifest_path}
        )
    return manifest_hash, reviews


def _walk_entries(root: Path) -> tuple[list[Path], list[dict[str, str]]]:
    files: list[Path] = []
    findings: list[dict[str, str]] = []

    for current, directories, filenames in os.walk(
        root, followlinks=False, onerror=_raise_walk_error
    ):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in directories:
            path = current_path / name
            if name.casefold() in FORBIDDEN_DIRECTORIES:
                findings.append(
                    {"category": "release_development_directory", "path": _relative(path, root)}
                )
            elif path.name.casefold() == ".env":
                findings.append(
                    {"category": "credential_file", "path": _relative(path, root)}
                )
            elif _is_link_or_junction(path):
                findings.append(
                    {"category": "symlink", "path": _relative(path, root)}
                )
            else:
                kept_directories.append(name)
        directories[:] = kept_directories

        for name in filenames:
            path = current_path / name
            if _is_link_or_junction(path):
                findings.append({"category": "symlink", "path": _relative(path, root)})
            else:
                files.append(path)

    return sorted(files), findings


def audit_bundle(root: Path, report_path: Path | None = None) -> dict[str, Any]:
    """Audit a staged release directory without printing file contents."""
    root = root.expanduser()
    if _is_link_or_junction(root):
        raise ValueError("release root cannot be a symlink")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("release root must be a directory")

    resolved_report: Path | None = None
    if report_path is not None:
        if report_path.expanduser().exists():
            raise ValueError("audit report already exists; choose a new path")
        resolved_report = report_path.expanduser().resolve()
        try:
            resolved_report.relative_to(root)
        except ValueError:
            pass
        else:
            raise ValueError("audit report must be written outside the release root")

    files, findings = _walk_entries(root)
    if resolved_report is not None:
        files = [path for path in files if path.resolve() != resolved_report]

    manual_review_manifest_sha256, reviewed_asset_map = _load_manual_asset_reviews(
        root, files, findings
    )
    text_files_scanned = 0
    manual_review_files: list[str] = []
    reviewed_assets: list[dict[str, str]] = []
    if not files and not findings:
        findings.append({"category": "empty_release_bundle", "path": "."})

    for path in files:
        relative_path = _relative(path, root)
        name = path.name.casefold()
        suffix = path.suffix.casefold()

        if (
            name == ".env"
            or name.endswith(".env")
            or (name.startswith(".env.") and name != ".env.example")
            or name in {"credentials", "id_ed25519", "id_rsa", "secrets"}
        ):
            findings.append({"category": "credential_file", "path": relative_path})
            continue
        if suffix in CREDENTIAL_FILE_EXTENSIONS:
            findings.append({"category": "credential_file", "path": relative_path})
            continue
        if suffix in FORBIDDEN_EXTENSIONS:
            findings.append({"category": "private_or_opaque_archive", "path": relative_path})
            continue

        if suffix in MEDIA_EXTENSIONS:
            review = reviewed_asset_map.get(relative_path)
            if review is None:
                manual_review_files.append(relative_path)
            else:
                reviewed_assets.append(review)
            if suffix != ".svg":
                continue
        if suffix in OPAQUE_REVIEW_EXTENSIONS:
            manual_review_files.append(relative_path)
            continue

        is_text = (
            suffix in TEXT_EXTENSIONS
            or name in TEXT_FILENAMES
            or name.startswith("dockerfile.")
            or name == ".env.example"
        )
        if not is_text:
            manual_review_files.append(relative_path)
            continue
        try:
            if path.stat().st_size > MAX_TEXT_FILE_BYTES:
                findings.append({"category": "oversized_text_file", "path": relative_path})
                continue
        except OSError:
            findings.append({"category": "unreadable_text", "path": relative_path})
            continue

        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            findings.append({"category": "unreadable_text", "path": relative_path})
            continue

        text_files_scanned += 1
        for category, pattern in CREDENTIAL_PATTERNS:
            if pattern.search(content):
                findings.append({"category": category, "path": relative_path})
        if any(pattern.search(content) for pattern in PERSONAL_PATH_PATTERNS):
            findings.append({"category": "personal_absolute_path", "path": relative_path})

    findings.sort(key=lambda item: (item["path"], item["category"]))
    manual_review_files = sorted(set(manual_review_files))
    if findings:
        status = "failed"
    elif manual_review_files:
        status = "needs_manual_review"
    else:
        status = "passed"

    result: dict[str, Any] = {
        "schema_version": 1,
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "files_seen": len(files),
        "text_files_scanned": text_files_scanned,
        "findings": findings,
        "manual_review_files": manual_review_files,
        "manual_review_manifest_sha256": manual_review_manifest_sha256,
        "reviewed_assets": sorted(reviewed_assets, key=lambda item: item["path"]),
        "scope_note": (
            "Checks staged text for common credential and personal-path patterns; "
            "rejects environment files, PDFs, and backup/archive formats. It does "
            "not inspect Docker image layers or establish copyright permission. "
            "Media assets without an exact-path and exact-SHA-256 review record "
            "remain pending visual review."
        ),
    }

    if resolved_report is not None:
        resolved_report.parent.mkdir(parents=True, exist_ok=True)
        resolved_report.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a staged release directory for obvious private-data leakage."
    )
    parser.add_argument("release_root", type=Path, help="assembled release directory")
    parser.add_argument(
        "--report", type=Path, help="optional JSON report path outside release_root"
    )
    args = parser.parse_args(argv)

    try:
        result = audit_bundle(args.release_root, args.report)
    except (OSError, ValueError):
        print(
            "release audit could not run; check the staged directory and report path.",
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            {
                "status": result["status"],
                "files_seen": result["files_seen"],
                "text_files_scanned": result["text_files_scanned"],
                "finding_count": len(result["findings"]),
                "manual_review_count": len(result["manual_review_files"]),
                "reviewed_asset_count": len(result["reviewed_assets"]),
                "report_written": args.report is not None,
                "findings": result["findings"],
                "manual_review_files": result["manual_review_files"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
