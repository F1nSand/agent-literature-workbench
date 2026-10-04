from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.providers.siliconflow import ProviderBudget
from app.providers import siliconflow
from app.providers.siliconflow import ProviderError, ProviderResult, SiliconFlowClient
from app.runs.models import Run
from app.topics.models import Conversation
from app.jobs.models import BackgroundTask


def test_reranker_accepts_the_baseline_forty_candidates_and_rejects_more() -> None:
    client = object.__new__(SiliconFlowClient)
    client.settings = SimpleNamespace(siliconflow_reranker_model="BAAI/bge-reranker-v2-m3")
    client._budget_check = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    calls: list[list[str]] = []

    def fake_post(_endpoint: str, payload: dict, **_kwargs: object) -> ProviderResult:
        calls.append(payload["documents"])
        return ProviderResult(
            data={"results": [{"index": index, "relevance_score": 1.0 - index / 100} for index in range(8)]},
            prompt_tokens=10,
            completion_tokens=0,
            estimated_prompt_tokens=10,
            latency_ms=0,
        )

    client._post = fake_post  # type: ignore[method-assign]
    documents = [f"evidence {index}" for index in range(40)]

    rows, _result = client.rerank("research question", documents)

    assert len(calls) == 1
    assert calls[0] == documents
    assert len(rows) == 8
    with pytest.raises(ProviderError, match="rerank_candidate_limit_reached"):
        client.rerank("research question", [*documents, "candidate 41"])
    assert len(calls) == 1


def test_missing_provider_key_fails_before_any_request(monkeypatch) -> None:
    monkeypatch.setattr(siliconflow, "get_settings", lambda: SimpleNamespace(siliconflow_api_key=None))

    with pytest.raises(ProviderError, match="provider_key_missing"):
        SiliconFlowClient(None, uuid4())


def test_embedding_rejects_unsupported_and_mismatched_dimensions() -> None:
    client = object.__new__(SiliconFlowClient)
    client.settings = SimpleNamespace(siliconflow_embedding_model="test-embedding", embedding_dimensions=2560)
    calls = []

    def fake_post(endpoint: str, payload: dict, **kwargs: object) -> ProviderResult:
        calls.append((endpoint, payload, kwargs))
        return ProviderResult(
            data={"data": [{"index": 0, "embedding": [1.0]}]},
            prompt_tokens=1,
            completion_tokens=0,
            estimated_prompt_tokens=1,
            latency_ms=0,
        )

    client._post = fake_post  # type: ignore[method-assign]
    with pytest.raises(ProviderError, match="embedding_dimension_unsupported"):
        client.embed(["short input"], dimensions=1536)
    assert calls == []

    with pytest.raises(ProviderError, match="embedding_dimension_mismatch"):
        client.embed(["short input"], dimensions=2560)
    assert len(calls) == 1


def test_provider_http_error_does_not_echo_body_or_authorization_key(monkeypatch, caplog) -> None:
    secret = "sk-" + "fixture-" + "never-print-this"
    client = object.__new__(SiliconFlowClient)
    client.settings = SimpleNamespace(siliconflow_base_url="https://provider.invalid/v1")
    client._key = secret
    client.document_id = None
    client.task_id = uuid4()
    client.run_id = None
    client.budget = None
    client._budget_check = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    client._record = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    request = httpx.Request("POST", "https://provider.invalid/v1/embeddings")
    monkeypatch.setattr(
        siliconflow.httpx,
        "post",
        lambda *_args, **_kwargs: httpx.Response(401, request=request, text=secret),
    )

    with caplog.at_level("DEBUG"):
        with pytest.raises(ProviderError, match="provider_http_401") as error:
            client._post("/embeddings", {"input": ["x"]}, purpose="embedding", model="test")

    assert secret not in str(error.value)
    assert secret not in caplog.text


def test_default_chat_payload_and_vision_ocr_defaults_use_new_bounded_caps(monkeypatch) -> None:
    client = object.__new__(SiliconFlowClient)
    client.settings = SimpleNamespace(
        siliconflow_chat_model="test-chat",
        siliconflow_vision_model="test-vision",
        siliconflow_ocr_model="test-ocr",
    )
    client.run_id = None
    client.budget = None
    client.task_id = uuid4()
    requests = []

    def fake_post(endpoint, payload, **kwargs):
        requests.append((endpoint, payload, kwargs))
        return ProviderResult({}, 1, 1, 1, 0)

    client._post = fake_post  # type: ignore[method-assign]
    client.chat({"messages": [{"role": "user", "content": "64k output boundary"}]})
    assert requests[-1][1]["max_tokens"] == 64_000
    assert requests[-1][2]["requested_output_tokens"] == 64_000

    with pytest.raises(ProviderError, match="stage_output_token_limit_reached"):
        client.chat({"messages": []}, max_tokens=64_001)
    assert len(requests) == 1

    client.vision("data:image/jpeg;base64,AA==", "brief visual check")
    assert requests[-1][1]["max_tokens"] == 8_192
    client.ocr("data:image/jpeg;base64,AA==", "transcribe the page")
    assert requests[-1][1]["max_tokens"] == 8_192


def test_chat_request_timeout_is_long_enough_for_bounded_long_output(monkeypatch) -> None:
    client = object.__new__(SiliconFlowClient)
    client.settings = SimpleNamespace(siliconflow_base_url="https://provider.invalid/v1")
    client._key = "test-only-placeholder"
    client.run_id = None
    client.budget = None
    client.document_id = None
    client.task_id = uuid4()
    client._budget_check = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    client._record = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    captured = {}

    def fake_post(_url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    monkeypatch.setattr(siliconflow.httpx, "post", fake_post)
    client._post(
        "/chat/completions", {"max_tokens": 64_000}, purpose="chat", model="test-chat",
        requested_output_tokens=64_000, estimated_prompt_tokens=1,
    )

    assert captured["timeout"].connect == 15.0
    assert captured["timeout"].read == 900.0


def test_server_error_records_unknown_usage_and_reserves_requested_output(monkeypatch) -> None:
    client = object.__new__(SiliconFlowClient)
    client.settings = SimpleNamespace(siliconflow_base_url="https://provider.invalid/v1")
    client._key = "test-only-placeholder"
    client.run_id = None
    client.budget = None
    client.document_id = None
    client.task_id = uuid4()
    client._budget_check = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    recorded = []
    client._record = lambda *args, **kwargs: recorded.append((args, kwargs))  # type: ignore[method-assign]
    monkeypatch.setattr(
        siliconflow.httpx, "post",
        lambda *_args, **_kwargs: httpx.Response(503, text="provider unavailable"),
    )

    with pytest.raises(ProviderError, match="provider_http_503"):
        client._post(
            "/chat/completions", {"max_tokens": 64_000}, purpose="chat", model="test-chat",
            requested_output_tokens=64_000, estimated_prompt_tokens=123,
        )

    assert recorded[0][0][2] == "unknown"
    assert recorded[0][1]["unknown_output_reservation"] == 64_000


def test_unknown_chat_usage_reserves_budget_and_next_call_uses_remaining_total(monkeypatch) -> None:
    import os

    database_url = os.getenv("WORKBENCH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("set WORKBENCH_TEST_DATABASE_URL to run provider budget PostgreSQL test")

    engine = create_engine(database_url, pool_pre_ping=True)
    monkeypatch.setattr(siliconflow, "get_engine", lambda: engine)
    conversation_id, run_id, task_id = uuid4(), uuid4(), uuid4()
    with Session(engine) as session:
        session.add(Conversation(id=conversation_id, topic_id=None, title="Budget test"))
        session.flush()
        session.add(Run(
            id=run_id, conversation_id=conversation_id, status="running", scope_mode="library",
            scope_snapshot={"schema_version": 2, "mode": "library", "documents": []},
            scope_hash="a" * 64, budget_snapshot={},
        ))
        session.flush()
        session.add(BackgroundTask(
            id=task_id, task_type="research.run", status="running", payload={"run_id": str(run_id)},
            attempt_count=1, max_attempts=1, fencing_token=1, lease_owner="test",
        ))
        session.commit()

    client = object.__new__(SiliconFlowClient)
    client.settings = SimpleNamespace(siliconflow_base_url="https://provider.invalid/v1", siliconflow_chat_model="test-chat")
    client._key = "test-only-placeholder"
    client.document_id = None
    client.task_id = task_id
    client.run_id = run_id
    client.budget = ProviderBudget(
        max_requests=160,
        purpose_limits={"chat": 64, "embedding": 48, "reranker": 48, "vision": 8, "ocr": 8},
        max_input_tokens=512_000,
        max_output_tokens=128_000,
        max_chat_output_tokens=64_000,
    )
    responses = [
        httpx.ReadTimeout("simulated unknown usage"),
        httpx.Response(200, json={"usage": {"prompt_tokens": 100, "completion_tokens": 64_000}, "choices": []}),
    ]
    requested = []

    def fake_http_post(_url, *, json, **kwargs):
        requested.append(json["max_tokens"])
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(siliconflow.httpx, "post", fake_http_post)
    try:
        with pytest.raises(ProviderError, match="provider_timeout"):
            client.chat({"messages": [{"role": "user", "content": "reserve unknown usage"}]})
        with Session(engine) as session:
            run = session.get(Run, run_id)
            assert run.input_tokens == siliconflow.estimate_tokens("reserve unknown usage")
            assert run.output_tokens == 64_000
            unknown = session.scalar(select(siliconflow.ProviderCall).where(siliconflow.ProviderCall.task_id == task_id))
            assert unknown.status == "unknown" and unknown.completion_tokens is None

        client.chat({"messages": [{"role": "user", "content": "use remaining budget"}]})
        with Session(engine) as session:
            run = session.get(Run, run_id)
            assert run.output_tokens == 128_000
        with pytest.raises(ProviderError, match="run_output_token_budget_reached"):
            client.chat({"messages": [{"role": "user", "content": "must be blocked"}]})
        assert requested == [64_000, 64_000]
    finally:
        with Session(engine) as session:
            session.execute(delete(siliconflow.ProviderCall).where(siliconflow.ProviderCall.task_id == task_id))
            session.execute(delete(BackgroundTask).where(BackgroundTask.id == task_id))
            session.execute(delete(Run).where(Run.id == run_id))
            session.execute(delete(Conversation).where(Conversation.id == conversation_id))
            session.commit()
        engine.dispose()
