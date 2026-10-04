#!/usr/bin/env python3
"""Verify DB/file-volume consistency after restoring a library backup."""

from __future__ import annotations

import json
import sys
from pathlib import Path


if Path("/app/app").is_dir():
    sys.path.insert(0, "/app")
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.db.session import get_engine  # noqa: E402
from app.operations.restore_verification import (  # noqa: E402
    RestoreVerificationError,
    verify_restored_library,
)


def main() -> int:
    try:
        report = verify_restored_library(get_engine(), get_settings().data_dir)
    except RestoreVerificationError as error:
        report = {"status": "failed", "error_code": str(error)}
    except Exception:
        report = {"status": "failed", "error_code": "database_validation_failed"}
    sys.stdout.write(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
