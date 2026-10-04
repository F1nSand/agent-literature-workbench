from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import cast, func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_engine
from app.documents.models import (
    ContentUnit,
    ContentUnitEmbedding,
    Document,
    DocumentVersion,
    EmbeddingCache,
    IndexGeneration,
)
from app.documents.pipeline import _cached_embeddings, _embedding_cache_key, _lexical_terms
from app.providers.siliconflow import (
    MAX_RERANK_CANDIDATES,
    MODEL_PRICE_RATES,
    OUTPUT_TOKEN_LIMITS,
    ProviderError,
    ProviderResult,
    SiliconFlowClient,
    estimate_tokens,
)


QUERY_EMBEDDING_TOKEN_CAP = 8_000
RERANKER_PROMPT_TOKEN_CAP = 1_350_000
MAX_PROVIDER_REQUESTS = 9
MAX_RERANKER_REQUESTS = 8
MAX_ESTIMATED_COST_CNY = 0.01
MAX_PAGE_TEXT_CHARS = 4_000
VECTOR_CANDIDATES = 30
LEXICAL_CANDIDATES = 30
RRF_CANDIDATES = 40
RESULT_PAGES = 8
RRF_K = 60


@dataclass(frozen=True)
class ActivePaper:
    id: UUID
    filename: str
    sha256: str
    page_count: int
    version_id: UUID


@dataclass(frozen=True)
class RankedUnit:
    id: UUID
    filename: str
    page_number: int
    ordinal: int
    text: str


def aggregate_pages(
    ordered_ids: list[UUID],
    units: dict[UUID, RankedUnit],
    scores: dict[UUID, float] | None = None,
    *,
    limit: int = RESULT_PAGES,
) -> list[dict[str, Any]]:
    """Collapse content-unit rankings into unique physical document pages."""
    page_scores: dict[tuple[str, int], float] = {}
    first_seen: dict[tuple[str, int], int] = {}
    for rank, unit_id in enumerate(ordered_ids, start=1):
        unit = units.get(unit_id)
        if unit is None:
            continue
        key = (unit.filename, unit.page_number)
        first_seen.setdefault(key, rank)
        score = (scores or {}).get(unit_id, -float(rank))
        page_scores[key] = max(page_scores.get(key, -float("inf")), score)

    ordered_pages = sorted(
        page_scores,
        key=lambda key: (-page_scores[key], first_seen[key], key[0], key[1]),
    )[:limit]
    return [{"document": filename, "page_number": page_number} for filename, page_number in ordered_pages]


def build_rrf_ranking(
    dense_ids: list[UUID],
    lexical_ids: list[UUID],
    exact_dense_rank: dict[UUID, int],
    *,
    limit: int = RRF_CANDIDATES,
) -> tuple[list[UUID], dict[UUID, float]]:
    scores: dict[UUID, float] = defaultdict(float)
    for ranked_ids in (dense_ids, lexical_ids):
        for rank, unit_id in enumerate(ranked_ids, start=1):
            scores[unit_id] += 1 / (RRF_K + rank)
    ranked = sorted(
        scores,
        key=lambda unit_id: (-scores[unit_id], exact_dense_rank.get(unit_id, 10_000), unit_id.hex),
    )[:limit]
    return ranked, dict(scores)


class CandidateEvaluationClient(SiliconFlowClient):
    """Provider adapter with a strict run-local cap and no product task records."""

    def __init__(self) -> None:
        self.settings = get_settings()
        if self.settings.siliconflow_api_key is None or not self.settings.siliconflow_api_key.get_secret_value():
            raise ProviderError("provider_key_missing")
        self._key = self.settings.siliconflow_api_key.get_secret_value()
        self.document_id = None
        self.task_id = None
        self._request_count = 0
        self._purpose_counts: dict[str, int] = defaultdict(int)
        self._purpose_tokens: dict[str, int] = defaultdict(int)
        self.provider_calls: list[dict[str, Any]] = []

    def _budget_check(self, purpose: str, requested_output_tokens: int = 0, estimated_prompt_tokens: int = 0) -> None:
        if self._request_count >= MAX_PROVIDER_REQUESTS:
            raise ProviderError("evaluation_request_cap_reached")
        if purpose == "embedding":
            if self._purpose_counts[purpose] >= 1:
                raise ProviderError("evaluation_embedding_request_cap_reached")
            if self._purpose_tokens[purpose] + estimated_prompt_tokens > QUERY_EMBEDDING_TOKEN_CAP:
                raise ProviderError("evaluation_embedding_token_cap_reached")
        elif purpose == "reranker":
            if self._purpose_counts[purpose] >= MAX_RERANKER_REQUESTS:
                raise ProviderError("evaluation_reranker_request_cap_reached")
            if self._purpose_tokens[purpose] + estimated_prompt_tokens > RERANKER_PROMPT_TOKEN_CAP:
                raise ProviderError("evaluation_reranker_token_cap_reached")
        else:
            raise ProviderError("evaluation_purpose_not_allowed")
        if purpose in OUTPUT_TOKEN_LIMITS and requested_output_tokens:
            if requested_output_tokens > OUTPUT_TOKEN_LIMITS[purpose]:
                raise ProviderError("stage_output_token_limit_reached")
        self._request_count += 1
        self._purpose_counts[purpose] += 1
        self._purpose_tokens[purpose] += estimated_prompt_tokens

    def _record(
        self,
        purpose: str,
        model: str,
        status: str,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        estimated_prompt_tokens: int | None,
        latency_ms: int | None,
        provider_trace_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        input_rate, output_rate, rate_source = MODEL_PRICE_RATES.get(model, (None, None, None))
        billable_prompt_tokens = prompt_tokens if prompt_tokens is not None else estimated_prompt_tokens
        estimated_cost = None
        if input_rate == 0 and output_rate == 0:
            estimated_cost = 0.0
        elif input_rate is not None and output_rate is not None and billable_prompt_tokens is not None:
            if completion_tokens is not None or output_rate == 0:
                estimated_cost = (billable_prompt_tokens * input_rate + (completion_tokens or 0) * output_rate) / 1_000_000
        self.provider_calls.append({
            "purpose": purpose,
            "model": model,
            "status": status,
            "prompt_tokens": prompt_tokens,
            "estimated_prompt_tokens": estimated_prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated_cost_cny": estimated_cost,
            "rate_source": rate_source,
            "latency_ms": latency_ms,
            "provider_trace_id": provider_trace_id,
            "error_code": error_code,
        })


def load_qrels(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise ValueError("qrels must be a JSON object containing questions")
    if not all(isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("query"), str) for row in data["questions"]):
        raise ValueError("every question must include string id and query fields")
    return data


def _active_snapshot(session: Session, manifest: dict[str, Any]) -> tuple[list[ActivePaper], IndexGeneration]:
    expected_rows = manifest.get("pdf_inventory")
    if not isinstance(expected_rows, list) or not expected_rows:
        raise ValueError("corpus manifest must contain pdf_inventory")
    expected = {row.get("filename"): row for row in expected_rows if isinstance(row, dict)}
    rows = session.execute(
        select(Document.id, Document.filename, DocumentVersion.sha256, DocumentVersion.page_count, DocumentVersion.id)
        .join(DocumentVersion, DocumentVersion.id == Document.active_version_id)
        .where(
            Document.is_deleted.is_(False),
            Document.status == "ready",
            DocumentVersion.status == "ready",
            Document.filename.in_(list(expected)),
        )
        .order_by(Document.filename)
    ).all()
    papers = [ActivePaper(row[0], row[1], row[2].upper(), row[3], row[4]) for row in rows]
    actual = {row.filename: row for row in papers}
    missing = set(expected) - set(actual)
    mismatches = []
    for filename in set(expected) & set(actual):
        wanted = expected[filename]
        paper = actual[filename]
        if wanted.get("sha256", "").upper() != paper.sha256 or wanted.get("pages") != paper.page_count:
            mismatches.append(filename)
    if missing or mismatches:
        raise ValueError(f"active sample corpus does not match manifest (missing={sorted(missing)}, mismatched={sorted(mismatches)})")
    generation = session.scalar(select(IndexGeneration).where(IndexGeneration.status == "active"))
    if generation is None:
        raise ValueError("active index generation not found")
    return papers, generation


def _cache_misses(session: Session, queries: list[str], model: str) -> list[str]:
    keys = {query: _embedding_cache_key(query, "query", model) for query in dict.fromkeys(queries)}
    cached = set(session.scalars(select(EmbeddingCache.cache_key).where(EmbeddingCache.cache_key.in_(list(keys.values())))).all())
    return [query for query, key in keys.items() if key not in cached]


def preflight(qrels_path: Path, manifest_path: Path) -> dict[str, Any]:
    qrels = load_qrels(qrels_path)
    raw_manifest = manifest_path.read_bytes()
    manifest = json.loads(raw_manifest.decode("utf-8"))
    manifest_hash = hashlib.sha256(raw_manifest).hexdigest().upper()
    if qrels.get("corpus_manifest_sha256", "").upper() != manifest_hash:
        raise ValueError("qrels corpus hash does not match the supplied corpus manifest")
    queries = [question["query"] for question in qrels["questions"]]
    estimated_embedding_tokens = sum(estimate_tokens(query) for query in queries)
    if estimated_embedding_tokens > QUERY_EMBEDDING_TOKEN_CAP:
        raise ValueError("query embeddings exceed the evaluation token cap")
    with Session(get_engine()) as session:
        papers, generation = _active_snapshot(session, manifest)
        cache_misses = _cache_misses(session, queries, generation.model)
    missing_query_indexes = [index for index, query in enumerate(queries) if query in set(cache_misses)]
    embedding_rate = MODEL_PRICE_RATES.get(generation.model, (None, None, None))[0]
    if embedding_rate is None:
        estimated_embedding_cost = None
    else:
        estimated_embedding_cost = sum(estimate_tokens(query) for query in cache_misses) * embedding_rate / 1_000_000
    if estimated_embedding_cost is not None and estimated_embedding_cost > MAX_ESTIMATED_COST_CNY:
        raise ValueError("estimated candidate run cost exceeds the evaluation cost cap")
    return {
        "qrels": qrels,
        "manifest": manifest,
        "manifest_sha256": manifest_hash,
        "papers": papers,
        "generation_id": generation.id,
        "embedding_model": generation.model,
        "queries": queries,
        "query_count": len(queries),
        "embedding_cache_hits": len(queries) - len(cache_misses),
        "embedding_cache_misses": len(cache_misses),
        "missing_query_indexes": missing_query_indexes,
        "estimated_embedding_tokens": estimated_embedding_tokens,
        "estimated_provider_requests_max": (1 if cache_misses else 0) + len(queries),
        "estimated_embedding_cost_cny_max": estimated_embedding_cost,
        "reranker_prompt_tokens_cap": RERANKER_PROMPT_TOKEN_CAP,
        "reranker_model": get_settings().siliconflow_reranker_model,
        "reranker_candidates_per_question": MAX_RERANK_CANDIDATES,
        "estimated_reranker_cost_cny": 0.0 if MODEL_PRICE_RATES.get(get_settings().siliconflow_reranker_model, (None, None, None))[0:2] == (0.0, 0.0) else None,
    }


def _retrieval_config() -> dict[str, Any]:
    return {
        "vector_candidates": VECTOR_CANDIDATES,
        "lexical_candidates": LEXICAL_CANDIDATES,
        "rrf_k": RRF_K,
        "rrf_candidates": RRF_CANDIDATES,
        "reranker_candidates_max": MAX_RERANK_CANDIDATES,
        "page_text_max_chars": MAX_PAGE_TEXT_CHARS,
        "result_pages": RESULT_PAGES,
        "reranker_input": "each candidate page concatenates up to four highest-RRF-ranked units, truncated to 4000 characters",
    }


def _stage_caps() -> dict[str, Any]:
    return {
        "max_provider_requests": MAX_PROVIDER_REQUESTS,
        "max_reranker_requests": MAX_RERANKER_REQUESTS,
        "max_embedding_query_tokens": QUERY_EMBEDDING_TOKEN_CAP,
        "max_reranker_prompt_tokens": RERANKER_PROMPT_TOKEN_CAP,
        "max_estimated_cost_cny": MAX_ESTIMATED_COST_CNY,
        "reranker_candidates_per_question": MAX_RERANK_CANDIDATES,
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _manifest_path(output_path: Path) -> Path:
    return output_path.with_name(output_path.name + ".manifest.json")


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _candidate_run_id(plan: dict[str, Any], qrels_path: Path, *, new_run: bool) -> tuple[str, str]:
    qrels_hash = _sha256_file(qrels_path)
    identity = {
        "qrels_sha256": qrels_hash,
        "corpus_manifest_sha256": plan["manifest_sha256"],
        "corpus": [
            {"filename": paper.filename, "sha256": paper.sha256, "page_count": paper.page_count, "version_id": str(paper.version_id)}
            for paper in plan["papers"]
        ],
        "index_generation_id": str(plan["generation_id"]),
        "embedding_model": plan["embedding_model"],
        "reranker_model": plan["reranker_model"],
        "retrieval_config": _retrieval_config(),
        "stage_caps": _stage_caps(),
    }
    if new_run:
        identity["explicit_new_run_nonce"] = uuid4().hex
    stable = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"candidate-{hashlib.sha256(stable).hexdigest()[:20]}", qrels_hash


def _build_run_manifest(plan: dict[str, Any], run_id: str, qrels_hash: str) -> dict[str, Any]:
    models = {plan["embedding_model"], plan["reranker_model"]}
    price_snapshot = {}
    for model in sorted(models):
        input_rate, output_rate, rate_source = MODEL_PRICE_RATES.get(model, (None, None, None))
        price_snapshot[model] = {
            "input_cny_per_million_tokens": input_rate,
            "output_cny_per_million_tokens": output_rate,
            "source": rate_source,
        }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "execution_status": "planned",
        "evaluation_scope": "candidate-exploration",
        "annotation_status": plan["qrels"].get("annotation_status", "unknown"),
        "official_claim_allowed": False,
        "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_id": plan["qrels"].get("dataset_id"),
        "qrels_source_review": plan["qrels"].get("source_review"),
        "qrels_sha256": qrels_hash,
        "corpus_manifest_sha256": plan["manifest_sha256"],
        "query_count": plan["query_count"],
        "corpus": [
            {"filename": paper.filename, "sha256": paper.sha256, "page_count": paper.page_count, "version_id": str(paper.version_id)}
            for paper in plan["papers"]
        ],
        "index_generation_id": str(plan["generation_id"]),
        "models": {"embedding": plan["embedding_model"], "reranker": plan["reranker_model"]},
        "retrieval_config": _retrieval_config(),
        "cache_snapshot": {
            "query_embedding_hits": plan["embedding_cache_hits"],
            "query_embedding_misses": plan["embedding_cache_misses"],
        },
        "stage_caps": _stage_caps(),
        "preflight_estimates": {
            "query_embedding_tokens": plan["estimated_embedding_tokens"],
            "provider_requests_max": plan["estimated_provider_requests_max"],
            "embedding_cost_cny_max": plan["estimated_embedding_cost_cny_max"],
            "reranker_cost_cny": plan["estimated_reranker_cost_cny"],
        },
        "price_snapshot": price_snapshot,
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _fetch_rankings(
    question: str,
    query_vector: list[float],
    papers: list[ActivePaper],
    generation_id: UUID,
    dimensions: int,
    client: CandidateEvaluationClient,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int, int, dict[str, Any] | None]:
    document_ids = [paper.id for paper in papers]
    version_ids = [paper.version_id for paper in papers]
    filename_by_id = {paper.id: paper.filename for paper in papers}
    started_retrieval = time.perf_counter()
    started_dense = time.perf_counter()
    with Session(get_engine()) as session, session.begin():
        session.execute(text("SET LOCAL hnsw.ef_search = 40"))
        session.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))
        half_distance = cast(ContentUnitEmbedding.embedding, HALFVEC(dimensions)).cosine_distance(query_vector).label("distance")
        dense_ann = session.execute(
            select(ContentUnitEmbedding.content_unit_id.label("id"), half_distance)
            .join(ContentUnit, ContentUnit.id == ContentUnitEmbedding.content_unit_id)
            .join(Document, Document.id == ContentUnit.document_id)
            .where(
                Document.id.in_(document_ids),
                Document.is_deleted.is_(False),
                Document.active_version_id == ContentUnit.version_id,
                ContentUnit.version_id.in_(version_ids),
                ContentUnitEmbedding.generation_id == generation_id,
            )
            .order_by(half_distance)
            .limit(VECTOR_CANDIDATES)
        ).all()
        dense_ann_ids = [row.id for row in dense_ann]
        exact_distance = ContentUnitEmbedding.embedding.cosine_distance(query_vector).label("distance")
        dense_exact = session.execute(
            select(ContentUnitEmbedding.content_unit_id.label("id"), exact_distance)
            .join(ContentUnit, ContentUnit.id == ContentUnitEmbedding.content_unit_id)
            .join(Document, Document.id == ContentUnit.document_id)
            .where(
                Document.id.in_(document_ids),
                Document.is_deleted.is_(False),
                Document.active_version_id == ContentUnit.version_id,
                ContentUnit.version_id.in_(version_ids),
                ContentUnitEmbedding.generation_id == generation_id,
                ContentUnitEmbedding.content_unit_id.in_(dense_ann_ids),
            )
            .order_by(exact_distance)
        ).all() if dense_ann_ids else []
        dense_ids = [row.id for row in dense_exact]
        exact_rank = {unit_id: rank for rank, unit_id in enumerate(dense_ids, start=1)}
    with Session(get_engine()) as session:
        dense_unit_rows = session.execute(
            select(
                ContentUnit.id.label("unit_id"),
                ContentUnit.page_number,
                ContentUnit.ordinal,
                Document.filename,
            )
            .join(Document, Document.id == ContentUnit.document_id)
            .where(
                ContentUnit.id.in_(dense_ids),
                Document.active_version_id == ContentUnit.version_id,
                Document.is_deleted.is_(False),
                ContentUnit.version_id.in_(version_ids),
            )
        ).all() if dense_ids else []
    dense_units = {
        row.unit_id: RankedUnit(row.unit_id, row.filename, row.page_number, row.ordinal, "")
        for row in dense_unit_rows
    }
    r1_pages = aggregate_pages(dense_ids, dense_units)
    dense_elapsed = int((time.perf_counter() - started_dense) * 1000)

    terms = _lexical_terms(question)
    lexical_ids: list[UUID] = []
    if terms:
        with Session(get_engine()) as session:
            lexical_rows = session.execute(
                select(
                    ContentUnit.id,
                    func.ts_rank_cd(
                        func.to_tsvector("simple", ContentUnit.search_text),
                        func.to_tsquery("simple", " | ".join(terms)),
                    ).label("score"),
                )
                .join(Document, Document.id == ContentUnit.document_id)
                .where(
                    Document.id.in_(document_ids),
                    Document.is_deleted.is_(False),
                    Document.active_version_id == ContentUnit.version_id,
                    ContentUnit.version_id.in_(version_ids),
                    func.to_tsvector("simple", ContentUnit.search_text).op("@@")(func.to_tsquery("simple", " | ".join(terms))),
                )
                .order_by(text("score DESC"), ContentUnit.id)
                .limit(LEXICAL_CANDIDATES)
            ).all()
        lexical_ids = [row.id for row in lexical_rows]
    rrf_ids, rrf_scores = build_rrf_ranking(dense_ids, lexical_ids, exact_rank)
    all_ids = list(dict.fromkeys([*dense_ids, *rrf_ids]))
    with Session(get_engine()) as session:
        unit_rows = session.execute(
            select(
                ContentUnit.id.label("unit_id"),
                ContentUnit.page_number,
                ContentUnit.ordinal,
                ContentUnit.text,
                Document.id.label("document_id"),
                Document.filename,
            )
            .join(Document, Document.id == ContentUnit.document_id)
            .where(
                ContentUnit.id.in_(all_ids),
                Document.active_version_id == ContentUnit.version_id,
                Document.is_deleted.is_(False),
                ContentUnit.version_id.in_(version_ids),
            )
        ).all() if all_ids else []
    units = {
        row.unit_id: RankedUnit(row.unit_id, filename_by_id.get(row.document_id, row.filename), row.page_number, row.ordinal, row.text)
        for row in unit_rows
    }
    rrf_page_scores: dict[UUID, float] = {
        unit_id: rrf_scores[unit_id] for unit_id in rrf_ids if unit_id in units
    }
    r2_pages = aggregate_pages(rrf_ids, units, rrf_page_scores)
    rrf_elapsed = int((time.perf_counter() - started_retrieval) * 1000)
    page_units: dict[tuple[str, int], list[UUID]] = defaultdict(list)
    for unit_id in rrf_ids:
        unit = units.get(unit_id)
        if unit is not None:
            page_units[(unit.filename, unit.page_number)].append(unit_id)
    rerank_pages = sorted(
        page_units,
        key=lambda page: (
            -max(rrf_scores[unit_id] for unit_id in page_units[page]),
            min(exact_rank.get(unit_id, 10_000) for unit_id in page_units[page]),
            page[0],
            page[1],
        ),
    )[:RRF_CANDIDATES]
    rerank_texts = [
        "\n".join(units[unit_id].text for unit_id in sorted(page_units[page], key=lambda uid: (-rrf_scores[uid], units[uid].ordinal))[:4])[:MAX_PAGE_TEXT_CHARS]
        for page in rerank_pages
    ]
    rerank_result: ProviderResult | None = None
    if rerank_texts:
        reranked, rerank_result = client.rerank(question, rerank_texts)
        scored_pages: list[tuple[float, tuple[str, int]]] = []
        for item in reranked:
            index = item.get("index")
            if isinstance(index, int) and 0 <= index < len(rerank_pages):
                try:
                    score = float(item.get("relevance_score", 0.0))
                except (TypeError, ValueError):
                    score = 0.0
                scored_pages.append((score, rerank_pages[index]))
        r3_pages = [
            {"document": page[0], "page_number": page[1]}
            for _score, page in sorted(scored_pages, key=lambda row: (-row[0], row[1][0], row[1][1]))[:RESULT_PAGES]
        ]
    else:
        r3_pages = []
    return r1_pages, r2_pages, r3_pages, dense_elapsed, rrf_elapsed, client.provider_calls[-1] if rerank_result is not None else None


def execute(preflight_data: dict[str, Any], run_id: str) -> dict[str, Any]:
    qrels = preflight_data["qrels"]
    if not preflight_data["papers"]:
        raise ValueError("no sample documents matched the corpus manifest")
    with Session(get_engine()) as session:
        active_papers, active_generation = _active_snapshot(session, preflight_data["manifest"])
    frozen_versions = {paper.id: paper.version_id for paper in preflight_data["papers"]}
    current_versions = {paper.id: paper.version_id for paper in active_papers}
    if frozen_versions != current_versions or active_generation.id != preflight_data["generation_id"]:
        raise ValueError("sample corpus or active index changed after preflight; rerun preflight")
    client = CandidateEvaluationClient()
    queries = preflight_data["queries"]
    with Session(get_engine()) as session:
        missing_queries_at_execution = set(_cache_misses(session, queries, preflight_data["embedding_model"]))
    started_embedding = time.perf_counter()
    query_vectors, embedding_result, cache_hits = _cached_embeddings(
        client,
        queries,
        "query",
        preflight_data["embedding_model"],
    )
    embedding_batch_ms = int((time.perf_counter() - started_embedding) * 1000)
    if len(query_vectors) != len(queries):
        raise ValueError("query embedding count mismatch")
    if embedding_result is not None and embedding_result.prompt_tokens is not None and embedding_result.prompt_tokens > QUERY_EMBEDDING_TOKEN_CAP:
        raise ValueError("provider-reported query embedding tokens exceeded the cap")

    strategies: dict[str, dict[str, Any]] = {
        name: {"rankings": {}, "latency_ms_by_question": {}, "request_cost_cny_by_question": {}}
        for name in ("R1", "R2", "R3")
    }
    embedding_call = next((row for row in client.provider_calls if row["purpose"] == "embedding"), None)
    embedding_cost = embedding_call["estimated_cost_cny"] if embedding_call is not None else 0.0
    missing_query_indexes = {
        index for index, query in enumerate(queries) if query in missing_queries_at_execution
    }
    weights = [estimate_tokens(query) for query in queries]
    missing_weight_total = sum(weights[index] for index in missing_query_indexes)
    embedding_cost_by_question = [0.0] * len(queries)
    if embedding_cost is not None and missing_weight_total:
        for index in missing_query_indexes:
            embedding_cost_by_question[index] = embedding_cost * weights[index] / missing_weight_total

    per_question_latency: dict[str, dict[str, int]] = {name: {} for name in ("R1", "R2", "R3")}
    reranker_calls: list[dict[str, Any] | None] = []
    embedding_share_ms = round(embedding_batch_ms / max(1, len(queries)))
    for index, question in enumerate(qrels["questions"]):
        r1, r2, r3, dense_elapsed, rrf_elapsed, rerank_call = _fetch_rankings(
            question["query"],
            query_vectors[index],
            preflight_data["papers"],
            preflight_data["generation_id"],
            2560,
            client,
        )
        question_id = question["id"]
        for name, ranking in (("R1", r1), ("R2", r2), ("R3", r3)):
            strategies[name]["rankings"][question_id] = ranking
            strategies[name]["request_cost_cny_by_question"][question_id] = embedding_cost_by_question[index]
        per_question_latency["R1"][question_id] = embedding_share_ms + dense_elapsed
        per_question_latency["R2"][question_id] = embedding_share_ms + rrf_elapsed
        reranker_latency = rerank_call["latency_ms"] if rerank_call and isinstance(rerank_call.get("latency_ms"), int) else 0
        per_question_latency["R3"][question_id] = embedding_share_ms + rrf_elapsed + reranker_latency
        if rerank_call:
            reranker_cost = rerank_call.get("estimated_cost_cny")
            existing = strategies["R3"]["request_cost_cny_by_question"][question_id]
            strategies["R3"]["request_cost_cny_by_question"][question_id] = (
                existing + reranker_cost if existing is not None and reranker_cost is not None else None
            )
        reranker_calls.append(rerank_call)
    for name in strategies:
        strategies[name]["latency_ms_by_question"] = per_question_latency[name]

    total_estimated_cost = None
    costs = [row.get("estimated_cost_cny") for row in client.provider_calls]
    if all(cost is not None for cost in costs):
        total_estimated_cost = round(sum(costs), 6)
    if total_estimated_cost is not None and total_estimated_cost > MAX_ESTIMATED_COST_CNY:
        raise ValueError("actual estimated candidate run cost exceeded the cost cap")
    if client._request_count > MAX_PROVIDER_REQUESTS or client._purpose_counts["reranker"] > MAX_RERANKER_REQUESTS:
        raise ValueError("candidate run exceeded provider request caps")
    return {
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "evaluation_scope": "candidate-exploration",
        "annotation_status": qrels.get("annotation_status", "unknown"),
        "official_claim_allowed": False,
        "corpus_manifest_sha256": preflight_data["manifest_sha256"],
        "dataset_id": qrels.get("dataset_id"),
        "qrels_source_review": qrels.get("source_review"),
        "query_count": len(queries),
        "corpus": [{"filename": paper.filename, "sha256": paper.sha256, "page_count": paper.page_count, "document_id": str(paper.id), "version_id": str(paper.version_id)} for paper in preflight_data["papers"]],
        "index_generation_id": str(preflight_data["generation_id"]),
        "embedding_model": preflight_data["embedding_model"],
        "retrieval_config": {
            **_retrieval_config(),
            "query_embedding_cache_hits": cache_hits,
            "query_embedding_batch_latency_ms": embedding_batch_ms,
            "query_embedding_latency_apportioned_equally": True,
        },
        "budget": {
            **_stage_caps(),
            "provider_requests_made": client._request_count,
            "provider_tokens_by_purpose": dict(client._purpose_tokens),
            "estimated_cost_cny": total_estimated_cost,
            "provider_calls": client.provider_calls,
        },
        "strategies": strategies,
    }


def generate(
    qrels_path: Path,
    corpus_manifest_path: Path,
    output_path: Path,
    *,
    run: bool = False,
    new_run: bool = False,
) -> dict[str, Any]:
    if new_run and not run:
        raise ValueError("--new-run requires provider execution")
    plan = preflight(qrels_path, corpus_manifest_path)
    run_id, qrels_hash = _candidate_run_id(plan, qrels_path, new_run=new_run)
    if not run:
        return {
            "mode": "dry-run",
            "run_id": run_id,
            "evaluation_scope": "candidate-exploration",
            "official_claim_allowed": False,
            "query_count": plan["query_count"],
            "sample_documents": [{"filename": paper.filename, "sha256": paper.sha256, "page_count": paper.page_count} for paper in plan["papers"]],
            "index_generation_id": str(plan["generation_id"]),
            "embedding_model": plan["embedding_model"],
            "embedding_cache_hits": plan["embedding_cache_hits"],
            "embedding_cache_misses": plan["embedding_cache_misses"],
            "estimated_embedding_tokens": plan["estimated_embedding_tokens"],
            "estimated_provider_requests_max": plan["estimated_provider_requests_max"],
            "estimated_embedding_cost_cny_max": plan["estimated_embedding_cost_cny_max"],
            "reranker_candidates_per_question": plan["reranker_candidates_per_question"],
            "reranker_prompt_tokens_cap": plan["reranker_prompt_tokens_cap"],
            "estimated_reranker_cost_cny": plan["estimated_reranker_cost_cny"],
            "max_estimated_cost_cny": MAX_ESTIMATED_COST_CNY,
            "output_path": str(output_path),
            "provider_calls_made": 0,
        }

    run_manifest_path = _manifest_path(output_path)
    if output_path.exists():
        if run_manifest_path.exists():
            existing_manifest = _read_json_object(run_manifest_path)
            if existing_manifest.get("run_id") == run_id:
                if existing_manifest.get("execution_status") != "completed":
                    raise RuntimeError(
                        "run output exists with an incomplete manifest; inspect the artifacts instead of rerunning provider calls"
                    )
                expected_hash = existing_manifest.get("run_output_sha256")
                if not isinstance(expected_hash, str) or _sha256_file(output_path) != expected_hash:
                    raise RuntimeError("completed run output checksum does not match its manifest")
                if existing_manifest.get("run_output_filename") != output_path.name:
                    raise RuntimeError("completed run output filename does not match its manifest")
                existing_run = _read_json_object(output_path)
                if existing_run.get("run_id") != run_id:
                    raise RuntimeError("completed run output ID does not match its manifest")
                return existing_run
        raise RuntimeError("run output already exists; choose a new output path instead of overwriting an evaluation artifact")
    if run_manifest_path.exists():
        existing_manifest = _read_json_object(run_manifest_path)
        if existing_manifest.get("run_id") == run_id:
            raise RuntimeError(
                "a manifest for this run already exists without a completed output; provider calls will not be repeated automatically"
            )
        raise RuntimeError("run manifest already exists; choose a new output path")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = _build_run_manifest(plan, run_id, qrels_hash)
    _write_json_atomic(run_manifest_path, manifest)
    manifest["execution_status"] = "running"
    manifest["started_at_utc"] = datetime.now(timezone.utc).isoformat()
    _write_json_atomic(run_manifest_path, manifest)
    try:
        run_data = execute(plan, run_id)
    except Exception as error:
        manifest["execution_status"] = "failed"
        error_code = getattr(error, "error_code", "evaluation_failed")
        manifest["error_code"] = error_code if isinstance(error_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", error_code) else "evaluation_failed"
        manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        _write_json_atomic(run_manifest_path, manifest)
        raise
    _write_json_atomic(output_path, run_data)
    manifest["execution_status"] = "completed"
    manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["run_output_filename"] = output_path.name
    manifest["run_output_sha256"] = _sha256_file(output_path)
    manifest["provider_requests_made"] = run_data.get("budget", {}).get("provider_requests_made")
    manifest["provider_calls"] = run_data.get("budget", {}).get("provider_calls", [])
    _write_json_atomic(run_manifest_path, manifest)
    return run_data
