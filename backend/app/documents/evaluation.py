from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


EvidenceKey = tuple[str, int]


def _evidence_key(row: Mapping[str, Any]) -> EvidenceKey | None:
    document = row.get("document")
    page_number = row.get("page_number")
    if not isinstance(document, str) or not document.strip():
        return None
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        return None
    return document, page_number


def _metric_mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def evaluate_rankings(
    qrels: Mapping[str, Sequence[Mapping[str, Any]]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    cutoff: int = 8,
    latency_ms: Mapping[str, int | float] | None = None,
    request_cost_cny: Mapping[str, int | float | None] | None = None,
) -> dict[str, Any]:
    """Score saved evidence rankings. This function never calls a model or a database."""
    if cutoff < 1:
        raise ValueError("cutoff must be positive")

    valid_qrels: dict[str, set[EvidenceKey]] = {}
    for question_id, rows in qrels.items():
        relevant = {key for row in rows if (key := _evidence_key(row)) is not None}
        if relevant:
            valid_qrels[question_id] = relevant

    recalls: list[float] = []
    hits: list[float] = []
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    costs: list[float] = []
    latency_values_complete = True
    cost_values_complete = True
    ranked_count = 0

    for question_id, relevant in valid_qrels.items():
        has_ranking = question_id in rankings
        ranked_rows = rankings.get(question_id, ())
        if has_ranking:
            ranked_count += 1

        ordered: list[EvidenceKey] = []
        seen: set[EvidenceKey] = set()
        for row in ranked_rows:
            key = _evidence_key(row)
            if key is not None and key not in seen:
                seen.add(key)
                ordered.append(key)
        top = ordered[:cutoff]
        matching_ranks = [rank for rank, key in enumerate(top, start=1) if key in relevant]
        recalls.append(len(set(top) & relevant) / len(relevant))
        hits.append(float(bool(matching_ranks)))
        reciprocal_ranks.append(1 / matching_ranks[0] if matching_ranks else 0.0)

        latency = latency_ms.get(question_id) if latency_ms is not None and has_ranking else None
        if isinstance(latency, (int, float)) and not isinstance(latency, bool) and latency >= 0:
            latencies.append(float(latency))
        else:
            latency_values_complete = False
        if request_cost_cny is not None:
            cost = request_cost_cny.get(question_id) if has_ranking else None
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
                costs.append(float(cost))
            else:
                cost_values_complete = False

    return {
        "cutoff": cutoff,
        "questions_ranked": ranked_count,
        "questions_with_qrels": len(valid_qrels),
        "questions_missing_rankings": len(valid_qrels) - ranked_count,
        "evidence_recall_at_k": _metric_mean(recalls),
        "hit_at_k": _metric_mean(hits),
        "mrr_at_k": _metric_mean(reciprocal_ranks),
        "latency_questions": len(latencies),
        "mean_latency_ms": _metric_mean(latencies) if latency_values_complete and valid_qrels and len(latencies) == len(valid_qrels) else None,
        "request_cost_cny": round(sum(costs), 6) if cost_values_complete and valid_qrels and len(costs) == len(valid_qrels) else None,
    }


def evaluate_run_document(qrels_data: Mapping[str, Any], run_data: Mapping[str, Any], cutoff: int = 8) -> dict[str, Any]:
    questions = qrels_data.get("questions")
    strategies = run_data.get("strategies")
    if not isinstance(questions, list) or not isinstance(strategies, dict):
        raise ValueError("qrels must contain questions and run data must contain strategies")

    qrels: dict[str, list[Mapping[str, Any]]] = {}
    for question in questions:
        if not isinstance(question, dict) or not isinstance(question.get("id"), str):
            raise ValueError("each qrels question must have a string id")
        if question["id"] in qrels:
            raise ValueError(f"duplicate qrels question ID: {question['id']}")
        evidence = question.get("candidate_evidence", question.get("evidence", []))
        if not isinstance(evidence, list):
            raise ValueError(f"evidence for {question['id']} must be a list")
        qrels[question["id"]] = [row for row in evidence if isinstance(row, dict)]

    results: dict[str, Any] = {}
    scorable_questions = sum(
        bool(any(_evidence_key(row) is not None for row in evidence)) for evidence in qrels.values()
    )
    for name in ("R1", "R2", "R3"):
        strategy = strategies.get(name)
        if not isinstance(strategy, dict):
            results[name] = {
                "questions_ranked": 0,
                "questions_with_qrels": scorable_questions,
                "questions_missing_rankings": scorable_questions,
                "status": "missing",
            }
            continue
        rankings = strategy.get("rankings", {})
        if not isinstance(rankings, dict):
            raise ValueError(f"rankings for {name} must be an object")
        safe_rankings = {
            question_id: [row for row in rows if isinstance(row, dict)]
            for question_id, rows in rankings.items()
            if isinstance(question_id, str) and isinstance(rows, list)
        }
        metrics = evaluate_rankings(
            qrels,
            safe_rankings,
            cutoff=cutoff,
            latency_ms=strategy.get("latency_ms_by_question") if isinstance(strategy.get("latency_ms_by_question"), dict) else None,
            request_cost_cny=strategy.get("request_cost_cny_by_question") if isinstance(strategy.get("request_cost_cny_by_question"), dict) else None,
        )
        if metrics["questions_ranked"] == 0:
            strategy_status = "no_rankings"
        elif metrics["questions_missing_rankings"]:
            strategy_status = "partial"
        else:
            strategy_status = "scored"
        results[name] = {"status": strategy_status, **metrics}

    annotation_status = qrels_data.get("annotation_status", "unknown")
    qrels_manifest_hash = qrels_data.get("corpus_manifest_sha256")
    run_manifest_hash = run_data.get("corpus_manifest_sha256")
    corpus_match = bool(
        isinstance(qrels_manifest_hash, str)
        and qrels_manifest_hash
        and isinstance(run_manifest_hash, str)
        and run_manifest_hash == qrels_manifest_hash
    )
    run_coverage_complete = bool(scorable_questions) and all(
        results[name].get("status") == "scored"
        and results[name].get("questions_ranked") == scorable_questions
        for name in ("R1", "R2", "R3")
    )
    run_scope_confirmed = (
        run_data.get("evaluation_scope") == "confirmed-evaluation"
        and run_data.get("official_claim_allowed") is True
    )
    result_status = (
        "candidate-exploration"
        if annotation_status != "confirmed"
        else "corpus-mismatch" if not corpus_match
        else "incomplete-run" if not run_coverage_complete
        else "confirmed-evaluation" if run_scope_confirmed else "nonofficial-run"
    )
    return {
        "dataset_id": qrels_data.get("dataset_id"),
        "run_id": run_data.get("run_id"),
        "annotation_status": annotation_status,
        "result_status": result_status,
        "official_claim_allowed": (
            annotation_status == "confirmed"
            and corpus_match
            and run_coverage_complete
            and run_scope_confirmed
        ),
        "scorable_question_count": scorable_questions,
        "run_coverage_complete": run_coverage_complete,
        "corpus_match": corpus_match,
        "corpus_manifest_sha256": qrels_manifest_hash,
        "run_corpus_manifest_sha256": run_manifest_hash,
        "results": results,
    }
