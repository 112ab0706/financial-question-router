import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
import httpx2
from openai import OpenAI
from pydantic import ValidationError
from typesafe_sdk import RetryPolicy, TypeSafeClient

import experiments
import prompts
from database_catalog import load_catalog
from database_routing import JevDatabaseQuestionRouter
from models import ExperimentConfig, PipelineConfig, RoutingExperimentQuestion
from pipeline import QuestionPipeline
from telemetry import load_pricing
from test_pipeline import ROOT, REWRITE, STATUS, TEMPLATE, openai_response


class DatabaseExperimentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.config = PipelineConfig.model_validate_json((ROOT / "config.json").read_text())
        self.experiment = ExperimentConfig.model_validate_json((ROOT / "experiment_config.json").read_text())
        self.catalog = load_catalog(ROOT / "database/2025q4_accounting_items.md")
        self.pricing = load_pricing(ROOT / "pricing.json")
        self.item = RoutingExperimentQuestion(question_id="q1", question="原題：甲公司2025年營業利益率是多少？",
                                              gold_needs_full_report=True)
        self.required_item = next(i.item_id for i in self.catalog.items if i.element == "Revenue")
        self.openai_requests = []
        self.jev_requests = []

    def pipeline(self, *, no_rewrite=False, bad_template=False, cannot_rewrite=False,
                 probability=0.9, jev_error_question=None, judge_payload=None, missing_jev_answer=None):
        def oa_handler(request):
            body = json.loads(request.content)
            self.openai_requests.append(body)
            name = body["text"]["format"]["name"]
            payloads = {
                "QuestionTemplate": {} if bad_template else TEMPLATE,
                "StatusResult": {**STATUS, "status": "no_rewrite" if no_rewrite else "rewrite"},
                "RewriteResult": {**REWRITE, "rewritten_question": None if cannot_rewrite else REWRITE["rewritten_question"]},
                "DatabaseRoutingResult": {
                    "needs_full_report": True, "required_item_ids": [], "reason": "缺少必要資訊。",
                } if judge_payload is None else judge_payload,
            }
            return httpx.Response(200, json=openai_response(payloads[name], model=body["model"]))

        def jev_handler(request):
            body = json.loads(request.content)
            self.jev_requests.append(body)
            if body["state"]["original_question"] == jev_error_question:
                return httpx2.Response(503, json={"error": "private secret"})
            values = probability if isinstance(probability, dict) else {name: probability for name in prompts.JEV_ROUTING_CHECKS}
            return httpx2.Response(200, json={
                "model": body["model"], "answers": {name: {"type": "noul", "noul": value}
                    for name, value in values.items() if name != missing_jev_answer},
                "usage": {"input_tokens": 100, "output_tokens": 10},
            })
        oa = self.stack.enter_context(OpenAI(api_key="offline-test", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(oa_handler))))
        jev = self.stack.enter_context(TypeSafeClient(api_key="offline-test", retry=RetryPolicy(max_retries=0),
            transport=httpx2.MockTransport(jev_handler)))
        return QuestionPipeline(self.config, oa, jev)

    def run_record(self, **kwargs):
        return experiments.run_paired(self.pipeline(**kwargs), self.item, self.catalog, self.experiment, self.pricing)

    def test_abc_share_catalog_rules_and_ab_share_exact_question(self):
        record = self.run_record()
        self.assertEqual(set(record["groups"]), set("ABC"))
        self.assertEqual(len(record["calls"]), 6)
        a, b, c = (record["groups"][g] for g in "ABC")
        self.assertEqual(a["question_used"], REWRITE["rewritten_question"])
        self.assertEqual(a["question_used"], b["question_used"])
        self.assertEqual(a["question_sha256"], b["question_sha256"])
        self.assertEqual(a["shared_call_indices"], b["shared_call_indices"])
        self.assertEqual(c["question_used"], self.item.question)
        self.assertEqual(c["shared_call_indices"], [])
        for request, question in zip(self.jev_requests, (a["question_used"], c["question_used"])):
            self.assertEqual(request["state"], JevDatabaseQuestionRouter.build_state(question, self.catalog))
            self.assertEqual(request["model"], self.config.step3.model)
            self.assertEqual(set(request["questions"]), set(prompts.JEV_ROUTING_CHECKS))
            for name, check in prompts.JEV_ROUTING_CHECKS.items():
                noul = request["questions"][name]
                self.assertEqual(noul["instructions"], check["instructions"])
                self.assertEqual(noul["criteria"], {"true": check["true"], "false": check["false"]})
            self.assertEqual(request["state"]["database_catalog"]["statements"], self.catalog.to_jev_state()["statements"])
            self.assertNotIn("availability", request["state"]["database_catalog"])
            self.assertNotIn("source_scope", request["state"]["database_catalog"])
        request = self.openai_requests[-1]
        payload = json.loads(request["input"][1]["content"].split("\n", 1)[1])
        self.assertEqual(payload, {"original_question": b["question_used"], "database_catalog": self.catalog.to_judgment_state()})
        self.assertEqual(request["model"], "gpt-5.4-mini")
        self.assertIn(prompts.DATABASE_ROUTING_RULES, request["input"][0]["content"])
        self.assertEqual(record["routing_judgment_version"]["A"], prompts.JEV_ROUTING_VERSION)
        for entry in record["groups"].values():
            self.assertIs(entry["result"], True)
            self.assertEqual(entry["bool_semantics"], "needs_full_report")

    def test_false_is_database_sufficient_and_new_gold_does_not_leak(self):
        self.item.gold_needs_full_report = False
        record = self.run_record(probability=0.2, judge_payload={
            "needs_full_report": False, "required_item_ids": [self.required_item], "reason": "科目足夠。",
        })
        metrics = experiments.summarize([record])
        self.assertEqual(metrics["positive_class"], "需要完整財報")
        self.assertEqual(metrics["group_evaluations"], 3)
        for group in "ABC":
            self.assertIs(record["groups"][group]["result"], False)
            self.assertIs(record["groups"][group]["correct"], True)
            self.assertEqual(metrics["groups"][group]["confusion_matrix"]["tn"], 1)
            self.assertEqual(metrics["groups"][group]["accuracy"], 1)
            self.assertNotIn("rejected", metrics["groups"][group])
        for request in self.openai_requests:
            self.assertNotIn("gold_needs_full_report", request["input"][1]["content"])
            self.assertNotIn("gold_answerable", request["input"][1]["content"])
        for request in self.jev_requests:
            self.assertNotIn("gold_needs_full_report", request["state"])

    def test_jev_tie_routes_to_full_report(self):
        record = self.run_record(probability=0.5)
        for group in "AC":
            self.assertIs(record["groups"][group]["result"], True)

    def test_each_atomic_failure_routes_and_all_below_threshold_stays_in_database(self):
        names = list(prompts.JEV_ROUTING_CHECKS)
        for active in names + [None]:
            with self.subTest(active=active):
                values = {name: 0.9 if name == active else 0.49 for name in names}
                record = self.run_record(probability=values)
                for group in "AC":
                    self.assertIs(record["groups"][group]["result"], active is not None)
                    call = record["calls"][record["groups"][group]["judge_call_indices"][0]]
                    self.assertEqual(call["probabilities"], values)
                    self.assertEqual(call["combine"], "any")
                    self.assertNotIn("probability", call)

    def test_missing_check_answer_remains_error_with_usage_not_false(self):
        record = self.run_record(probability=0.1, missing_jev_answer="missing_detail")
        for group in "AC":
            self.assertIsNone(record["groups"][group]["result"])
            self.assertEqual(record["groups"][group]["outcome"], "error")
            call = record["calls"][record["groups"][group]["judge_call_indices"][0]]
            self.assertIsNotNone(call["usage"])
        self.assertIs(record["groups"]["B"]["result"], True)

    def test_shared_cost_only_counted_once(self):
        record = self.run_record()
        shared = sum(record["calls"][i]["cost_usd"] for i in record["shared_preprocessing"]["call_indices"])
        judge_costs = sum(r["judge_cost"]["total_cost_usd"] for r in record["groups"].values())
        self.assertAlmostEqual(record["total_cost_usd"], shared + judge_costs)
        for group in "AB":
            row = record["groups"][group]
            self.assertAlmostEqual(row["standalone_cost_estimate"]["total_cost_usd"], shared + row["judge_cost"]["total_cost_usd"])
        c = record["groups"]["C"]
        self.assertEqual(c["standalone_cost_estimate"], c["judge_cost"])

    def test_all_three_judge_original_after_no_rewrite_or_preprocessing_failure(self):
        for settings, calls in (({"no_rewrite": True}, 5), ({"cannot_rewrite": True}, 6), ({"bad_template": True}, 4)):
            with self.subTest(settings=settings):
                self.openai_requests.clear()
                self.jev_requests.clear()
                record = self.run_record(**settings)
                self.assertEqual(len(record["calls"]), calls)
                for row in record["groups"].values():
                    self.assertEqual(row["question_used"], self.item.question)
                    self.assertEqual(row["outcome"], "completed")

    def test_one_judge_failure_does_not_become_false_or_block_other_groups(self):
        record = self.run_record(jev_error_question=REWRITE["rewritten_question"])
        self.assertIsNone(record["groups"]["A"]["result"])
        self.assertEqual(record["groups"]["A"]["outcome"], "error")
        for group in "BC":
            self.assertIs(record["groups"][group]["result"], True)
        self.assertNotIn("private secret", json.dumps(record))
        metrics = experiments.summarize([record])["groups"]["A"]
        self.assertEqual(metrics["false_count"], 0)
        self.assertEqual(metrics["errors"], 1)

    def test_arbitrary_order_preserves_pairing(self):
        record = experiments.run_paired(self.pipeline(), self.item, self.catalog,
            self.experiment, self.pricing, judge_order="CBA")
        self.assertEqual(record["judge_order"], list("CBA"))
        self.assertEqual(self.jev_requests[0]["state"]["original_question"], self.item.question)
        self.assertEqual(record["groups"]["A"]["question_used"], record["groups"]["B"]["question_used"])

    def test_old_gold_and_legacy_results_are_rejected(self):
        with self.assertRaises(ValidationError):
            RoutingExperimentQuestion(question_id="q1", question="原題", gold_answerable=True)
        with self.assertRaises(ValueError):
            experiments.summarize([{"gold_answerable": True}])

    def test_cli_only_writes_abc_new_semantics_and_gold(self):
        record = self.run_record()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory)
            dataset = path / "dataset.json"
            output = path / "results"
            dataset.write_text(json.dumps([self.item.model_dump()]), encoding="utf-8")
            with patch("experiments.run_paired", return_value=record) as run, \
                    patch.dict("os.environ", {"OPENAI_API_KEY": "offline-test", "TYPESAFE_API_KEY": "offline-test"}), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = experiments.main(["--dataset", str(dataset), "--repeat", "2", "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 2)
            self.assertTrue((output / "q0001_r2.json").exists())
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["groups"], list("ABC"))
            self.assertEqual(manifest["bool_semantics"], "needs_full_report")
            self.assertEqual(manifest["database_catalog"], self.catalog.to_state())
            self.assertNotIn("gold_answerable", manifest["dataset"][0])
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["group_evaluations"], 6)


if __name__ == "__main__":
    unittest.main()
