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

import main_a
import prompts
from database_catalog import load_catalog
from group_a import AgentDispatcher, GroupARouter, GroupASystem, route_group_a
from models import AgentResponse, PipelineConfig, QuestionTemplate, RewriteValidationResult
from rewrite_validation import local_rewrite_check, validate_template_fragments
from telemetry import CallRecorder
from test_pipeline import ROOT, openai_response


ORIGINAL = "甲公司2025年兩大客戶的營收差額是多少？"
REWRITTEN = "甲公司2025年度兩大客戶的營業收入差額是多少？"
TEMPLATE = {"Time": "2025年", "Concept": "營收", "Filter": "甲公司；兩大客戶", "Logic": "差額"}
VALIDATION = {name: True for name in RewriteValidationResult.model_fields if name != "reason"}
VALIDATION["reason"] = "公司、期間、客戶範圍及差額計算均保留。"


class GroupATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog(ROOT / "database/2025q4_accounting_items.md")
        cls.config = PipelineConfig.model_validate_json((ROOT / "group_a_config.json").read_text())

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.openai_requests = []
        self.jev_requests = []

    def router(self, *, rewritten=REWRITTEN, validation=None, no_rewrite=False,
               template=None, oa_error=None, jev_error=False, missing_check=False,
               probabilities=None, validation_refusal=False):
        def oa_handler(request):
            body = json.loads(request.content)
            self.openai_requests.append(body)
            name = body["text"]["format"]["name"]
            if name == oa_error:
                return httpx.Response(503, json={"error": {"message": "private secret"}})
            payloads = {
                "QuestionTemplate": TEMPLATE if template is None else template,
                "StatusResult": {"status": "no_rewrite" if no_rewrite else "rewrite", "Context_Gap": "整理用語。"},
                "RewriteResult": {"rewritten_question": rewritten, "Context_Gap": "整理營收用語。"},
                "RewriteValidationResult": VALIDATION if validation is None else validation,
            }
            return httpx.Response(200, json=openai_response(
                payloads[name], model=body["model"], refusal=validation_refusal and name == "RewriteValidationResult",
            ))

        def jev_handler(request):
            body = json.loads(request.content)
            self.jev_requests.append(body)
            if jev_error:
                return httpx2.Response(503, json={"error": "private secret"})
            values = probabilities or {name: 0.1 for name in prompts.JEV_ROUTING_CHECKS}
            return httpx2.Response(200, json={
                "model": "jev-1.13.0", "answers": {
                    name: {"type": "noul", "noul": probability} for name, probability in values.items()
                    if not (missing_check and name == "missing_detail")
                }, "usage": {"input_tokens": 100, "output_tokens": 10},
            })
        oa = self.stack.enter_context(OpenAI(api_key="offline-test", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(oa_handler))))
        jev = self.stack.enter_context(TypeSafeClient(api_key="offline-test", retry=RetryPolicy(max_retries=0),
                                                    transport=httpx2.MockTransport(jev_handler)))
        return GroupARouter(self.config, self.catalog, oa, jev)

    def test_verified_rewrite_retains_original_in_judgment_and_handoff(self):
        seen = []
        def database_agent(request):
            seen.append(request)
            return {"status": "completed", "answer": {"difference": 0}, "reason": "agent 自行查核資料。"}
        recorder = CallRecorder()
        result = GroupASystem(self.router(), AgentDispatcher(database_agent=database_agent)).run(ORIGINAL, recorder)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.final_route, "database")
        self.assertEqual(result.answer, {"difference": 0})
        self.assertEqual(result.routing.preprocessing.question_used, REWRITTEN)
        self.assertTrue(result.routing.preprocessing.rewrite_validation.is_faithful)
        self.assertEqual([r["text"]["format"]["name"] for r in self.openai_requests],
                         ["QuestionTemplate", "StatusResult", "RewriteResult", "RewriteValidationResult"])
        self.assertEqual(len(self.jev_requests), 1)
        state = self.jev_requests[0]["state"]
        self.assertEqual(state["original_question"], ORIGINAL)
        self.assertEqual(state["question_used"], REWRITTEN)
        self.assertEqual(seen[0].original_question, ORIGINAL)
        self.assertEqual(seen[0].question_used, REWRITTEN)
        self.assertEqual(result.routing.data_availability, "delegated_to_database_agent")
        self.assertEqual(len(recorder.calls), 5)
        self.assertEqual(set(result.routing.judgment.probabilities), set(prompts.JEV_ROUTING_CHECKS))

    def test_semantic_changes_are_rejected_and_original_is_dispatched(self):
        cases = [
            ("乙公司2025年度兩大客戶的營業收入差額是多少？", "company_preserved"),
            ("甲公司2025年度營業收入差額是多少？", "filters_preserved"),
            ("甲公司2025年度兩大客戶的營業收入成長率是多少？", "operations_preserved"),
        ]
        for rewritten, failed_field in cases:
            with self.subTest(failed_field=failed_field):
                seen = []
                validator = {**VALIDATION, failed_field: False, "reason": "原題條件遭到改變。"}
                router = self.router(rewritten=rewritten, validation=validator)
                result = GroupASystem(router, AgentDispatcher(database_agent=lambda request:
                    (seen.append(request) or AgentResponse(status="completed", answer="agent 答案")))).run(ORIGINAL)
                self.assertEqual(result.routing.preprocessing.reason, "rewrite_validation_failed")
                self.assertEqual(result.routing.preprocessing.question_used, ORIGINAL)
                self.assertEqual(seen[0].question_used, ORIGINAL)

    def test_changed_year_is_rejected_before_semantic_verifier(self):
        result = self.router(rewritten=REWRITTEN.replace("2025", "2024")).route(ORIGINAL)
        self.assertEqual(result.preprocessing.question_used, ORIGINAL)
        self.assertFalse(result.preprocessing.rewrite_validation.period_preserved)
        self.assertEqual(len(self.openai_requests), 3)

    def test_verification_error_refusal_or_invalid_result_falls_back(self):
        cases = [
            {"oa_error": "RewriteValidationResult"},
            {"validation_refusal": True},
            {"validation": {**VALIDATION, "company_preserved": "true"}},
        ]
        for options in cases:
            with self.subTest(options=options):
                result = self.router(**options).route(ORIGINAL)
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.preprocessing.question_used, ORIGINAL)
                self.assertEqual(result.preprocessing.error_stage, "validate_rewrite")
                self.assertNotIn("private secret", result.model_dump_json())

    def test_no_rewrite_null_and_unchanged_do_not_add_validation_call(self):
        for options, calls in [({"no_rewrite": True}, 2), ({"rewritten": None}, 3), ({"rewritten": ORIGINAL}, 3)]:
            with self.subTest(options=options):
                start = len(self.openai_requests)
                result = self.router(**options).route(ORIGINAL)
                self.assertEqual(result.preprocessing.question_used, ORIGINAL)
                self.assertIsNone(result.preprocessing.rewrite_validation)
                self.assertEqual(len(self.openai_requests) - start, calls)

    def test_hallucinated_template_falls_back_before_status(self):
        result = self.router(template={**TEMPLATE, "Filter": "乙公司"}).route(ORIGINAL)
        self.assertEqual(result.preprocessing.question_used, ORIGINAL)
        self.assertEqual(result.preprocessing.error_stage, "validate_template")
        self.assertEqual(len(self.openai_requests), 1)

    def test_judgment_error_never_dispatches_an_agent_or_becomes_false(self):
        for options in ({"jev_error": True}, {"missing_check": True}):
            with self.subTest(options=options):
                seen = []
                handler = lambda request: seen.append(request)
                result = GroupASystem(self.router(**options), AgentDispatcher(
                    database_agent=handler, full_report_agent=handler,
                )).run(ORIGINAL)
                self.assertEqual(result.status, "routing_error")
                self.assertIsNone(result.routing.needs_full_report)
                self.assertIsNone(result.routing.route)
                self.assertEqual(seen, [])
                self.assertNotIn("private secret", result.model_dump_json())

    def test_full_report_decision_only_calls_full_report_agent(self):
        seen = []
        probabilities = {name: (0.5 if name == "missing_detail" else 0.1) for name in prompts.JEV_ROUTING_CHECKS}
        result = GroupASystem(self.router(probabilities=probabilities), AgentDispatcher(
            database_agent=lambda request: self.fail("資料庫 agent 不應被呼叫"),
            full_report_agent=lambda request: (seen.append(request) or {"status": "completed", "answer": "完整財報答案"}),
        )).run(ORIGINAL)
        self.assertEqual(result.final_route, "full_report")
        self.assertTrue(result.routing.needs_full_report)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].original_question, ORIGINAL)

    def test_database_insufficiency_escalates_once_and_keeps_trace(self):
        for db_status in ("needs_full_report", "insufficient_data"):
            with self.subTest(db_status=db_status):
                seen = []
                def db(request):
                    seen.append(request.route)
                    return {"status": db_status, "reason": "指定公司的資料不足。"}
                def full(request):
                    seen.append(request.route)
                    self.assertEqual(request.previous_attempts[0].response.reason, "指定公司的資料不足。")
                    return {"status": "completed", "answer": "完整財報答案"}
                result = GroupASystem(self.router(), AgentDispatcher(database_agent=db, full_report_agent=full)).run(ORIGINAL)
                self.assertEqual(seen, ["database", "full_report"])
                self.assertEqual([a.route for a in result.attempts], seen)
                self.assertFalse(result.routing.needs_full_report)
                self.assertEqual(result.final_route, "full_report")

    def test_full_report_insufficiency_stops_without_loop_or_fabricated_answer(self):
        seen = []
        def full(request):
            seen.append(request)
            return {"status": "needs_full_report", "reason": "完整財報也沒有揭露。"}
        result = GroupASystem(self.router(), AgentDispatcher(
            database_agent=lambda request: {"status": "needs_full_report", "reason": "缺資料。"},
            full_report_agent=full,
        )).run(ORIGINAL)
        self.assertEqual(result.status, "insufficient_data")
        self.assertIsNone(result.answer)
        self.assertEqual(len(seen), 1)

    def test_agent_errors_and_invalid_answers_do_not_escalate(self):
        def exception(request):
            raise RuntimeError("private secret")
        for handler in (exception, lambda request: {"status": "completed", "answer": None},
                        lambda request: {"status": "error", "reason": "agent 服務失敗。"}):
            with self.subTest(handler=handler):
                result = GroupASystem(self.router(), AgentDispatcher(database_agent=handler,
                    full_report_agent=lambda request: self.fail("服務錯誤不能假装是資料不足"))).run(ORIGINAL)
                self.assertEqual(result.status, "agent_error")
                self.assertEqual(len(result.attempts), 1)
                self.assertNotIn("private secret", result.model_dump_json())

    def test_unconnected_agents_are_reported_as_awaiting_agent(self):
        routing = self.router().route(ORIGINAL)
        result = AgentDispatcher().dispatch(routing)
        self.assertEqual(result.status, "awaiting_agent")
        self.assertEqual(result.final_route, "database")
        self.assertEqual(result.attempts, [])
        result = AgentDispatcher(database_agent=lambda request: {
            "status": "needs_full_report", "reason": "缺資料。",
        }).dispatch(routing)
        self.assertEqual(result.final_route, "full_report")
        self.assertEqual(len(result.attempts), 1)

    def test_empty_question_calls_nothing(self):
        with self.assertRaises(ValueError):
            self.router().route("   ")
        self.assertEqual(self.openai_requests + self.jev_requests, [])

    def test_cli_json_and_cost_log(self):
        router = self.router()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, \
                patch("main_a.route_group_a", side_effect=lambda q, c, config, log: router.route(q, log)), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            path = Path(directory) / "route.json"
            code = main_a.main([ORIGINAL, "--log", str(path)])
            payload = json.loads(stdout.getvalue())
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["route"], "database")
        self.assertEqual(payload["bool_semantics"], "needs_full_report")
        self.assertEqual(saved["original_question"], ORIGINAL)
        self.assertEqual(len(saved["calls"]), 5)
        self.assertIsNotNone(saved["calls"][0]["usage"])
        self.assertIsNotNone(saved["total_cost_usd"])

    def test_cli_routing_error_keeps_null_result_and_nonzero_exit(self):
        result = self.router(jev_error=True).route(ORIGINAL)
        with patch("main_a.route_group_a", return_value=result), redirect_stdout(io.StringIO()) as stdout:
            code = main_a.main([ORIGINAL])
        self.assertEqual(code, 1)
        self.assertIsNone(json.loads(stdout.getvalue())["needs_full_report"])

    def test_managed_entry_needs_both_keys_without_calling_api(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "", "TYPESAFE_API_KEY": ""}), \
                patch("group_a.OpenAI") as client, self.assertRaises(ValueError):
            route_group_a(ORIGINAL, self.catalog, self.config)
        client.assert_not_called()


class ProtectedDetailTests(unittest.TestCase):
    def test_year_currency_and_threshold_changes_are_rejected(self):
        for original, rewritten in [
            ("甲公司2025年營收？", "甲公司2024年營業收入？"),
            ("超過10%的公司？", "超過5%的公司？"),
            ("美元100萬元？", "新臺幣100萬元？"),
            ("營業收入？", "2025年營業收入？"),
        ]:
            with self.subTest(original=original):
                self.assertFalse(local_rewrite_check(original, rewritten).is_faithful)

    def test_equivalent_dates_numbers_and_currency_translation_can_be_reviewed(self):
        for original, rewritten in [
            ("民國114年營收？", "2025年營業收入？"),
            ("甲公司２０２５年營收超過１，０００？", "甲公司2025年營業收入超過1000？"),
            ("Revenue in USD in 2025?", "2025年美元營業收入？"),
        ]:
            with self.subTest(original=original):
                self.assertIsNone(local_rewrite_check(original, rewritten))

    def test_template_must_use_original_fragments(self):
        validate_template_fragments(ORIGINAL, QuestionTemplate(**TEMPLATE))
        for value in ("", "乙公司", "甲公司；"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_template_fragments(ORIGINAL, QuestionTemplate(**{**TEMPLATE, "Filter": value}))


if __name__ == "__main__":
    unittest.main()
