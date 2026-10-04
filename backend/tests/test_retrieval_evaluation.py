import pytest

from app.documents.evaluation import evaluate_rankings, evaluate_run_document


def test_evaluate_rankings_deduplicates_evidence_pages_and_reports_unknown_cost() -> None:
    report = evaluate_rankings(
        {
            "Q1": [{"document": "paper-a.pdf", "page_number": 2}, {"document": "paper-a.pdf", "page_number": 4}],
            "Q2": [{"document": "paper-b.pdf", "page_number": 1}],
        },
        {
            "Q1": [
                {"document": "paper-a.pdf", "page_number": 2},
                {"document": "paper-a.pdf", "page_number": 2},
                {"document": "paper-a.pdf", "page_number": 9},
            ],
            "Q2": [],
        },
        latency_ms={"Q1": 100, "Q2": 200},
        request_cost_cny={"Q1": 0.01, "Q2": None},
    )

    assert report["questions_ranked"] == 2
    assert report["questions_missing_rankings"] == 0
    assert report["evidence_recall_at_k"] == 0.25
    assert report["hit_at_k"] == 0.5
    assert report["mrr_at_k"] == 0.5
    assert report["mean_latency_ms"] == 150
    assert report["request_cost_cny"] is None

    incomplete_latency = evaluate_rankings(
        {"Q1": [{"document": "paper.pdf", "page_number": 1}], "Q2": [{"document": "other.pdf", "page_number": 1}]},
        {"Q1": [], "Q2": []},
        latency_ms={"Q1": 10},
    )
    assert incomplete_latency["latency_questions"] == 1
    assert incomplete_latency["mean_latency_ms"] is None


def test_missing_question_rankings_score_zero_in_the_macro_average() -> None:
    report = evaluate_rankings(
        {
            "Q1": [{"document": "paper.pdf", "page_number": 1}],
            "Q2": [{"document": "paper.pdf", "page_number": 2}],
        },
        {"Q1": [{"document": "paper.pdf", "page_number": 1}]},
        latency_ms={"Q1": 100},
        request_cost_cny={"Q1": 0.01},
    )

    assert report["questions_with_qrels"] == 2
    assert report["questions_ranked"] == 1
    assert report["questions_missing_rankings"] == 1
    assert report["evidence_recall_at_k"] == 0.5
    assert report["hit_at_k"] == 0.5
    assert report["mrr_at_k"] == 0.5
    assert report["mean_latency_ms"] is None
    assert report["request_cost_cny"] is None


def test_candidate_annotations_are_never_reported_as_confirmed_evaluation() -> None:
    report = evaluate_run_document(
        {
            "dataset_id": "sample-v1",
            "annotation_status": "candidate",
            "corpus_manifest_sha256": "manifest-hash",
            "questions": [{"id": "Q1", "candidate_evidence": [{"document": "paper.pdf", "page_number": 3}]}],
        },
        {
            "run_id": "run-1",
            "corpus_manifest_sha256": "manifest-hash",
            "strategies": {
                "R1": {"rankings": {"Q1": [{"document": "paper.pdf", "page_number": 3}]}},
            },
        },
    )

    assert report["result_status"] == "candidate-exploration"
    assert report["official_claim_allowed"] is False
    assert report["corpus_match"] is True
    assert report["results"]["R1"]["evidence_recall_at_k"] == 1.0
    assert report["results"]["R2"]["status"] == "missing"
    assert report["results"]["R3"]["status"] == "missing"


def test_confirmed_labels_cannot_score_against_a_different_corpus_manifest() -> None:
    report = evaluate_run_document(
        {
            "annotation_status": "confirmed",
            "corpus_manifest_sha256": "expected",
            "questions": [{"id": "Q1", "evidence": [{"document": "paper.pdf", "page_number": 1}]}],
        },
        {"corpus_manifest_sha256": "different", "strategies": {}},
    )

    assert report["result_status"] == "corpus-mismatch"
    assert report["official_claim_allowed"] is False
    assert report["corpus_match"] is False


def test_confirmed_labels_with_partial_strategy_coverage_are_incomplete() -> None:
    report = evaluate_run_document(
        {
            "annotation_status": "confirmed",
            "corpus_manifest_sha256": "expected",
            "questions": [
                {"id": "Q1", "evidence": [{"document": "paper.pdf", "page_number": 1}]},
                {"id": "Q2", "evidence": [{"document": "paper.pdf", "page_number": 2}]},
            ],
        },
        {
            "corpus_manifest_sha256": "expected",
            "evaluation_scope": "confirmed-evaluation",
            "official_claim_allowed": True,
            "strategies": {
                name: {"rankings": {"Q1": [{"document": "paper.pdf", "page_number": 1}]}}
                for name in ("R1", "R2", "R3")
            },
        },
    )

    assert report["result_status"] == "incomplete-run"
    assert report["official_claim_allowed"] is False
    assert report["run_coverage_complete"] is False
    assert report["results"]["R1"]["status"] == "partial"
    assert report["results"]["R1"]["evidence_recall_at_k"] == 0.5
    assert report["results"]["R1"]["questions_missing_rankings"] == 1


def test_official_claim_requires_confirmed_scope_and_complete_r1_r2_r3_coverage() -> None:
    questions = [
        {"id": "Q1", "evidence": [{"document": "paper.pdf", "page_number": 1}]},
        {"id": "Q2", "evidence": [{"document": "paper.pdf", "page_number": 2}]},
    ]
    report = evaluate_run_document(
        {"annotation_status": "confirmed", "corpus_manifest_sha256": "expected", "questions": questions},
        {
            "corpus_manifest_sha256": "expected",
            "evaluation_scope": "confirmed-evaluation",
            "official_claim_allowed": True,
            "strategies": {
                name: {
                    "rankings": {
                        "Q1": [{"document": "paper.pdf", "page_number": 1}],
                        "Q2": [{"document": "paper.pdf", "page_number": 2}],
                    }
                }
                for name in ("R1", "R2", "R3")
            },
        },
    )

    assert report["result_status"] == "confirmed-evaluation"
    assert report["official_claim_allowed"] is True
    assert report["run_coverage_complete"] is True


def test_candidate_run_remains_nonofficial_after_labels_are_confirmed() -> None:
    questions = [{"id": "Q1", "evidence": [{"document": "paper.pdf", "page_number": 1}]}]
    report = evaluate_run_document(
        {"annotation_status": "confirmed", "corpus_manifest_sha256": "expected", "questions": questions},
        {
            "corpus_manifest_sha256": "expected",
            "evaluation_scope": "candidate-exploration",
            "official_claim_allowed": False,
            "strategies": {
                name: {"rankings": {"Q1": [{"document": "paper.pdf", "page_number": 1}]}}
                for name in ("R1", "R2", "R3")
            },
        },
    )

    assert report["result_status"] == "nonofficial-run"
    assert report["official_claim_allowed"] is False
    assert report["run_coverage_complete"] is True


def test_duplicate_qrels_question_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate qrels question ID"):
        evaluate_run_document(
            {
                "annotation_status": "confirmed",
                "questions": [
                    {"id": "Q1", "evidence": [{"document": "paper.pdf", "page_number": 1}]},
                    {"id": "Q1", "evidence": [{"document": "paper.pdf", "page_number": 2}]},
                ],
            },
            {"strategies": {}},
        )
