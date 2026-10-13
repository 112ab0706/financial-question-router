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
from typesafe_sdk import RetryPolicy, TypeSafeAPIError, TypeSafeClient

import main
import prompts
from models import FinancialReport, PipelineConfig, QuestionTemplate, StatusResult
from pipeline import PipelineError, QuestionPipeline
from telemetry import load_pricing


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = {"Time": "2025年", "Concept": "營業利益率", "Filter": "甲公司", "Logic": None}
REWRITE = {
    "rewritten_question": "甲公司2025年度的營業利益率是多少？",
    "Context_Gap": "會計概念可直接對應，保留原公司及2025年。",
}
STATUS = {"status": "rewrite", "Context_Gap": "會計概念可直接對應。"}


def openai_response(payload, status="completed", refusal=False, model="test-model"):
    content = (
        {"type": "refusal", "refusal": "Unable to process"} if refusal else
        {"type": "output_text", "text": json.dumps(payload), "annotations": []}
    )
    return {
        "id": "resp_test", "object": "response", "created_at": 0,
        "model": model, "status": status,
        "usage": {
            "input_tokens": 1000, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": 100, "output_tokens_details": {"reasoning_tokens": 50}, "total_tokens": 1100,
        },
        "error": None, "incomplete_details": None,
        "output": [{"id": "msg_test", "type": "message", "role": "assistant",
                    "status": "completed", "content": [content]}],
        "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
    }


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.config = PipelineConfig.model_validate_json((ROOT / "config.json").read_text())
        self.report = FinancialReport.model_validate_json(
            (ROOT / "financial_report_fields.json").read_text(encoding="utf-8"),
        )
        self.requests = []
        self.jev_requests = []

    def pipeline(self, *, rewrite=None, probability=0.9, refusal=False, incomplete=False,
                 jev_status=200, malformed_template=False, missing_answer=False,
                 classification=None):
        rewrite = REWRITE if rewrite is None else rewrite
        classification = STATUS if classification is None else classification

        def openai_handler(request):
            self.assertEqual(request.url.path, "/v1/responses")
            body = json.loads(request.content)
            self.requests.append(body)
            payload = [TEMPLATE, classification, rewrite][len(self.requests) - 1]
            if malformed_template and len(self.requests) == 1:
                payload = {"Time": "2025年"}
            return httpx.Response(200, json=openai_response(
                payload, refusal=refusal,
                status="incomplete" if incomplete else "completed",
                model=body["model"],
            ))

        def jev_handler(request):
            self.assertEqual(request.url.path, "/v1/systemone")
            body = json.loads(request.content)
            self.jev_requests.append(body)
            if jev_status != 200:
                return httpx2.Response(jev_status, json={"error": "test error"})
            return httpx2.Response(200, json={
                "model": body["model"],
                "answers": {} if missing_answer else {
                    "answerable": {"type": "noul", "noul": probability},
                },
                "usage": {"input_tokens": 100, "output_tokens": 10},
            })

        openai_client = self.stack.enter_context(OpenAI(
            api_key="test-openai", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(openai_handler)),
        ))
        jev_client = self.stack.enter_context(TypeSafeClient(
            api_key="test-typesafe", retry=RetryPolicy(max_retries=0),
            transport=httpx2.MockTransport(jev_handler),
        ))
        return QuestionPipeline(self.config, openai_client, jev_client)

    def test_full_flow_sdk_parsing_and_data_handoff(self):
        question = "甲公司2025年營業利益率是多少？"
        result = self.pipeline().run(question, self.report)
        self.assertIs(result, True)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(self.jev_requests), 1)
        for call, model, system_prompt in zip(
            self.requests,
            [self.config.step1.model, self.config.status_check.model, self.config.step2.model],
            [prompts.STEP1_SYSTEM_PROMPT, prompts.STATUS_SYSTEM_PROMPT, prompts.STEP2_SYSTEM_PROMPT],
        ):
            self.assertEqual(call["model"], model)
            self.assertEqual(call["input"][0], {"role": "system", "content": system_prompt})
            self.assertFalse(call["store"])
            schema = call["text"]["format"]
            self.assertEqual(schema["type"], "json_schema")
            self.assertTrue(schema["strict"])
        body = json.loads(self.requests[1]["input"][1]["content"].split("\n", 1)[1])
        self.assertEqual(body, {
            "user_question": question, "OriginalTemplate": TEMPLATE,
        })
        self.assertEqual(set(self.requests[1]["text"]["format"]["schema"]["properties"]), {
            "status", "Context_Gap",
        })
        rewrite_body = json.loads(self.requests[2]["input"][1]["content"].split("\n", 1)[1])
        self.assertEqual(rewrite_body, {
            "user_question": question, "OriginalTemplate": TEMPLATE,
            "Status": "rewrite", "Status_Context_Gap": STATUS["Context_Gap"],
        })
        self.assertEqual(set(self.requests[2]["text"]["format"]["schema"]["properties"]), {
            "rewritten_question", "Context_Gap",
        })
        self.assertNotIn("reasoning", self.requests[0])
        self.assertEqual(self.requests[1]["reasoning"], {"effort": "low"})
        self.assertEqual(self.requests[2]["reasoning"], {"effort": "medium"})
        jev = self.jev_requests[0]
        self.assertEqual(jev["model"], self.config.step3.model)
        self.assertEqual(jev["state"]["question"], REWRITE["rewritten_question"])
        self.assertNotIn("Context_Gap", jev["state"])
        self.assertNotIn("status", jev["state"])
        self.assertEqual(jev["state"]["financial_report"], self.report.model_dump())
        self.assertEqual(jev["questions"]["answerable"]["type"], "noul")

    def test_sdk_usage_is_collected_for_every_executed_step(self):
        pipeline = self.pipeline()
        self.assertIs(pipeline.run("問題", self.report), True)
        summary = pipeline.recorder.summary(load_pricing(ROOT / "pricing.json"))
        self.assertEqual(len(summary["calls"]), 4)
        self.assertEqual(summary["unknown_cost_calls"], 0)
        self.assertAlmostEqual(summary["total_cost_usd"], 0.0019142)
        self.assertEqual(summary["calls"][0]["usage"]["input_tokens"], 1000)
        self.assertEqual(summary["calls"][3]["usage"]["input_tokens"], 100)




    def test_4o_mini_without_reasoning_setting(self):
        pipeline = self.pipeline()
        pipeline.config.status_check.model = "gpt-4o-mini"
        pipeline.config.status_check.reasoning_effort = None
        self.assertIs(pipeline.run("問題", self.report), True)
        self.assertEqual(self.requests[1]["model"], "gpt-4o-mini")
        self.assertNotIn("reasoning", self.requests[1])

    def test_below_threshold_and_tie_are_false(self):
        for probability in (0.0, 0.2, 0.5):
            with self.subTest(probability=probability):
                self.requests.clear()
                self.jev_requests.clear()
                self.assertIs(self.pipeline(probability=probability).run("問題", self.report), False)

    def test_threshold_is_configurable(self):
        pipeline = self.pipeline(probability=0.8)
        pipeline.config.step3.yes_threshold = 0.9
        self.assertIs(pipeline.run("問題", self.report), False)

    def test_empty_question_makes_no_calls(self):
        with self.assertRaises(ValueError):
            self.pipeline().run("  ", self.report)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.jev_requests, [])




    def test_jev_http_error_propagates(self):
        with self.assertRaises(TypeSafeAPIError):
            self.pipeline(jev_status=503).run("問題", self.report)

    def test_missing_noul_is_error(self):
        with self.assertRaises(PipelineError):
            self.pipeline(missing_answer=True).run("問題", self.report)

    def test_invalid_probability_is_error(self):
        # SDK 版本可能在解碼階段或流程內拒絕；兩者都不能回傳 True/False。
        for probability in (-0.1, 1.1):
            with self.subTest(probability=probability):
                self.requests.clear()
                with self.assertRaises((PipelineError, TypeSafeAPIError, ValidationError)):
                    self.pipeline(probability=probability).run("問題", self.report)





    def test_no_company_prefix_or_added_year_is_required(self):
        question = "哪一年度的營業收入最高？"
        self.assertIs(self.pipeline(rewrite={
            **REWRITE, "rewritten_question": question,
        }).run("哪一年營業收入最高？", self.report), True)
        self.assertEqual(self.jev_requests[0]["state"]["question"], question)






    def test_no_rewrite_uses_original_and_still_judges(self):
        question = "保留原題 2025 年？"
        pipeline = self.pipeline(classification={"status": "no_rewrite", "Context_Gap": "保留原文。"})
        self.assertIs(pipeline.run(question, self.report), True)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.jev_requests[0]["state"]["question"], question)
        self.assertEqual(len(self.jev_requests), 1)

    def test_direct_rewrite_requires_rewrite_status(self):
        pipeline = self.pipeline()
        with self.assertRaises(ValueError):
            pipeline.rewrite_question("原題", QuestionTemplate(**TEMPLATE), StatusResult(
                status="no_rewrite", Context_Gap="直接使用原題。"))
        self.assertEqual(self.requests, [])

    def test_no_rewrite_can_produce_false_only_from_final_judge(self):
        pipeline = self.pipeline(probability=0.2, classification={
            "status": "no_rewrite", "Context_Gap": "原文已足夠。"})
        self.assertIs(pipeline.run("原題", self.report), False)
        self.assertEqual(len(self.jev_requests), 1)

    def test_failed_rewrite_falls_back_to_original_and_still_judges(self):
        payloads = [
            {"rewritten_question": None, "Context_Gap": "無法忠實改寫。"},
            {**REWRITE, "rewritten_question": " "},
            {**REWRITE, "rewritten_question": "{Concept: 營業收入}"},
            {**REWRITE, "IFRS-based English Question": "extra"},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.requests.clear()
                self.jev_requests.clear()
                pipeline = self.pipeline(rewrite=payload)
                self.assertIs(pipeline.run("原題", self.report), True)
                self.assertEqual(len(self.requests), 3)
                self.assertEqual(self.jev_requests[0]["state"]["question"], "原題")

    def test_invalid_status_falls_back_without_suppressing_final_judge(self):
        payloads = [
            {"status": "rejected", "Context_Gap": "舊版分類。"},
            {"status": "exact", "Context_Gap": "舊版分類。"},
            {"status": "adapted", "Context_Gap": "舊版分類。"},
            {"status": "unknown", "Context_Gap": "無效分類。"},
            {"Context_Gap": "缺少欄位。"},
            {**STATUS, "Context_Gap": " "},
            {**STATUS, "rewritten_question": "額外欄位"},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.requests.clear()
                self.jev_requests.clear()
                pipeline = self.pipeline(classification=payload)
                prepared = pipeline.prepare_question("原題")
                self.assertEqual(prepared["reason"], "preprocessing_error")
                self.assertEqual(prepared["question_used"], "原題")
                self.assertEqual(prepared["error_stage"], "classify_status")
                self.assertIs(pipeline.is_answerable(prepared["question_used"], self.report), True)
                self.assertEqual(len(self.requests), 2)
                self.assertEqual(len(self.jev_requests), 1)

    def test_refusal_incomplete_and_invalid_template_use_original(self):
        for settings in ({"refusal": True}, {"incomplete": True}, {"malformed_template": True}):
            with self.subTest(settings=settings):
                self.requests.clear()
                self.jev_requests.clear()
                pipeline = self.pipeline(**settings)
                self.assertIs(pipeline.run("原題", self.report), True)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(self.jev_requests[0]["state"]["question"], "原題")

    def test_status_schema_and_prompt_only_allow_two_decisions(self):
        enum=StatusResult.model_json_schema()["properties"]["status"]["enum"]
        self.assertEqual(enum, ["rewrite", "no_rewrite"])
        self.assertNotIn("rejected", prompts.STATUS_SYSTEM_PROMPT)
        self.assertNotIn("exact", prompts.STATUS_SYSTEM_PROMPT)
        self.assertNotIn("adapted", prompts.STATUS_SYSTEM_PROMPT)


class CliTests(unittest.TestCase):
    def test_logging_keeps_bool_stdout_and_saves_usage_cost(self):
        def evaluate(question, catalog, config, recorder):
            # 使用本檔的 SDK mock 流程，不發出真實網路請求。
            with recorder.call("openai", "gpt-6-luna", "DatabaseRoutingResult", "test") as event:
                event["resolved_model"] = "gpt-6-luna"
                event["usage"] = {
                    "input_tokens": 1000, "output_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                }
            return False

        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            self.assertTrue(Path(directory).resolve().is_relative_to(ROOT.resolve()))
            path = Path(directory) / "cost.json"
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("main.needs_full_report", side_effect=evaluate), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                status = main.main(["測試問題", "--log", str(path)])
            self.assertEqual(status, 0)
            self.assertEqual(stdout.getvalue(), "False\n")
            self.assertEqual(stderr.getvalue(), "")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(data["result"], False)
            self.assertEqual(data["total_cost_usd"], 0.00015)
            self.assertEqual(data["unknown_cost_calls"], 0)
            self.assertEqual(data["bool_semantics"], "needs_full_report")
            self.assertEqual(data["pipeline_architecture"], "original_database_routing")

    def test_stdout_only_bool(self):
        for answer in (True, False):
            with self.subTest(answer=answer):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("main.needs_full_report", return_value=answer), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    status = main.main(["測試問題"])
                self.assertEqual(status, 0)
                self.assertEqual(stdout.getvalue(), f"{answer}\n")
                self.assertEqual(stderr.getvalue(), "")

    def test_error_has_no_bool_stdout(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("main.needs_full_report", side_effect=PipelineError("測試錯誤")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = main.main(["測試問題"])
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("測試錯誤", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
