"""記錄 API 回傳的 usage；按版本化費率試算，並非供應商帳單。"""

import hashlib
import json
import math
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


def load_pricing(path: Path) -> dict:
    pricing = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(pricing, dict) or not isinstance(pricing.get("models"), dict):
        raise ValueError("費率檔必須包含 models 物件")
    for key, rates in pricing["models"].items():
        if not isinstance(rates, dict) or "input" not in rates or "output" not in rates:
            raise ValueError(f"費率設定不完整：{key}")
        if key.startswith("openai:") and "cached_input" not in rates:
            raise ValueError(f"缺少 cached_input 費率：{key}")
        for name, value in rates.items():
            if value is None and name == "cache_write":
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"費率必須為非負有限數字：{key}.{name}")
    aliases = pricing.get("aliases", {})
    if not isinstance(aliases, dict) or any(v not in pricing["models"] for v in aliases.values()):
        raise ValueError("費率別名必須對應 models 中的版本")
    return pricing


def price_call(call: dict, pricing: dict) -> dict:
    provider = call["provider"]
    model = call.get("resolved_model") or call["requested_model"]
    key = f"{provider}:{model}"
    # 未列出的 snapshot 不猜價；缺少 usage 也不當成免費。
    key = pricing.get("aliases", {}).get(key, key)
    rates = pricing["models"].get(key)
    usage = call.get("usage")
    unknown = {"cost_usd": None, "cost_status": "unknown"}
    if not rates or not usage:
        return unknown
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if type(inp) is not int or inp < 0:
        return unknown
    if provider == "typesafe":
        amount = Decimal(inp) * Decimal(str(rates["input"]))
    else:
        details = usage.get("input_tokens_details") or {}
        cached = details.get("cached_tokens")
        writes = details.get("cache_write_tokens")
        if writes is None and rates.get("cache_write") is None:
            writes = 0  # 舊模型不另收 cache-write 費。
        if (type(out) is not int or out < 0 or type(cached) is not int
                or type(writes) is not int or min(cached, writes) < 0
                or cached + writes > inp):
            return unknown
        if writes and rates.get("cache_write") is None:
            return unknown
        amount = (
            Decimal(inp - cached - writes) * Decimal(str(rates["input"]))
            + Decimal(cached) * Decimal(str(rates["cached_input"]))
            + Decimal(writes) * Decimal(str(rates.get("cache_write") or 0))
            # output_tokens 已包含 reasoning_tokens，不再重複加計。
            + Decimal(out) * Decimal(str(rates["output"]))
        )
    return {"cost_usd": float(amount / Decimal(1_000_000)), "cost_status": "estimated_from_usage"}


class CallRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    @contextmanager
    def call(self, provider: str, model: str, step: str, prompt: str):
        event = {
            "provider": provider, "requested_model": model, "step": step,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "usage": None, "outcome": "error",
        }
        self.calls.append(event)
        start = time.perf_counter()
        try:
            yield event
            event["outcome"] = "completed"
        except Exception as error:
            # 不保存原始錯誤訊息、HTTP body 或 header，避免洩漏金鑰。
            event["error_type"] = type(error).__name__
            event["http_status"] = getattr(error, "status_code", None)
            if event["http_status"] is None:
                event["http_status"] = getattr(error, "status", None)
            body = getattr(error, "body", None)
            detail = body.get("detail") if isinstance(body, dict) else None
            if isinstance(detail, dict) and detail.get("error_type") == "max_tokens_exceeded":
                event["safe_error_code"] = "max_tokens_exceeded"
            raise
        finally:
            event["latency_seconds"] = time.perf_counter() - start

    @staticmethod
    def response(event: dict, response) -> None:
        event["resolved_model"] = response.model
        event["response_id"] = getattr(response, "id", None)
        usage = getattr(response, "usage", None)
        event["usage"] = None if usage is None else usage.model_dump(mode="json")

    def summary(self, pricing: dict) -> dict:
        calls = [{**call, **price_call(call, pricing)} for call in self.calls]
        known = sum(c["cost_usd"] for c in calls if c["cost_usd"] is not None)
        unknown = sum(c["cost_usd"] is None for c in calls)
        return {
            "calls": calls, "known_cost_usd": known,
            "total_cost_usd": None if unknown else known,
            "unknown_cost_calls": unknown, "pricing_snapshot": pricing,
        }

    def save(self, path: Path, pricing: dict, **metadata) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**metadata, **self.summary(pricing)},
                                  ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
