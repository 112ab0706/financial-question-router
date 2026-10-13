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
from typesafe_sdk import RetryPolicy, TypeSafeClient

import legacy_experiments as experiments
import prompts
from models import ExperimentConfig, ExperimentQuestion, FinancialReport, PipelineConfig
from pipeline import QuestionPipeline
from telemetry import load_pricing
from test_pipeline import REWRITE, ROOT, STATUS, TEMPLATE, openai_response


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.config = PipelineConfig.model_validate_json((ROOT / "config.json").read_text())
        self.experiment = ExperimentConfig.model_validate_json((ROOT / "experiment_config.json").read_text())
        self.report = FinancialReport.model_validate_json((ROOT / "financial_report_fields.json").read_text(encoding="utf-8"))
        self.pricing = load_pricing(ROOT / "pricing.json")
        self.item = ExperimentQuestion(question_id="q1", question="原題：甲公司2025年營業利益率是多少？", gold_answerable=True)
        self.openai_requests = []
        self.jev_requests = []

    def pipeline(self, *, no_rewrite=False, cannot_rewrite=False, bad_template=False,
                 jev_error_question=None, judge_payload=None, oa_error_schema=None):
        def oa_handler(request):
            body = json.loads(request.content)
            self.openai_requests.append(body)
            name = body["text"]["format"]["name"]
            if name == oa_error_schema:
                return httpx.Response(503, json={"error": "private secret"})
            payloads = {
                "QuestionTemplate": {} if bad_template else TEMPLATE,
                "StatusResult": {**STATUS, "status": "no_rewrite" if no_rewrite else "rewrite"},
                "RewriteResult": {**REWRITE, "rewritten_question": None if cannot_rewrite else REWRITE["rewritten_question"]},
                "AnswerabilityResult": {"answerable": True} if judge_payload is None else judge_payload,
            }
            return httpx.Response(200, json=openai_response(payloads[name], model=body["model"]))

        def jev_handler(request):
            body = json.loads(request.content)
            self.jev_requests.append(body)
            if body["state"]["question"] == jev_error_question:
                return httpx2.Response(503, json={"error": "private secret test"})
            return httpx2.Response(200, json={"model": body["model"],
                "answers": {"answerable": {"type": "noul", "noul": 0.9}},
                "usage": {"input_tokens": 100, "output_tokens": 10}})

        oa = self.stack.enter_context(OpenAI(api_key="test", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(oa_handler))))
        jev = self.stack.enter_context(TypeSafeClient(api_key="test", retry=RetryPolicy(max_retries=0),
            transport=httpx2.MockTransport(jev_handler)))
        return QuestionPipeline(self.config, oa, jev)

    def run_record(self, **kwargs):
        return experiments.run_paired(self.pipeline(**kwargs), self.item, self.report, self.experiment, self.pricing)

    def test_shared_rewrite_and_original_questions_use_real_sdk(self):
        record = self.run_record()
        self.assertEqual(len(record["calls"]), 7)
        self.assertEqual(len(self.openai_requests), 5)
        self.assertEqual(len(self.jev_requests), 2)
        a, b, c, d = (record["groups"][g] for g in "ABCD")
        self.assertEqual(a["question_used"], REWRITE["rewritten_question"])
        self.assertEqual(a["question_sha256"], b["question_sha256"])
        self.assertEqual(c["question_used"], self.item.question)
        self.assertEqual(c["question_sha256"], d["question_sha256"])
        for group in (a, b, c, d):
            self.assertIs(group["result"], True)
        for request, question in zip(self.jev_requests, (a["question_used"], c["question_used"])):
            self.assertEqual(request["state"], {"question": question, "financial_report": self.report.model_dump()})
            self.assertEqual(request["model"], self.config.step3.model)
        for request, question in zip(self.openai_requests[3:], (b["question_used"], d["question_used"])):
            payload = json.loads(request["input"][1]["content"].split("\n", 1)[1])
            self.assertEqual(payload, {"question": question, "financial_report": self.report.model_dump()})
            self.assertEqual(request["model"], self.experiment.openai_judge.model)
            self.assertEqual(request["reasoning"], {"effort": "medium"})
            self.assertEqual(request["input"][0]["content"], prompts.JEV_INSTRUCTIONS)
        self.assertEqual(a["shared_call_indices"], b["shared_call_indices"])
        self.assertEqual(c["shared_call_indices"], [])
        self.assertEqual(d["shared_call_indices"], [])

    def test_common_cost_counted_once_and_deployment_cost_allocated(self):
        record = self.run_record()
        prefix_cost = sum(record["calls"][i]["cost_usd"] for i in record["shared_preprocessing"]["call_indices"])
        judges = sum(record["groups"][g]["judge_cost"]["total_cost_usd"] for g in "ABCD")
        deployed = sum(record["groups"][g]["standalone_cost_estimate"]["total_cost_usd"] for g in "ABCD")
        self.assertAlmostEqual(record["total_cost_usd"], prefix_cost + judges)
        self.assertAlmostEqual(deployed, record["total_cost_usd"] + prefix_cost)
        summary = experiments.summarize([record, record])
        self.assertEqual(summary["group_evaluations"], 8)
        self.assertAlmostEqual(summary["actual_experiment_cost"]["total_cost_usd"], record["total_cost_usd"] * 2)





    def test_old_stopped_false_records_are_excluded_from_routing_metrics(self):
        record = self.run_record(no_rewrite=True)
        record["groups"]["A"].update(result=False, outcome="stopped", stop_reason="rejected")
        self.assertEqual(experiments.result_label(record["groups"]["A"]), "rejected")
        metrics = experiments.summarize([record])["groups"]["A"]
        self.assertEqual(metrics["false_count"], 0)
        self.assertEqual(metrics["rejected"], 1)
        self.assertIsNone(metrics["accuracy"])

    def test_no_rewrite_uses_same_original_for_all_four_judges(self):
        record = self.run_record(no_rewrite=True)
        self.assertEqual(len(record["calls"]), 6)
        self.assertEqual(len(self.jev_requests), 2)
        self.assertEqual(len(self.openai_requests), 4)
        self.assertNotIn("RewriteResult", [r["text"]["format"]["name"] for r in self.openai_requests])
        self.assertEqual(record["shared_preprocessing"]["reason"], "no_rewrite")
        for group in "ABCD":
            entry=record["groups"][group]
            self.assertIs(entry["result"], True)
            self.assertEqual(entry["outcome"], "completed")
            self.assertEqual(entry["question_used"], self.item.question)
            self.assertEqual(len(entry["judge_call_indices"]), 1)

    def test_failed_rewrite_still_sends_original_to_ab_and_cd(self):
        record = self.run_record(cannot_rewrite=True)
        self.assertEqual(len(record["calls"]), 7)
        self.assertEqual(record["shared_preprocessing"]["reason"], "cannot_rewrite")
        for group in "ABCD":
            self.assertIs(record["groups"][group]["result"], True)
            self.assertEqual(record["groups"][group]["question_used"], self.item.question)
        metrics=experiments.summarize([record])["groups"]["A"]
        self.assertEqual(metrics["decision_coverage"], 1)
        self.assertEqual(metrics["stopped"], 0)
        self.assertEqual(metrics["rejected"], 0)
        self.assertEqual(metrics["cannot_rewrite"], 0)
        self.assertEqual(metrics["original_inputs"], 1)
        self.assertEqual(metrics["accuracy"], 1)

    def test_invalid_template_still_attempts_every_final_judge(self):
        record = self.run_record(bad_template=True)
        self.assertEqual(record["shared_preprocessing"]["reason"], "preprocessing_error")
        self.assertEqual(len(record["calls"]), 5)
        self.assertEqual(len(self.jev_requests), 2)
        for group in "ABCD":
            self.assertIs(record["groups"][group]["result"], True)
            self.assertEqual(record["groups"][group]["question_used"], self.item.question)
        metrics=experiments.summarize([record])
        self.assertEqual(metrics["preprocessing_errors"], 1)
        self.assertEqual(metrics["groups"]["A"]["errors"], 0)
        self.assertIsNone(record["total_cost_usd"])

    def test_http_error_at_any_preprocessing_stage_falls_back_and_judges(self):
        for schema in ("QuestionTemplate", "StatusResult", "RewriteResult"):
            with self.subTest(schema=schema):
                self.openai_requests.clear()
                self.jev_requests.clear()
                record = self.run_record(oa_error_schema=schema)
                for group in "ABCD":
                    entry=record["groups"][group]
                    self.assertIs(entry["result"], True)
                    self.assertEqual(entry["question_used"], self.item.question)
                    self.assertEqual(len(entry["judge_call_indices"]), 1)
                self.assertEqual(record["shared_preprocessing"]["reason"], "preprocessing_error")
                self.assertNotIn("private secret", json.dumps(record))

    def test_actual_false_is_scored_and_gold_is_not_sent_to_models(self):
        self.item.gold_answerable = False
        record = self.run_record(judge_payload={"answerable": False})
        entry = record["groups"]["B"]
        self.assertEqual(experiments.result_label(entry), "False")
        self.assertIs(entry["correct"], True)
        metrics = experiments.summarize([record])["groups"]["B"]
        self.assertEqual(metrics["false_count"], 1)
        self.assertEqual(metrics["confusion_matrix"]["tn"], 1)
        for request in self.openai_requests:
            self.assertNotIn("gold_answerable", request["input"][1]["content"])
        for request in self.jev_requests:
            self.assertNotIn("gold_answerable", request["state"])


    def test_judge_failure_is_isolated_and_not_false(self):
        record = self.run_record(jev_error_question=REWRITE["rewritten_question"])
        self.assertIsNone(record["groups"]["A"]["result"])
        for group in "BCD":
            self.assertIs(record["groups"][group]["result"], True)
        self.assertIsNone(record["total_cost_usd"])
        self.assertNotIn("private secret", json.dumps(record))
        metrics = experiments.summarize([record])["groups"]["A"]
        self.assertIsNone(metrics["accuracy"])
        self.assertEqual(metrics["accuracy_including_errors"], 0)
        self.assertEqual(metrics["errors"], 1)
        self.assertEqual(metrics["confusion_matrix"]["fn"], 0)

    def test_openai_non_boolean_is_error_in_b_and_d(self):
        record = self.run_record(judge_payload={"answerable": "true"})
        for group in "BD":
            self.assertIsNone(record["groups"][group]["result"])
        self.assertIs(record["groups"]["C"]["result"], True)

    def test_arbitrary_order_preserves_pairing(self):
        record = experiments.run_paired(self.pipeline(), self.item, self.report,
            self.experiment, self.pricing, judge_order="DCBA")
        self.assertEqual(record["judge_order"], list("DCBA"))
        self.assertEqual(self.jev_requests[0]["state"]["question"], self.item.question)
        self.assertEqual(record["groups"]["A"]["question_used"], record["groups"]["B"]["question_used"])

    def test_batch_cli_saves_each_round_and_configuration(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(ROOT.resolve()))
            dataset = Path(directory) / "dataset.json"
            output = Path(directory) / "results"
            dataset.write_text(json.dumps([self.item.model_dump()]), encoding="utf-8")
            record = self.run_record()
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("legacy_experiments.run_paired", return_value=record) as run, \
                 patch.dict("os.environ", {"OPENAI_API_KEY": "test", "TYPESAFE_API_KEY": "test"}), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = experiments.main(["--dataset", str(dataset), "--repeat", "2", "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 2)
            self.assertEqual([c.kwargs["repeat"] for c in run.call_args_list], [1, 2])
            self.assertTrue((output / "q0001_r2.json").exists())
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["group_evaluations"], 8)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["financial_report"], self.report.model_dump())
            self.assertEqual(manifest["experiment_config"], self.experiment.model_dump())
            self.assertNotIn("API_KEY", json.dumps(manifest))


if __name__ == "__main__":
    unittest.main()
