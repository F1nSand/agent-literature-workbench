from app.agent.budget import default_budget_snapshot, provider_budget_from_snapshot
from app.agent.executor import _chat_output_limit


def test_new_run_budget_uses_requested_run_and_provider_limits() -> None:
    snapshot = default_budget_snapshot()

    assert snapshot["max_tool_calls"] == 48
    assert snapshot["max_model_steps"] == 64
    assert snapshot["max_wall_clock_seconds"] == 1_800
    assert snapshot["max_input_tokens"] == 512_000
    assert snapshot["max_output_tokens"] == 128_000
    assert snapshot["max_chat_output_tokens"] == 64_000
    assert snapshot["max_provider_requests"] == 160
    assert snapshot["provider_purpose_limits"] == {
        "chat": 64, "embedding": 48, "reranker": 48, "vision": 8, "ocr": 8,
    }
    budget = provider_budget_from_snapshot(snapshot)
    assert budget.max_chat_output_tokens == 64_000


def test_legacy_frozen_budget_keeps_the_512_token_per_chat_ceiling() -> None:
    snapshot = {
        "max_tool_calls": 12,
        "max_model_steps": 16,
        "max_wall_clock_seconds": 600,
        "max_input_tokens": 24_000,
        "max_output_tokens": 4_096,
        "max_provider_requests": 40,
        "provider_purpose_limits": {"chat": 16, "embedding": 12, "reranker": 12, "vision": 2},
    }

    assert provider_budget_from_snapshot(snapshot).max_chat_output_tokens == 512
    assert _chat_output_limit(snapshot, 0) == 512
    assert _chat_output_limit(snapshot, 4_000) == 96


def test_chat_request_uses_single_and_cumulative_remaining_limits() -> None:
    snapshot = {"max_chat_output_tokens": 64_000, "max_output_tokens": 128_000}

    assert _chat_output_limit(snapshot, 0) == 64_000
    assert _chat_output_limit(snapshot, 100_000) == 28_000
    assert _chat_output_limit(snapshot, 128_000) == 1
