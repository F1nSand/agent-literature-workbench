"""Run bounded SiliconFlow M0 capability probes without exposing the API key."""

from __future__ import annotations

import base64
import json
import re
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pypdfium2 as pdfium
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://api.siliconflow.cn/v1"
REPORT_PATH = ROOT / "experiments" / "capabilities" / "m0-siliconflow-report.json"
MAX_OUTPUT_TOKENS = 128
DEEPSEEK_PRICES_CNY_PER_MILLION = {"prompt_tokens": 4.0, "completion_tokens": 6.0}


class ProbeFailure(Exception):
    def __init__(self, status: int | None, provider_code: str | int | None) -> None:
        super().__init__("provider request failed")
        self.status = status
        self.provider_code = provider_code


def load_key() -> str:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        raise ProbeFailure(None, "missing_env_file")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "SILICONFLOW_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value and not value.lstrip().startswith("#"):
                return value
            break
    raise ProbeFailure(None, "missing_api_key")


def request_json(api_key: str, method: str, path: str, body: dict | None = None) -> dict:
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{BASE_URL}{path}",
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        provider_code: str | int | None = None
        try:
            error_body = json.loads(error.read().decode("utf-8"))
            provider_code = error_body.get("code")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
        raise ProbeFailure(error.code, provider_code) from None
    except (URLError, TimeoutError, OSError):
        raise ProbeFailure(None, "network_error") from None


def usage_from(response: dict) -> dict[str, int]:
    usage = response.get("usage") or {}
    return {
        str(key): int(value)
        for key, value in usage.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def render_jpeg_data_url(pdf_path: Path, page_number: int) -> tuple[str, int]:
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        page = document[page_number - 1]
        try:
            bitmap = page.render(scale=1.15)
            image = bitmap.to_pil().convert("RGB")
            image.thumbnail((960, 1240))
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=76, optimize=True)
            raw = buffer.getvalue()
        finally:
            page.close()
    finally:
        document.close()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"), len(raw)


def render_diagram_crop_data_url(pdf_path: Path, page_number: int) -> tuple[str, int]:
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        page = document[page_number - 1]
        try:
            image = page.render(scale=1.8).to_pil().convert("RGB")
            image = image.crop((0, 0, image.width, int(image.height * 0.48)))
            image.thumbnail((1280, 920))
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=80, optimize=True)
            raw = buffer.getvalue()
        finally:
            page.close()
    finally:
        document.close()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"), len(raw)


def write_report(report: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def call_probe(api_key: str, report: dict, label: str, model: str, payload: dict) -> dict:
    started = time.monotonic()
    try:
        response = request_json(api_key, "POST", "/chat/completions", payload)
        choice = response.get("choices", [{}])[0]
        message = choice.get("message", {})
        safe_message = {
            key: message[key]
            for key in ("content", "tool_calls")
            if key in message
        }
        safe_message["finish_reason"] = choice.get("finish_reason")
        result = {
            "status": "ok",
            "model": response.get("model", model),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "usage": usage_from(response),
            "message": safe_message,
        }
    except ProbeFailure as error:
        result = {
            "status": "failed",
            "model": model,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "http_status": error.status,
            "provider_code": error.provider_code,
        }
    report["probes"][label] = result
    write_report(report)
    return result


def run_vision_followup() -> int:
    try:
        api_key = load_key()
        available_models = {
            item.get("id")
            for item in request_json(api_key, "GET", "/models?sub_type=chat").get("data", [])
            if isinstance(item, dict)
        }
    except ProbeFailure as error:
        print(json.dumps({"status": "blocked", "http_status": error.status, "provider_code": error.provider_code}))
        return 2

    if not REPORT_PATH.is_file():
        print(json.dumps({"status": "blocked", "reason": "initial_report_missing"}))
        return 2
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    model = "Qwen/Qwen3-VL-32B-Instruct"
    if model not in available_models:
        report["probes"]["vision_mobia_diagram_crop_32b"] = {
            "status": "unavailable_in_account",
            "model": model,
        }
        write_report(report)
        print(json.dumps(report["probes"]["vision_mobia_diagram_crop_32b"], ensure_ascii=False))
        return 0

    mobi_matches = list((ROOT / "示例论文文档与问题集").glob("04*.pdf"))
    if not mobi_matches:
        print(json.dumps({"status": "blocked", "reason": "sample_pdf_missing"}))
        return 2
    image_data, image_bytes = render_diagram_crop_data_url(mobi_matches[0], 5)
    if "vision_mobia_page_5" in report["probes"]:
        report["probes"]["vision_mobia_page_5"]["human_quality_review"] = (
            "The 8B response did not identify the figure's numbered top-level stages and stopped at the output-token limit; "
            "do not treat it as a successful diagram extraction."
        )
    report["selected_models"]["vision_followup"] = model
    report["probe_limits"]["maximum_billed_requests"] = 7
    report["probe_limits"]["vision_crop_image_bytes"] = image_bytes
    report["probe_limits"]["vision_followup_max_output_tokens"] = 256
    result = call_probe(
        api_key,
        report,
        "vision_mobia_diagram_crop_32b",
        model,
        {
            "model": model,
            "temperature": 0,
            "max_tokens": 256,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "请只根据图本身回答：编号 1、2、3 的三个主阶段各是什么？箭头表示怎样的顺序或反馈？如果读不清某项就明确说读不清，不要用图下方正文补答。",
                        },
                        {"type": "image_url", "image_url": {"url": image_data}},
                    ],
                }
            ],
        },
    )
    message = result.get("message", {})
    report["probes"]["vision_mobia_diagram_crop_32b"]["message"] = str(
        message.get("content") or ""
    )[:900]
    report["probes"]["vision_mobia_diagram_crop_32b"]["quality_review"] = (
        "Human review pending; compare named stages and arrows against the source page."
    )
    write_report(report)
    print(
        json.dumps(
            {
                "status": result.get("status"),
                "model": model,
                "usage": result.get("usage", {}),
                "message": report["probes"]["vision_mobia_diagram_crop_32b"]["message"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def normalize_marker(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", value).casefold()


def main() -> int:
    if "--vision-followup" in sys.argv:
        return run_vision_followup()
    try:
        api_key = load_key()
        model_response = request_json(api_key, "GET", "/models")
    except ProbeFailure as error:
        safe_result = {"status": "blocked", "http_status": error.status, "provider_code": error.provider_code}
        print(json.dumps(safe_result, ensure_ascii=False))
        return 2

    available_models = {
        item.get("id") for item in model_response.get("data", []) if isinstance(item, dict)
    }
    selected = {
        "vision_ocr": "Qwen/Qwen3-VL-8B-Instruct",
        "chat_tool": "deepseek-ai/DeepSeek-V3.2",
        "embedding": "Qwen/Qwen3-Embedding-4B",
        "reranker": "BAAI/bge-reranker-v2-m3",
    }
    report = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider": "SiliconFlow",
        "base_url": BASE_URL,
        "account_model_count": len(available_models),
        "selected_models": selected,
        "probe_limits": {
            "approved_total_budget_cny": 10,
            "maximum_billed_requests": 6,
            "max_output_tokens_per_chat_request": MAX_OUTPUT_TOKENS,
            "image_max_edge_px": 1240,
            "automatic_retries": 0,
        },
        "price_notes": {
            "deepseek_v3_2": {
                "source": "https://docs.siliconflow.cn/docs/release-notes/overview",
                "cny_per_million_tokens": DEEPSEEK_PRICES_CNY_PER_MILLION,
            },
            "other_models": "Exact account/model rates are not exposed by GET /models; token usage is recorded and no bulk calls are made.",
        },
        "probes": {},
    }
    write_report(report)

    for purpose, model in selected.items():
        if model not in available_models:
            report["probes"][purpose] = {"status": "unavailable_in_account", "model": model}
            write_report(report)

    embedding_model = selected["embedding"]
    if report["probes"].get("embedding", {}).get("status") != "unavailable_in_account":
        started = time.monotonic()
        try:
            response = request_json(
                api_key,
                "POST",
                "/embeddings",
                {
                    "model": embedding_model,
                    "input": "长程智能体的研究记忆与工具调用机制。",
                    "dimensions": 2560,
                    "encoding_format": "float",
                },
            )
            vector = response.get("data", [{}])[0].get("embedding", [])
            report["probes"]["embedding"] = {
                "status": "ok" if len(vector) == 2560 else "wrong_dimension",
                "model": response.get("model", embedding_model),
                "dimensions": len(vector),
                "latency_ms": round((time.monotonic() - started) * 1000),
                "usage": usage_from(response),
            }
        except ProbeFailure as error:
            report["probes"]["embedding"] = {
                "status": "failed",
                "model": embedding_model,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "http_status": error.status,
                "provider_code": error.provider_code,
            }
        write_report(report)

    reranker_model = selected["reranker"]
    if report["probes"].get("reranker", {}).get("status") != "unavailable_in_account":
        started = time.monotonic()
        try:
            response = request_json(
                api_key,
                "POST",
                "/rerank",
                {
                    "model": reranker_model,
                    "query": "智能体如何使用工具检索专题文献？",
                    "documents": [
                        "语言模型智能体会依据研究问题调用检索工具，并将结果返回到专题范围。",
                        "本文研究城市道路中的长距离车辆轨迹预测。",
                        "系统可调用搜索工具查找文献，再根据返回证据进行回答。",
                    ],
                    "top_n": 3,
                    "return_documents": False,
                },
            )
            results = response.get("results", [])
            order = [item.get("index") for item in results]
            report["probes"]["reranker"] = {
                "status": "ok" if len(order) == 3 else "incomplete_results",
                "model": reranker_model,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "ranked_indices": order,
                "scores": [item.get("relevance_score") for item in results],
                "relevant_in_top_two": any(index in (0, 2) for index in order[:2]),
            }
        except ProbeFailure as error:
            report["probes"]["reranker"] = {
                "status": "failed",
                "model": reranker_model,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "http_status": error.status,
                "provider_code": error.provider_code,
            }
        write_report(report)

    tool_model = selected["chat_tool"]
    tool_call_result = None
    if report["probes"].get("chat_tool", {}).get("status") != "unavailable_in_account":
        tool_result = call_probe(
            api_key,
            report,
            "tool_call_first_leg",
            tool_model,
            {
                "model": tool_model,
                "temperature": 0,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "messages": [
                    {"role": "system", "content": "Use the provided function to find evidence. Call it once and keep the query concise."},
                    {"role": "user", "content": "请在当前专题中搜索 Agent 工具调用相关文献。"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "find_research_evidence",
                            "description": "Search a bounded local topic corpus for evidence.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"},
                                    "scope": {"type": "string", "enum": ["current_topic"]},
                                },
                                "required": ["query", "scope"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                "tool_choice": "auto",
            },
        )
        message = tool_result.get("message", {})
        tool_calls = message.get("tool_calls", [])
        safe_calls = []
        for item in tool_calls:
            function = item.get("function", {})
            try:
                arguments = json.loads(function.get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = {}
            safe_calls.append({"name": function.get("name"), "arguments": arguments})
        report["probes"]["tool_call_first_leg"]["message"] = {
            "tool_calls": safe_calls,
            "finish_reason": tool_result.get("message", {}).get("finish_reason"),
            "content": (tool_result.get("message", {}).get("content") or "")[:300],
        }
        write_report(report)
        if tool_result.get("status") == "ok" and safe_calls:
            tool_call_result = (tool_calls[0], safe_calls[0])

    if tool_call_result:
        raw_call, safe_call = tool_call_result
        followup = call_probe(
            api_key,
            report,
            "tool_call_second_leg",
            tool_model,
            {
                "model": tool_model,
                "temperature": 0,
                "max_tokens": 64,
                "messages": [
                    {"role": "user", "content": "请在当前专题中搜索 Agent 工具调用相关文献。"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [raw_call],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": raw_call.get("id", "local-probe-call"),
                        "name": safe_call.get("name", "find_research_evidence"),
                        "content": json.dumps({"matches": 1, "evidence": "示例结果：Agent 工具调用"}, ensure_ascii=False),
                    },
                ],
            },
        )
        report["probes"]["tool_call_second_leg"]["message"] = {
            "content": (followup.get("message", {}).get("content") or "")[:300],
            "finish_reason": followup.get("message", {}).get("finish_reason"),
        }
        first_usage = report["probes"].get("tool_call_first_leg", {}).get("usage", {})
        second_usage = report["probes"].get("tool_call_second_leg", {}).get("usage", {})
        prompt_tokens = first_usage.get("prompt_tokens", 0) + second_usage.get("prompt_tokens", 0)
        completion_tokens = first_usage.get("completion_tokens", 0) + second_usage.get("completion_tokens", 0)
        report["probes"]["chat_tool"] = {
            "status": "roundtrip_ok" if followup.get("status") == "ok" else "roundtrip_incomplete",
            "model": tool_model,
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            "estimated_cost_cny": round(
                prompt_tokens * DEEPSEEK_PRICES_CNY_PER_MILLION["prompt_tokens"] / 1_000_000
                + completion_tokens * DEEPSEEK_PRICES_CNY_PER_MILLION["completion_tokens"] / 1_000_000,
                8,
            ),
        }
        write_report(report)

    vision_model = selected["vision_ocr"]
    if report["probes"].get("vision_ocr", {}).get("status") != "unavailable_in_account":
        fixtures = ROOT / "evals" / "datasets" / "fixtures" / "synthetic-scan-page.pdf"
        mobi_matches = list((ROOT / "示例论文文档与问题集").glob("04*.pdf"))
        try:
            fixture_data, fixture_bytes = render_jpeg_data_url(fixtures, 1)
            report["probe_limits"]["ocr_image_bytes"] = fixture_bytes
            ocr = call_probe(
                api_key,
                report,
                "ocr_synthetic_scan",
                vision_model,
                {
                    "model": vision_model,
                    "temperature": 0,
                    "max_tokens": MAX_OUTPUT_TOKENS,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "逐行识别图片里可见的中文和英文，不要补写。只返回文字。"},
                                {"type": "image_url", "image_url": {"url": fixture_data}},
                            ],
                        }
                    ],
                },
            )
            ocr_text = str(ocr.get("message", {}).get("content") or "")
            expected = ["合成 OCR 测试样本", "物理页码：1", "ALPHA 42 BETA", "引文定位、页面识别"]
            normalized_output = normalize_marker(ocr_text)
            report["probes"]["ocr_synthetic_scan"]["message"] = ocr_text[:500]
            report["probes"]["ocr_synthetic_scan"]["expected_markers"] = expected
            report["probes"]["ocr_synthetic_scan"]["matched_markers"] = [
                marker for marker in expected if normalize_marker(marker) in normalized_output
            ]
            report["probes"]["ocr_synthetic_scan"]["marker_recall"] = round(
                len(report["probes"]["ocr_synthetic_scan"]["matched_markers"]) / len(expected), 3
            )
            write_report(report)
        except (ProbeFailure, OSError, ValueError):
            if "ocr_synthetic_scan" not in report["probes"]:
                report["probes"]["ocr_synthetic_scan"] = {"status": "local_fixture_error"}
                write_report(report)

        if mobi_matches:
            try:
                figure_data, figure_bytes = render_jpeg_data_url(mobi_matches[0], 5)
                report["probe_limits"]["vision_image_bytes"] = figure_bytes
                vision = call_probe(
                    api_key,
                    report,
                    "vision_mobia_page_5",
                    vision_model,
                    {
                        "model": vision_model,
                        "temperature": 0,
                        "max_tokens": MAX_OUTPUT_TOKENS,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "请用不超过三点概括这页图中的主流程，并指出是否存在反馈或迭代箭头；看不清的部分请说明。"},
                                    {"type": "image_url", "image_url": {"url": figure_data}},
                                ],
                            }
                        ],
                    },
                )
                report["probes"]["vision_mobia_page_5"]["message"] = str(
                    vision.get("message", {}).get("content") or ""
                )[:600]
                write_report(report)
            except (ProbeFailure, OSError, ValueError):
                if "vision_mobia_page_5" not in report["probes"]:
                    report["probes"]["vision_mobia_page_5"] = {"status": "local_fixture_error"}
                    write_report(report)

    api_key = ""
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
