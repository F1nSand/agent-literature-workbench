"""Assemble a clean, source-based local release staging directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from .release_audit import (
        MANUAL_REVIEW_MANIFEST_NAME,
        MAX_MANUAL_REVIEW_FILE_BYTES,
        audit_bundle,
    )
except ImportError:  # Running this file directly from the scripts directory.
    from release_audit import (
        MANUAL_REVIEW_MANIFEST_NAME,
        MAX_MANUAL_REVIEW_FILE_BYTES,
        audit_bundle,
    )


ROOT_FILES = (
    ".dockerignore",
    ".env.example",
    "README.md",
    "compose.yaml",
    "compose.production.yaml",
    "本地配置说明.md",
)
PACKAGE_DIRECTORIES = (
    "backend",
    "frontend",
    "config",
    "docs/design",
    "docs/demo",
    "docs/operations",
)
RUNTIME_SCRIPTS = (
    "backup.ps1",
    "audit_docker_image_layers.py",
    "docker_image_layer_audit_allowlist.json",
    "build_release_bundle.py",
    "compose.ps1",
    "compose.restore-test.yaml",
    "release_audit.py",
    "restore_test.ps1",
    "verify_restored_library.py",
)
SKIP_DIRECTORIES = {
    ".git",
    ".playwright-mcp",
    ".pnpm-store",
    ".pytest_cache",
    ".venv",
    ".vscode",
    ".cache",
    ".turbo",
    ".vite",
    "__pycache__",
    "build",
    "coverage",
    "data",
    "dist",
    "node_modules",
    "tests",
    "__tests__",
}
MANIFEST_NAME = "RELEASE-MANIFEST.json"


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (callable(is_junction) and is_junction())


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _raise_walk_error(error: OSError) -> None:
    raise error


def _source_files(source_root: Path) -> list[tuple[Path, Path]]:
    selected = list(ROOT_FILES) + list(PACKAGE_DIRECTORIES)
    selected.append("scripts")
    pairs: list[tuple[Path, Path]] = []

    for relative in selected:
        source = source_root / relative
        if _is_link_or_junction(source):
            raise ValueError("selected source path cannot be a link")
        if not source.exists():
            raise ValueError("a required release source is missing")
        if source.is_file():
            pairs.append((source, Path(relative)))
            continue
        if not source.is_dir():
            raise ValueError("selected release source must be a file or directory")

        if relative == "scripts":
            for name in RUNTIME_SCRIPTS:
                path = source / name
                if _is_link_or_junction(path) or not path.is_file():
                    raise ValueError("a required runtime script is missing or linked")
                pairs.append((path, Path("scripts") / name))
            continue

        for current, directories, filenames in os.walk(
            source, followlinks=False, onerror=_raise_walk_error
        ):
            current_path = Path(current)
            kept_directories: list[str] = []
            for name in sorted(directories):
                path = current_path / name
                if name.casefold() in SKIP_DIRECTORIES or name.casefold().endswith(".egg-info"):
                    continue
                if _is_link_or_junction(path):
                    raise ValueError("selected source tree contains a link")
                kept_directories.append(name)
            directories[:] = kept_directories

            for name in sorted(filenames):
                path = current_path / name
                if name.casefold() in {".ds_store", "thumbs.db"}:
                    continue
                folded_name = name.casefold()
                if folded_name == ".env" or (folded_name.startswith(".env.") and folded_name != ".env.example"):
                    continue
                if _is_link_or_junction(path) or not path.is_file():
                    raise ValueError("selected source tree contains a link or special file")
                pairs.append((path, Path(relative) / path.relative_to(source)))

    return sorted(pairs, key=lambda pair: pair[1].as_posix())


def _write_manifest(bundle: Path) -> None:
    entries: list[dict[str, Any]] = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        payload = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(bundle).as_posix(),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest = {"schema_version": 1, "files": entries}
    (bundle / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_release_bundle(
    source_root: Path,
    output_path: Path,
    report_path: Path | None = None,
    manual_review_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Build a fresh package and audit it before publishing the staging directory."""
    source_arg = source_root.expanduser()
    if _is_link_or_junction(source_arg):
        raise ValueError("source root cannot be a link")
    source = source_arg.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("source root must be a directory")

    output_arg = output_path.expanduser()
    if _is_link_or_junction(output_arg) or output_arg.exists():
        raise ValueError("output already exists or is a link")
    output = output_arg.resolve(strict=False)
    if output == source or _is_within(source, output):
        raise ValueError("output cannot be the source root or its parent")
    for relative in PACKAGE_DIRECTORIES + ("scripts",):
        if _is_within(output, (source / relative).resolve()):
            raise ValueError("output cannot be inside a selected source tree")
    if report_path is not None:
        report = report_path.expanduser().resolve(strict=False)
        if _is_within(report, output):
            raise ValueError("audit report must be outside the output bundle")
        for relative in ROOT_FILES + PACKAGE_DIRECTORIES + ("scripts",):
            source_entry = (source / relative).resolve(strict=False)
            if _is_within(report, source_entry):
                raise ValueError("audit report cannot modify a packaged source tree")

    review_manifest: Path | None = None
    if manual_review_manifest_path is not None:
        review_arg = manual_review_manifest_path.expanduser()
        if (
            _is_link_or_junction(review_arg)
            or review_arg.suffix.casefold() != ".json"
            or not review_arg.is_file()
        ):
            raise ValueError("manual review manifest must be a regular file")
        review_manifest = review_arg.resolve(strict=True)
        if review_manifest.stat().st_size > MAX_MANUAL_REVIEW_FILE_BYTES:
            raise ValueError("manual review manifest exceeds size limit")
        if (
            report_path is not None
            and review_manifest == report_path.expanduser().resolve(strict=False)
        ):
            raise ValueError("audit report cannot overwrite the manual review manifest")

    pairs = _source_files(source)
    if review_manifest is not None and any(
        relative_target.as_posix() == MANUAL_REVIEW_MANIFEST_NAME
        for _, relative_target in pairs
    ):
        raise ValueError("manual review manifest target conflicts with a source file")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".release-stage-", dir=output.parent) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir()
        for source_file, relative_target in pairs:
            target = stage / relative_target
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target)
        if review_manifest is not None:
            shutil.copy2(review_manifest, stage / MANUAL_REVIEW_MANIFEST_NAME)
        _write_manifest(stage)
        audit = audit_bundle(stage, report_path)
        if audit["status"] == "failed":
            return {"published": False, "output": output, "audit": audit}
        if output.exists() or _is_link_or_junction(output):
            raise ValueError("output appeared during assembly; refusing to overwrite it")
        os.rename(stage, output)

    return {"published": True, "output": output, "audit": audit}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble and audit a clean local release staging directory."
    )
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True, help="new staging directory")
    parser.add_argument("--report", type=Path, help="optional audit report outside the bundle")
    parser.add_argument(
        "--manual-review-manifest",
        type=Path,
        help="optional JSON file containing exact-hash media review records",
    )
    args = parser.parse_args(argv)

    try:
        result = build_release_bundle(
            args.source_root,
            args.output,
            args.report,
            args.manual_review_manifest,
        )
    except (OSError, ValueError):
        print(
            "release bundle assembly failed; check source, output, and report paths.",
            file=sys.stderr,
        )
        return 2

    audit = result["audit"]
    print(
        json.dumps(
            {
                "published_for_review": result["published"],
                "audit_status": audit["status"],
                "files_seen": audit["files_seen"],
                "text_files_scanned": audit["text_files_scanned"],
                "findings": audit["findings"],
                "manual_review_files": audit["manual_review_files"],
                "reviewed_asset_count": len(audit["reviewed_assets"]),
                "report_written": args.report is not None,
            },
            ensure_ascii=False,
        )
    )
    if not result["published"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
