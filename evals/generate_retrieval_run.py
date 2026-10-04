#!/usr/bin/env python3
"""Generate bounded candidate-only R1/R2/R3 rankings from the active sample corpus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
BACKEND_ROOT = ROOT / "backend"
if not BACKEND_ROOT.exists():
    BACKEND_ROOT = Path("/app")
if not (ROOT / "experiments").exists():
    ROOT = Path("/app")
sys.path.insert(0, str(BACKEND_ROOT))

from app.documents.retrieval_eval_runner import generate  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate R1/R2/R3 retrieval rankings; defaults to a no-call preflight.")
    parser.add_argument("--qrels", type=Path, default=EVAL_DIR / "datasets/sample-retrieval-qrels.json")
    parser.add_argument("--manifest", type=Path, default=EVAL_DIR / "datasets/sample-corpus-manifest.json")
    default_output = Path("/runs/sample-retrieval-candidate-run.json") if Path("/runs").exists() else ROOT / "experiments/runs/sample-retrieval-candidate-run.json"
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--execute", action="store_true", help="make the capped embedding/reranker requests and save a candidate-only run")
    parser.add_argument("--new-run", action="store_true", help="start a fresh run ID; choose a new --output path to preserve prior artifacts")
    args = parser.parse_args()
    try:
        report = generate(args.qrels, args.manifest, args.output, run=args.execute, new_run=args.new_run)
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
