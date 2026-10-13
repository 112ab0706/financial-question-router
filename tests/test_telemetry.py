import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from openai.types.responses.response_usage import ResponseUsage

from telemetry import CallRecorder, load_pricing, price_call

ROOT = Path(__file__).resolve().parents[1]


class CostTests(unittest.TestCase):
    def test_provider_error_status_is_recorded_without_sensitive_body(self):
        class ProviderError(Exception):
            status = 400
            body = {"detail": {"error_type": "max_tokens_exceeded", "message": "private body"}}
        recorder = CallRecorder()
        with self.assertRaises(ProviderError):
            with recorder.call("typesafe", "jev-latest", "needs_full_report", "prompt"):
                raise ProviderError("private body")
        self.assertEqual(recorder.calls[0]["http_status"], 400)
        self.assertEqual(recorder.calls[0]["safe_error_code"], "max_tokens_exceeded")
        self.assertNotIn("private body", json.dumps(recorder.calls))

    def setUp(self):
        self.pricing = load_pricing(ROOT / "pricing.json")

    def test_cache_read_write_and_reasoning_are_not_double_counted(self):
        call = {
            "provider": "openai", "requested_model": "gpt-6-luna",
            "usage": {
                "input_tokens": 3000,
                "input_tokens_details": {"cached_tokens": 1000, "cache_write_tokens": 500},
                "output_tokens": 400, "output_tokens_details": {"reasoning_tokens": 300},
            },
        }
        # 普通輸入 1500，cache read 1000，cache write 500，總輸出 400。
        self.assertAlmostEqual(price_call(call, self.pricing)["cost_usd"], 0.0004225)

    def test_jev_output_is_free_even_without_output_usage(self):
        call = {"provider": "typesafe", "requested_model": "jev-latest",
                "resolved_model": "jev-1.13.0", "usage": {"input_tokens": 1000}}
        self.assertEqual(price_call(call, self.pricing)["cost_usd"], 0.000042)

    def test_unknown_usage_model_and_inconsistent_tokens_are_not_zero(self):
        base = {"provider": "openai", "requested_model": "gpt-6-luna"}
        for change in (
            {}, {"usage": {"input_tokens": 1000, "output_tokens": 100}},
            {"resolved_model": "unlisted-version", "usage": {"input_tokens": 1000}},
            {"usage": {"input_tokens": 100, "output_tokens": 10,
                       "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 30}}},
        ):
            with self.subTest(change=change):
                self.assertIsNone(price_call({**base, **change}, self.pricing)["cost_usd"])

    def test_success_and_error_usage_can_be_saved_without_error_body(self):
        recorder = CallRecorder()
        usage = ResponseUsage(
            input_tokens=1000, input_tokens_details={"cached_tokens": 0, "cache_write_tokens": 0},
            output_tokens=100, output_tokens_details={"reasoning_tokens": 90}, total_tokens=1100,
        )
        with recorder.call("openai", "gpt-5.4-mini", "rewrite", "prompt") as event:
            recorder.response(event, SimpleNamespace(model="gpt-5.4-mini-2026-03-17", id="resp_example", usage=usage))
        with self.assertRaises(RuntimeError):
            with recorder.call("typesafe", "jev-latest", "answerable", "prompt"):
                raise RuntimeError("secret-value")
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(ROOT.resolve()))
            path = Path(directory) / "record.json"
            recorder.save(path, self.pricing, result=None)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("secret-value", raw)
            data = json.loads(raw)
            self.assertEqual(data["unknown_cost_calls"], 1)
            self.assertIsNone(data["total_cost_usd"])
            self.assertAlmostEqual(data["known_cost_usd"], 0.0012)
            self.assertGreaterEqual(data["calls"][0]["latency_seconds"], 0)

    def test_invalid_pricing_is_rejected_before_calls(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(ROOT.resolve()))
            path = Path(directory) / "prices.json"
            path.write_text('{"models": {"openai:bad": {"input": -1, "output": 0}}}')
            with self.assertRaises(ValueError):
                load_pricing(path)


if __name__ == "__main__":
    unittest.main()
