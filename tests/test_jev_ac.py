import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx2
from typesafe_sdk import RetryPolicy, TypeSafeClient

import experiment_jev_ac
import prompts
from database_catalog import load_catalog
from test_pipeline import ROOT


class SavedJevExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.source = self.path / "source"
        self.source.mkdir()
        self.catalog = load_catalog(ROOT / "database/2025q4_accounting_items.md")
        self.original = "What was operating revenue in 2014?"
        self.selected = "2014年的營業收入是多少？"
        cost = {"known_cost_usd": 0, "total_cost_usd": 0, "unknown_cost_calls": 0}
        self.record = {
            "question_id": "q001", "original_question": self.original,
            "bool_semantics": "needs_full_report", "catalog_sha256": self.catalog.source_sha256,
            "gold_needs_full_report": None, "calls": [],
            "shared_preprocessing": {"question_used": self.selected, "outcome": "rewritten", "reason": None},
            "groups": {g: {"question_used": self.original if g == "C" else self.selected,
                "result": True, "outcome": "completed", "correct": None,
                "judge_cost": cost, "standalone_cost_estimate": cost, "api_latency_seconds": 0}
                for g in "ABC"},
        }
        self.source_file = self.source / "q0001_r1.json"
        self.source_file.write_text(json.dumps(self.record, ensure_ascii=False), encoding="utf-8")
        self.source_hash = hashlib.sha256(self.source_file.read_bytes()).hexdigest()
        self.gold = self.path / "gold.json"
        self.gold.write_text(json.dumps({
            "bool_semantics": "needs_full_report", "source_record_sha256": {self.source_file.name: self.source_hash},
            "labels": [{"question_id": "q001", "gold_needs_full_report": True}],
        }), encoding="utf-8")

    def test_only_two_jev_calls_reuse_exact_inputs_and_keep_labels_local(self):
        requests = []
        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            return httpx2.Response(200, json={
                "model": "jev-1.13.0",
                "answers": {name: {"type": "noul", "noul": 0.1} for name in prompts.JEV_ROUTING_CHECKS},
                "usage": {"input_tokens": 100, "output_tokens": 30},
            })
        with TypeSafeClient(api_key="offline-test", retry=RetryPolicy(max_retries=0),
                            transport=httpx2.MockTransport(handler)) as client, \
                patch("experiment_jev_ac.TypeSafeClient", return_value=client), \
                patch("openai.OpenAI", side_effect=AssertionError("OpenAI must never be called")), \
                patch.dict("os.environ", {"TYPESAFE_API_KEY": "offline-test", "OPENAI_API_KEY": ""}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = experiment_jev_ac.main(["--source-run", str(self.source), "--gold", str(self.gold),
                                           "--output", str(self.path / "new")])
        self.assertEqual(code, 0)
        self.assertEqual(len(requests), 2)
        self.assertEqual({r["state"]["original_question"] for r in requests}, {self.original, self.selected})
        for request in requests:
            self.assertEqual(set(request["questions"]), set(prompts.JEV_ROUTING_CHECKS))
            self.assertNotIn("gold_needs_full_report", json.dumps(request))
            self.assertNotIn("gold_answerable", json.dumps(request))
        result = experiment_jev_ac.read_json(self.path / "new/q0001_r1.json")
        self.assertEqual(result["groups"]["A"]["question_used"], self.selected)
        self.assertEqual(result["groups"]["C"]["question_used"], self.original)
        self.assertTrue(result["shared_preprocessing"]["reused"])
        self.assertEqual(result["shared_preprocessing"]["new_cost_usd"], 0)
        self.assertTrue(all(c["provider"] == "typesafe" for c in result["calls"]))
        self.assertAlmostEqual(result["total_cost_usd"], 0.0000084)
        for group in "AC":
            self.assertIs(result["groups"][group]["result"], False)
            self.assertIs(result["groups"][group]["correct"], False)
        self.assertEqual(hashlib.sha256(self.source_file.read_bytes()).hexdigest(), self.source_hash)

    def test_changed_source_or_mismatched_gold_fails_before_api(self):
        self.source_file.write_text(self.source_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with patch("experiment_jev_ac.TypeSafeClient") as client, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = experiment_jev_ac.main(["--source-run", str(self.source), "--gold", str(self.gold),
                                           "--output", str(self.path / "new")])
        self.assertEqual(code, 1)
        client.assert_not_called()
        self.assertFalse((self.path / "new").exists())

    def test_legacy_answerability_labels_are_not_flipped(self):
        self.gold.write_text(json.dumps({"bool_semantics": "answerable", "labels": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            experiment_jev_ac.load_saved(self.source, self.catalog, self.gold)


if __name__ == "__main__":
    unittest.main()
