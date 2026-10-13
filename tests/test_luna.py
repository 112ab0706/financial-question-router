import json
import unittest

import httpx
from openai import OpenAI

from experiment_luna import run_one
from legacy_experiments import summarize
from models import ExperimentConfig, ExperimentQuestion, FinancialReport, PipelineConfig
from pipeline import QuestionPipeline
from telemetry import load_pricing
from test_pipeline import ROOT, openai_response


class LunaTests(unittest.TestCase):
    def run_mock(self, payload):
        self.requests = []
        config = PipelineConfig.model_validate_json((ROOT / "config.json").read_text())
        judge = ExperimentConfig.model_validate_json((ROOT / "experiment_config_E.json").read_text()).openai_judge
        report = FinancialReport.model_validate_json((ROOT / "financial_report_fields.json").read_text(encoding="utf-8"))
        item = ExperimentQuestion(question_id="q1", question="What is operating revenue?", gold_answerable=False)
        def handler(request):
            body = json.loads(request.content)
            self.requests.append(body)
            return httpx.Response(200, json=openai_response(payload, model=body["model"]))
        with OpenAI(api_key="test", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            pipeline = QuestionPipeline(config, client, None)
            record = run_one(pipeline, item, report, judge, load_pricing(ROOT / "pricing.json"))
        return record, report

    def test_original_is_only_sent_once_to_luna_with_no_gold_or_preprocessing(self):
        record, report = self.run_mock({"answerable": False})
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request["model"], "gpt-6-luna")
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertEqual(request["text"]["format"]["name"], "AnswerabilityResult")
        payload = json.loads(request["input"][1]["content"].split("\n", 1)[1])
        self.assertEqual(payload, {"question": "What is operating revenue?", "financial_report": report.model_dump()})
        self.assertIs(record["groups"]["E"]["result"], False)
        self.assertIs(record["groups"]["E"]["correct"], True)
        self.assertAlmostEqual(record["total_cost_usd"], 0.00015)
        self.assertEqual(record["groups"]["E"]["shared_call_indices"], [])

    def test_summary_counts_one_group_and_scores_true_against_false_gold(self):
        record, _ = self.run_mock({"answerable": True})
        summary = summarize([record], "E")
        self.assertEqual(summary["group_evaluations"], 1)
        self.assertEqual(list(summary["groups"]), ["E"])
        self.assertEqual(summary["groups"]["E"]["confusion_matrix"]["fp"], 1)
        self.assertEqual(summary["groups"]["E"]["accuracy"], 0)
        self.assertEqual(summary["groups"]["E"]["decision_coverage"], 1)

    def test_invalid_answer_is_error_not_false_and_cost_stays_unknown(self):
        record, _ = self.run_mock({"answerable": "false"})
        entry = record["groups"]["E"]
        self.assertEqual(entry["outcome"], "error")
        self.assertIsNone(entry["result"])
        self.assertIsNone(entry["correct"])
        self.assertIsNone(record["total_cost_usd"])
        summary = summarize([record], "E")["groups"]["E"]
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["false_count"], 0)


if __name__ == "__main__":
    unittest.main()
