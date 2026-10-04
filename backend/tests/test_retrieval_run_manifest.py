import json
from pathlib import Path
from uuid import UUID

import pytest

from app.documents import retrieval_eval_runner as runner


@pytest.fixture
def candidate_plan() -> dict:
    return {
        "qrels": {
            "dataset_id": "candidate-set",
            "annotation_status": "pending",
            "source_review": "pending",
        },
        "manifest_sha256": "A" * 64,
        "papers": [
            runner.ActivePaper(
                id=UUID("00000000-0000-0000-0000-000000000001"),
                filename="paper.pdf",
                sha256="B" * 64,
                page_count=10,
                version_id=UUID("00000000-0000-0000-0000-000000000002"),
            )
        ],
        "generation_id": UUID("00000000-0000-0000-0000-000000000003"),
        "embedding_model": "embedding-candidate",
        "reranker_model": "reranker-candidate",
        "query_count": 1,
        "embedding_cache_hits": 0,
        "embedding_cache_misses": 1,
        "estimated_embedding_tokens": 12,
        "estimated_provider_requests_max": 2,
        "estimated_embedding_cost_cny_max": 0.001,
        "reranker_candidates_per_question": 8,
        "reranker_prompt_tokens_cap": 100,
        "estimated_reranker_cost_cny": 0,
    }


def _install_preflight(monkeypatch: pytest.MonkeyPatch, candidate_plan: dict) -> None:
    monkeypatch.setattr(runner, "preflight", lambda *_args: candidate_plan)


def test_run_manifest_is_persisted_before_execution_and_completed_output_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_plan: dict
) -> None:
    _install_preflight(monkeypatch, candidate_plan)
    qrels = tmp_path / "qrels.json"
    qrels.write_text('{"dataset_id":"candidate-set"}\n', encoding="utf-8")
    output = tmp_path / "run.json"
    execute_calls = 0

    def execute(plan: dict, run_id: str) -> dict:
        nonlocal execute_calls
        execute_calls += 1
        assert plan is candidate_plan
        manifest = json.loads((tmp_path / "run.json.manifest.json").read_text(encoding="utf-8"))
        assert manifest["execution_status"] == "running"
        assert manifest["run_id"] == run_id
        assert manifest["official_claim_allowed"] is False
        return {"run_id": run_id, "evaluation_scope": "candidate-exploration", "budget": {"provider_requests_made": 0}}

    monkeypatch.setattr(runner, "execute", execute)
    first = runner.generate(qrels, tmp_path / "corpus.json", output, run=True)

    manifest_path = tmp_path / "run.json.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["execution_status"] == "completed"
    assert manifest["run_output_filename"] == output.name
    assert execute_calls == 1

    def unexpected_execute(*_args: object) -> dict:
        raise AssertionError("a completed run must be reused without provider calls")

    monkeypatch.setattr(runner, "execute", unexpected_execute)
    second = runner.generate(qrels, tmp_path / "corpus.json", output, run=True)
    assert second == first
    assert execute_calls == 1


def test_incomplete_manifest_prevents_automatic_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_plan: dict) -> None:
    _install_preflight(monkeypatch, candidate_plan)
    qrels = tmp_path / "qrels.json"
    qrels.write_text('{"dataset_id":"candidate-set"}\n', encoding="utf-8")
    output = tmp_path / "run.json"
    execute_calls = 0

    def fail_execution(_plan: dict, _run_id: str) -> dict:
        nonlocal execute_calls
        execute_calls += 1
        raise ValueError("provider detail must not be persisted")

    monkeypatch.setattr(runner, "execute", fail_execution)
    with pytest.raises(ValueError):
        runner.generate(qrels, tmp_path / "corpus.json", output, run=True)

    manifest = json.loads((tmp_path / "run.json.manifest.json").read_text(encoding="utf-8"))
    assert manifest["execution_status"] == "failed"
    assert manifest["error_code"] == "evaluation_failed"
    assert "provider detail" not in json.dumps(manifest)

    with pytest.raises(RuntimeError, match="already exists without a completed output"):
        runner.generate(qrels, tmp_path / "corpus.json", output, run=True)
    assert execute_calls == 1


def test_completed_output_checksum_mismatch_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_plan: dict) -> None:
    _install_preflight(monkeypatch, candidate_plan)
    qrels = tmp_path / "qrels.json"
    qrels.write_text('{"dataset_id":"candidate-set"}\n', encoding="utf-8")
    output = tmp_path / "run.json"
    monkeypatch.setattr(runner, "execute", lambda _plan, run_id: {"run_id": run_id, "budget": {}})
    runner.generate(qrels, tmp_path / "corpus.json", output, run=True)
    output.write_text('{"run_id":"tampered"}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="checksum does not match"):
        runner.generate(qrels, tmp_path / "corpus.json", output, run=True)


def test_new_run_requires_execution_and_does_not_write_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_preflight(*_args: object) -> dict:
        raise AssertionError("preflight must not run")

    monkeypatch.setattr(runner, "preflight", unexpected_preflight)
    qrels = tmp_path / "qrels.json"
    qrels.write_text('{"dataset_id":"candidate-set"}\n', encoding="utf-8")
    output = tmp_path / "run.json"

    with pytest.raises(ValueError, match="requires provider execution"):
        runner.generate(qrels, tmp_path / "corpus.json", output, new_run=True)
    assert not output.exists()
    assert not (tmp_path / "run.json.manifest.json").exists()
