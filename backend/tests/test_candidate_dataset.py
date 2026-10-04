import json
import subprocess
import sys
from pathlib import Path

from app.documents.evaluation import evaluate_run_document


def test_supplemental_candidates_validate_and_remain_non_official(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    output_path = tmp_path / "supplemental-qrels.json"
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "evals" / "validate_candidate_set.py"),
            "--emit-retrieval-qrels",
            str(output_path),
        ],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    validation = json.loads(result.stdout)
    qrels = json.loads(output_path.read_text(encoding="utf-8"))
    assert validation["status"] == "candidate-valid"
    assert validation["official_evaluation_allowed"] is False
    assert validation["category_counts"] == {
        "answerable": 8,
        "insufficient_evidence": 4,
        "scope_security": 4,
    }
    assert len(qrels["questions"]) == 8

    report = evaluate_run_document(
        qrels,
        {
            "run_id": "schema-smoke-only",
            "corpus_manifest_sha256": qrels["corpus_manifest_sha256"],
            "strategies": {name: {"rankings": {}} for name in ("R1", "R2", "R3")},
        },
    )
    assert report["result_status"] == "candidate-exploration"
    assert report["official_claim_allowed"] is False
