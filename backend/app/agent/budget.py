from app.providers.siliconflow import ProviderBudget


MAX_TOOL_CALLS = 48
MAX_MODEL_STEPS = 64
MAX_WALL_CLOCK_SECONDS = 1_800
MAX_INPUT_TOKENS = 512_000
MAX_OUTPUT_TOKENS = 128_000
MAX_CHAT_OUTPUT_TOKENS = 64_000
MAX_PROVIDER_REQUESTS = 160
PROVIDER_PURPOSE_LIMITS = {
    "chat": 64,
    "embedding": 48,
    "reranker": 48,
    "vision": 8,
    "ocr": 8,
}


def default_budget_snapshot() -> dict:
    return {
        "max_tool_calls": MAX_TOOL_CALLS,
        "max_model_steps": MAX_MODEL_STEPS,
        "max_wall_clock_seconds": MAX_WALL_CLOCK_SECONDS,
        "max_input_tokens": MAX_INPUT_TOKENS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_chat_output_tokens": MAX_CHAT_OUTPUT_TOKENS,
        "max_provider_requests": MAX_PROVIDER_REQUESTS,
        "provider_purpose_limits": dict(PROVIDER_PURPOSE_LIMITS),
    }


def provider_budget_from_snapshot(snapshot: dict) -> ProviderBudget:
    return ProviderBudget(
        max_requests=int(snapshot["max_provider_requests"]),
        purpose_limits={str(key): int(value) for key, value in snapshot["provider_purpose_limits"].items()},
        max_input_tokens=int(snapshot["max_input_tokens"]),
        max_output_tokens=int(snapshot["max_output_tokens"]),
        # Runs created before the per-call setting existed retain the original
        # 512-token chat ceiling from the executor/provider implementation.
        max_chat_output_tokens=int(snapshot.get("max_chat_output_tokens", 512)),
    )
