from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_engine
from app.documents.models import ProviderCall


MAX_REQUESTS_PER_TASK = 64
MAX_RERANK_CANDIDATES = 40
PURPOSE_LIMITS = {"ocr": 32, "vision": 8, "embedding": 16, "reranker": 8, "chat": 16}
PROMPT_TOKEN_LIMITS = {"embedding": 120_000}
OUTPUT_TOKEN_LIMITS = {"ocr": 8_192, "vision": 8_192, "chat": 64_000}
MODEL_PRICE_RATES = {
    "deepseek-ai/DeepSeek-V3.2": (4.0, 6.0, "https://siliconflow.cn/pricing"),
    "Qwen/Qwen3-VL-8B-Instruct": (0.5, 2.0, "https://siliconflow.cn/pricing (checked 2026-09-29)"),
    "Qwen/Qwen3-VL-32B-Instruct": (1.0, 4.0, "https://siliconflow.cn/pricing (checked 2026-09-29)"),
    "Qwen/Qwen3-Embedding-4B": (0.14, 0.0, "https://siliconflow.cn/pricing (checked 2026-09-29)"),
    "BAAI/bge-reranker-v2-m3": (0.0, 0.0, "https://siliconflow.cn/pricing (listed free, checked 2026-09-29)"),
}


class ProviderError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class ProviderResult:
    data: dict[str, Any]
    prompt_tokens: int | None
    completion_tokens: int | None
    estimated_prompt_tokens: int | None
    latency_ms: int


@dataclass(frozen=True)
class ProviderBudget:
    max_requests: int
    purpose_limits: dict[str, int]
    max_input_tokens: int
    max_output_tokens: int
    # The default deliberately preserves pre-change runs and callers.
    max_chat_output_tokens: int = 512


def estimate_tokens(text: str) -> int:
    """Conservative mixed-script estimate used only for pre-request caps."""
    parts = re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+|[^\w\s]", text)
    total = 0
    for part in parts:
        if re.fullmatch(r"[A-Za-z0-9]+", part):
            total += max(1, math.ceil(len(part) / 3))
        else:
            total += 1
    return total


class SiliconFlowClient:
    def __init__(
        self,
        document_id: UUID | None,
        task_id: UUID,
        *,
        run_id: UUID | None = None,
        budget: ProviderBudget | None = None,
    ):
        self.document_id = document_id
        self.task_id = task_id
        self.run_id = run_id
        self.budget = budget
        self.settings = get_settings()
        if self.settings.siliconflow_api_key is None or not self.settings.siliconflow_api_key.get_secret_value():
            raise ProviderError("provider_key_missing")
        self._key = self.settings.siliconflow_api_key.get_secret_value()

    def _budget_check(self, purpose: str, requested_output_tokens: int = 0, estimated_prompt_tokens: int = 0) -> None:
        if purpose not in PURPOSE_LIMITS:
            raise ProviderError("provider_purpose_invalid")
        if self.run_id is not None and self.budget is not None:
            with Session(get_engine()) as session:
                from app.jobs.models import BackgroundTask
                from app.runs.models import Run

                run = session.get(Run, self.run_id)
                if run is None:
                    raise ProviderError("run_budget_unavailable")
                task_ids = session.scalars(
                    select(BackgroundTask.id).where(
                        BackgroundTask.task_type == "research.run",
                        BackgroundTask.payload["run_id"].as_string() == str(self.run_id),
                    )
                ).all()
                calls = session.scalars(
                    select(ProviderCall).where(ProviderCall.task_id.in_(task_ids))
                ).all() if task_ids else []
                if len(calls) >= self.budget.max_requests:
                    raise ProviderError("run_provider_request_budget_reached")
                purpose_calls = [call for call in calls if call.purpose == purpose]
                purpose_limit = self.budget.purpose_limits.get(purpose, 0)
                if purpose_limit <= 0 or len(purpose_calls) >= purpose_limit:
                    raise ProviderError("run_provider_purpose_budget_reached")
                if (run.input_tokens or 0) + estimated_prompt_tokens > self.budget.max_input_tokens:
                    raise ProviderError("run_input_token_budget_reached")
                if (run.output_tokens or 0) + requested_output_tokens > self.budget.max_output_tokens:
                    raise ProviderError("run_output_token_budget_reached")
                output_cap = (
                    self.budget.max_chat_output_tokens
                    if purpose == "chat" else OUTPUT_TOKEN_LIMITS.get(purpose)
                )
                if output_cap is not None and requested_output_tokens > output_cap:
                    raise ProviderError("run_single_request_output_budget_reached")
            return
        with Session(get_engine()) as session:
            calls = session.scalars(
                select(ProviderCall).where(ProviderCall.task_id == self.task_id)
            ).all()
        if len(calls) >= MAX_REQUESTS_PER_TASK:
            raise ProviderError("stage_request_limit_reached")
        purpose_calls = [call for call in calls if call.purpose == purpose]
        if len(purpose_calls) >= PURPOSE_LIMITS[purpose]:
            raise ProviderError("stage_purpose_request_limit_reached")
        if purpose in PROMPT_TOKEN_LIMITS:
            already_used = sum(
                call.prompt_tokens if call.prompt_tokens is not None else (call.estimated_prompt_tokens or 0)
                for call in purpose_calls
            )
            if already_used + estimated_prompt_tokens > PROMPT_TOKEN_LIMITS[purpose]:
                raise ProviderError("stage_prompt_token_limit_reached")
        if purpose in OUTPUT_TOKEN_LIMITS:
            output_cap = OUTPUT_TOKEN_LIMITS[purpose]
            if requested_output_tokens <= 0 or requested_output_tokens > output_cap:
                raise ProviderError("stage_output_token_limit_reached")

    def check_embedding_capacity(self, estimated_prompt_tokens: int) -> None:
        self._budget_check("embedding", estimated_prompt_tokens=estimated_prompt_tokens)

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
        unknown_output_reservation: int = 0,
    ) -> None:
        input_rate, output_rate, source = MODEL_PRICE_RATES.get(model, (None, None, None))
        estimated_cost = None
        if input_rate == 0 and output_rate == 0:
            estimated_cost = 0.0
        elif input_rate is not None and output_rate is not None:
            billable_prompt_tokens = prompt_tokens if prompt_tokens is not None else estimated_prompt_tokens
            if billable_prompt_tokens is not None and (completion_tokens is not None or output_rate == 0):
                estimated_cost = (
                    billable_prompt_tokens * input_rate + (completion_tokens or 0) * output_rate
                ) / 1_000_000
        with Session(get_engine()) as session:
            session.add(
                ProviderCall(
                    document_id=self.document_id,
                    task_id=self.task_id,
                    purpose=purpose,
                    model=model,
                    status=status,
                    prompt_tokens=prompt_tokens,
                    estimated_prompt_tokens=estimated_prompt_tokens,
                    completion_tokens=completion_tokens,
                    estimated_cost_cny=estimated_cost,
                    rate_input_cny_per_million=input_rate,
                    rate_output_cny_per_million=output_rate,
                    rate_source=source,
                    provider_trace_id=provider_trace_id,
                    latency_ms=latency_ms,
                    error_code=error_code,
                )
            )
            if self.run_id is not None:
                from app.runs.models import Run

                run = session.scalar(select(Run).where(Run.id == self.run_id).with_for_update())
                if run is not None:
                    billed_prompt_tokens = prompt_tokens if prompt_tokens is not None else (estimated_prompt_tokens or 0)
                    run.input_tokens = (run.input_tokens or 0) + billed_prompt_tokens
                    accounted_output = completion_tokens if completion_tokens is not None else unknown_output_reservation
                    run.output_tokens = (run.output_tokens or 0) + accounted_output
            session.commit()

    def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        purpose: str,
        model: str,
        requested_output_tokens: int = 0,
        estimated_prompt_tokens: int = 0,
    ) -> ProviderResult:
        self._budget_check(purpose, requested_output_tokens, estimated_prompt_tokens)
        started = time.perf_counter()
        trace_id = None
        try:
            timeout_seconds = 900.0 if purpose == "chat" else (180.0 if purpose in {"vision", "ocr"} else 120.0)
            response = httpx.post(
                f"{self.settings.siliconflow_base_url.rstrip('/')}/{endpoint.lstrip('/')}",
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=httpx.Timeout(timeout_seconds, connect=15.0),
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            trace_id = response.headers.get("x-siliconcloud-trace-id") or response.headers.get("x-trace-id")
            if response.status_code >= 400:
                error_code = f"provider_http_{response.status_code}"
                # A server-side 5xx can arrive after generation began; preserve
                # the request cap when the provider cannot report its usage.
                status = "unknown" if response.status_code >= 500 else "failed"
                self._record(
                    purpose, model, status, None, None, estimated_prompt_tokens or None,
                    latency_ms, trace_id, error_code,
                    unknown_output_reservation=requested_output_tokens if status == "unknown" else 0,
                )
                raise ProviderError(error_code)
            try:
                data = response.json()
            except ValueError:
                self._record(
                    purpose, model, "unknown", None, None, estimated_prompt_tokens or None,
                    latency_ms, trace_id, "provider_invalid_response",
                    unknown_output_reservation=requested_output_tokens,
                )
                raise ProviderError("provider_invalid_response") from None
            usage = data.get("usage") if isinstance(data, dict) else None
            prompt_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
            completion_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
            if purpose == "embedding" and prompt_tokens is None and isinstance(usage, dict):
                prompt_tokens = usage.get("total_tokens")
            if purpose in {"chat", "vision"}:
                status = "succeeded" if prompt_tokens is not None and completion_tokens is not None else "unknown"
            else:
                status = "succeeded" if prompt_tokens is not None or purpose == "reranker" else "unknown"
            self._record(
                purpose,
                model,
                status,
                prompt_tokens,
                completion_tokens,
                estimated_prompt_tokens or None,
                latency_ms,
                trace_id,
                None if status == "succeeded" else "provider_usage_missing",
                unknown_output_reservation=requested_output_tokens if status == "unknown" else 0,
            )
            if self.run_id is not None and purpose in {"chat", "vision"} and completion_tokens is None:
                raise ProviderError("provider_usage_missing")
            return ProviderResult(
                data=data,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_prompt_tokens=estimated_prompt_tokens or None,
                latency_ms=latency_ms,
            )
        except ProviderError:
            raise
        except httpx.TimeoutException:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self._record(
                purpose, model, "unknown", None, None, estimated_prompt_tokens or None,
                latency_ms, trace_id, "provider_timeout",
                unknown_output_reservation=requested_output_tokens,
            )
            raise ProviderError("provider_timeout") from None
        except httpx.RequestError:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self._record(
                purpose, model, "unknown", None, None, estimated_prompt_tokens or None,
                latency_ms, trace_id, "provider_transport_error",
                unknown_output_reservation=requested_output_tokens,
            )
            raise ProviderError("provider_transport_error") from None

    def embed(
        self,
        inputs: list[str],
        model: str | None = None,
        dimensions: int | None = None,
    ) -> tuple[list[list[float]], ProviderResult]:
        if not inputs or len(inputs) > 256:
            raise ProviderError("embedding_unit_limit_reached")
        active_model = model or self.settings.siliconflow_embedding_model
        active_dimensions = dimensions or self.settings.embedding_dimensions
        if active_dimensions != 2560:
            raise ProviderError("embedding_dimension_unsupported")
        estimate = sum(estimate_tokens(value) for value in inputs)
        result = self._post(
            "/embeddings",
            {
                "model": active_model,
                "input": inputs,
                "dimensions": active_dimensions,
                "encoding_format": "float",
                "truncate": "right",
            },
            purpose="embedding",
            model=active_model,
            estimated_prompt_tokens=estimate,
        )
        data = result.data.get("data")
        if not isinstance(data, list) or len(data) != len(inputs):
            raise ProviderError("embedding_response_count_mismatch")
        vectors: list[list[float] | None] = [None] * len(inputs)
        for row in data:
            index = row.get("index")
            vector = row.get("embedding")
            if not isinstance(index, int) or index < 0 or index >= len(inputs) or not isinstance(vector, list):
                raise ProviderError("embedding_response_invalid")
            if len(vector) != active_dimensions:
                raise ProviderError("embedding_dimension_mismatch")
            vectors[index] = vector
        if any(vector is None for vector in vectors):
            raise ProviderError("embedding_response_incomplete")
        return [vector for vector in vectors if vector is not None], result

    def rerank(self, query: str, documents: list[str]) -> tuple[list[dict[str, Any]], ProviderResult]:
        if not documents or len(documents) > MAX_RERANK_CANDIDATES:
            raise ProviderError("rerank_candidate_limit_reached")
        result = self._post(
            "/rerank",
            {"model": self.settings.siliconflow_reranker_model, "query": query, "documents": documents, "top_n": min(8, len(documents))},
            purpose="reranker",
            model=self.settings.siliconflow_reranker_model,
            estimated_prompt_tokens=estimate_tokens(query + "\n" + "\n".join(documents)),
        )
        rows = result.data.get("results")
        if not isinstance(rows, list):
            raise ProviderError("rerank_response_invalid")
        return rows, result

    def chat(self, payload: dict[str, Any], max_tokens: int = 64_000) -> ProviderResult:
        if max_tokens <= 0 or max_tokens > OUTPUT_TOKEN_LIMITS["chat"]:
            raise ProviderError("stage_output_token_limit_reached")
        if self.run_id is not None and self.budget is not None:
            with Session(get_engine()) as session:
                from app.runs.models import Run

                run = session.get(Run, self.run_id)
                if run is None:
                    raise ProviderError("run_budget_unavailable")
                max_tokens = min(
                    max_tokens,
                    self.budget.max_chat_output_tokens,
                    self.budget.max_output_tokens - (run.output_tokens or 0),
                )
                if max_tokens <= 0:
                    raise ProviderError("run_output_token_budget_reached")
        messages = payload.get("messages", [])
        text = "\n".join(
            str(message.get("content", ""))
            for message in messages
            if isinstance(message, dict)
        )
        return self._post(
            "/chat/completions",
            {**payload, "model": self.settings.siliconflow_chat_model, "max_tokens": max_tokens},
            purpose="chat",
            model=self.settings.siliconflow_chat_model,
            requested_output_tokens=max_tokens,
            estimated_prompt_tokens=estimate_tokens(text),
        )

    def vision(self, image_data_url: str, prompt: str, max_tokens: int = 8_192) -> ProviderResult:
        if max_tokens <= 0 or max_tokens > OUTPUT_TOKEN_LIMITS["vision"]:
            raise ProviderError("stage_output_token_limit_reached")
        return self._post(
            "/chat/completions",
            {
                "model": self.settings.siliconflow_vision_model,
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ]}
                ],
                "max_tokens": max_tokens,
                "temperature": 0,
            },
            purpose="vision",
            model=self.settings.siliconflow_vision_model,
            requested_output_tokens=max_tokens,
            estimated_prompt_tokens=estimate_tokens(prompt),
        )

    def ocr(self, image_data_url: str, prompt: str, max_tokens: int = 8_192) -> ProviderResult:
        if max_tokens <= 0 or max_tokens > OUTPUT_TOKEN_LIMITS["ocr"]:
            raise ProviderError("stage_output_token_limit_reached")
        return self._post(
            "/chat/completions",
            {
                "model": self.settings.siliconflow_ocr_model,
                "messages": [
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ]}
                ],
                "max_tokens": max_tokens,
                "temperature": 0,
            },
            purpose="ocr",
            model=self.settings.siliconflow_ocr_model,
            requested_output_tokens=max_tokens,
            estimated_prompt_tokens=estimate_tokens(prompt),
        )
