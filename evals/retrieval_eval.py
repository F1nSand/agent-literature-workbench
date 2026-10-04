#!/usr/bin/env python3
"""Offline scorer for saved R1/R2/R3 retrieval runs. No provider access is used."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
BACKEND_ROOT = ROOT / "backend"
if not BACKEND_ROOT.exists():
    BACKEND_ROOT = Path("/app")
sys.path.insert(0, str(BACKEND_ROOT))

from app.documents.evaluation import evaluate_run_document  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Score saved R1/R2/R3 retrieval rankings without model calls.")
    parser.add_argument("--qrels", type=Path, required=True, help="evidence labels JSON")
    parser.add_argument("--run", type=Path, required=True, help="saved retrieval rankings JSON")
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    parser.add_argument("--k", type=int, default=8, help="ranking cutoff (default: 8)")
    args = parser.parse_args()

    try:
        report = evaluate_run_document(_read_json(args.qrels), _read_json(args.run), cutoff=args.k)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        parser.error(str(error))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
