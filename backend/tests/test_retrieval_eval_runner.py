from collections import defaultdict
from uuid import UUID

import pytest

from app.documents.retrieval_eval_runner import CandidateEvaluationClient, RankedUnit, aggregate_pages, build_rrf_ranking
from app.providers.siliconflow import ProviderError


def test_aggregate_pages_deduplicates_chunks_and_keeps_first_vector_page_order() -> None:
    first = UUID("00000000-0000-0000-0000-000000000001")
    duplicate_page = UUID("00000000-0000-0000-0000-000000000002")
    second_page = UUID("00000000-0000-0000-0000-000000000003")
    units = {
        first: RankedUnit(first, "paper.pdf", 4, 0, "one"),
        duplicate_page: RankedUnit(duplicate_page, "paper.pdf", 4, 1, "two"),
        second_page: RankedUnit(second_page, "paper.pdf", 7, 0, "three"),
    }

    assert aggregate_pages([first, duplicate_page, second_page], units) == [
        {"document": "paper.pdf", "page_number": 4},
        {"document": "paper.pdf", "page_number": 7},
    ]


def test_rrf_combines_both_lists_and_uses_dense_rank_for_ties() -> None:
    dense_only = UUID("00000000-0000-0000-0000-000000000001")
    both = UUID("00000000-0000-0000-0000-000000000002")
    lexical_only = UUID("00000000-0000-0000-0000-000000000003")

    ranked, scores = build_rrf_ranking(
        [both, dense_only],
        [lexical_only, both],
        {both: 1, dense_only: 2},
    )

    assert ranked == [both, lexical_only, dense_only]
    assert scores[both] == 1 / 61 + 1 / 62
    assert scores[lexical_only] == 1 / 61


def test_candidate_evaluation_provider_enforces_eight_reranker_calls() -> None:
    client = object.__new__(CandidateEvaluationClient)
    client._request_count = 0
    client._purpose_counts = defaultdict(int)
    client._purpose_tokens = defaultdict(int)

    for _ in range(8):
        client._budget_check("reranker", estimated_prompt_tokens=100)

    with pytest.raises(ProviderError, match="evaluation_reranker_request_cap_reached"):
        client._budget_check("reranker", estimated_prompt_tokens=100)
    assert client._request_count == 8


def test_candidate_evaluation_provider_rejects_disallowed_purposes() -> None:
    client = object.__new__(CandidateEvaluationClient)
    client._request_count = 0
    client._purpose_counts = defaultdict(int)
    client._purpose_tokens = defaultdict(int)

    with pytest.raises(ProviderError, match="evaluation_purpose_not_allowed"):
        client._budget_check("chat", estimated_prompt_tokens=1)
    assert client._request_count == 0
