"""前端預設條件、原意保護及 agent 交接的離線整合測試。"""

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
from group_a import AgentDispatcher, GroupARouter, GroupASystem
from models import GroupAInput, PipelineConfig, RewriteValidationResult
from telemetry import CallRecorder
from test_pipeline import ROOT, openai_response


QUESTION = "營收是多少？"
EMPTY_TEMPLATE = {"Time": None, "Concept": None, "Filter": None, "Logic": None}
VALIDATION = {name: True for name in RewriteValidationResult.model_fields if name != "reason"}
VALIDATION["reason"] = "保留原題及已套入的前端條件。"


def requirements(year="missing", company="missing", *, year_evidence=None, company_evidence=None):
    return {
        "year_scope": year, "company_scope": company,
        "year_evidence": year_evidence, "company_evidence": company_evidence,
        "reason": "僅在回答需要條件且原題未指定時套入前端預設。",
    }


def contextual(question=QUESTION, *, year=2025, company="台積電"):
    conditions = []
    if year is not None:
        conditions.append(f"年度：{year}年")
    if company is not None:
        conditions.append(f"公司：{company}")
    return question + ("\n（系統補入查詢條件：" + "；".join(conditions) + "）" if conditions else "")


def request_payload(request):
    return json.loads(request["input"][-1]["content"].rsplit("\n", 1)[-1])


class QuestionContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog(ROOT / "database/2025q4_accounting_items.md")
        cls.config = PipelineConfig.model_validate_json((ROOT / "group_a_config.json").read_text())

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.openai_requests = []
        self.jev_requests = []

    def router(self, *, scopes=None, rewritten=None, no_rewrite=True, validation=None,
               oa_error=None, refusal=None, template=None):
        def oa_handler(request):
            body = json.loads(request.content)
            self.openai_requests.append(body)
            name = body["text"]["format"]["name"]
            if name == oa_error:
                return httpx.Response(503, json={"error": {"message": "private secret"}})
            payloads = {
                "ContextRequirements": requirements() if scopes is None else scopes,
                "QuestionTemplate": EMPTY_TEMPLATE if template is None else template,
                "StatusResult": {"status": "no_rewrite" if no_rewrite else "rewrite", "Context_Gap": "整理用語。"},
                "RewriteResult": {"rewritten_question": rewritten, "Context_Gap": "整理營收用語。"},
                "RewriteValidationResult": VALIDATION if validation is None else validation,
            }
            return httpx.Response(200, json=openai_response(
                payloads[name], model=body["model"], refusal=refusal == name,
            ))

        def jev_handler(request):
            body = json.loads(request.content)
            self.jev_requests.append(body)
            return httpx2.Response(200, json={
                "model": "jev-1.13.0", "answers": {
                    name: {"type": "noul", "noul": 0.1} for name in prompts.JEV_ROUTING_CHECKS
                }, "usage": {"input_tokens": 100, "output_tokens": 10},
            })

        oa = self.stack.enter_context(OpenAI(api_key="offline-test", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(oa_handler))))
        jev = self.stack.enter_context(TypeSafeClient(api_key="offline-test", retry=RetryPolicy(max_retries=0),
                                                    transport=httpx2.MockTransport(jev_handler)))
        return GroupARouter(self.config, self.catalog, oa, jev)

    def assert_resolution(self, result, question, *, year=None, company=None):
        resolved = result.context_resolution
        expected = contextual(question, year=year, company=company)
        self.assertEqual(result.status, "completed")
        self.assertEqual(resolved.status, "completed")
        self.assertEqual(resolved.original_question, question)
        self.assertEqual(resolved.contextual_question, expected)
        self.assertEqual(resolved.applied_year, year)
        self.assertEqual(resolved.applied_company, company)
        self.assertEqual(result.original_question, question)
        self.assertEqual(result.preprocessing.original_question, question)
        self.assertEqual(result.preprocessing.contextual_question, expected)
        self.assertEqual(result.preprocessing.question_used, expected)
        self.assertEqual(result.preprocessing.outcome, "contextual" if year is not None or company is not None else "original")

    def test_frontend_dict_fills_both_required_conditions_before_preprocessing(self):
        recorder = CallRecorder()
        result = self.router().route({
            "original_question": QUESTION, "latest_year": 2025, "current_company": "台積電",
        }, recorder)
        self.assert_resolution(result, QUESTION, year=2025, company="台積電")
        self.assertEqual(result.context_resolution.latest_year, 2025)
        self.assertEqual(result.context_resolution.current_company, "台積電")
        names = [request["text"]["format"]["name"] for request in self.openai_requests]
        self.assertEqual(names, ["ContextRequirements", "QuestionTemplate", "StatusResult"])
        self.assertEqual(len(recorder.calls), 4)
        requirement_input = request_payload(self.openai_requests[0])
        self.assertEqual(requirement_input["original_question"], QUESTION)
        self.assertEqual(requirement_input, {"original_question": QUESTION})
        self.assertEqual(request_payload(self.openai_requests[1])["user_question"], contextual())
        self.assertEqual(request_payload(self.openai_requests[2])["user_question"], contextual())
        state = self.jev_requests[0]["state"]
        self.assertEqual(state["original_question"], QUESTION)
        self.assertEqual(state["contextual_question"], contextual())
        self.assertEqual(state["question_used"], contextual())
        self.assertEqual(state["applied_context"], {"year": 2025, "company": "台積電"})
        self.assertIn("contextual_question", json.dumps(self.jev_requests[0]["questions"]))

    def test_typed_frontend_input_is_supported_by_system_and_agent(self):
        seen = []
        frontend = GroupAInput(original_question=QUESTION, latest_year=2025, current_company="台積電")
        result = GroupASystem(self.router(), AgentDispatcher(database_agent=lambda request:
            (seen.append(request) or {"status": "completed", "answer": "答案"}))).run(frontend)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].original_question, QUESTION)
        self.assertEqual(seen[0].contextual_question, contextual())
        self.assertEqual(seen[0].question_used, contextual())
        self.assertEqual(seen[0].applied_year, 2025)
        self.assertEqual(seen[0].applied_company, "台積電")

    def test_explicit_year_is_kept_and_only_company_is_filled(self):
        question = "2024年營收是多少？"
        result = self.router(scopes=requirements("specified", year_evidence="2024年")).route(
            GroupAInput(original_question=question, latest_year=2025, current_company="台積電"))
        self.assert_resolution(result, question, company="台積電")
        self.assertEqual(self.jev_requests[-1]["state"]["applied_context"], {"company": "台積電"})

    def test_explicit_company_is_kept_and_only_year_is_filled(self):
        question = "聯發科的營收是多少？"
        result = self.router(scopes=requirements(company="specified", company_evidence="聯發科")).route(
            {"original_question": question, "latest_year": 2025, "current_company": "台積電"})
        self.assert_resolution(result, question, year=2025)
        self.assertEqual(self.jev_requests[-1]["state"]["applied_context"], {"year": 2025})

    def test_both_explicit_conditions_keep_original_and_apply_nothing(self):
        question = "聯發科2024年營收是多少？"
        scopes = requirements("specified", "specified", year_evidence="2024年", company_evidence="聯發科")
        result = self.router(scopes=scopes).route(
            {"original_question": question, "latest_year": 2025, "current_company": "台積電"})
        self.assert_resolution(result, question)
        self.assertEqual(self.jev_requests[-1]["state"]["applied_context"], {})

    def test_definition_does_not_get_arbitrary_year_or_company(self):
        question = "營業收入的定義是什麼？"
        scopes = requirements("not_required", "not_required")
        result = self.router(scopes=scopes).route(
            {"original_question": question, "latest_year": 2025, "current_company": "台積電"})
        self.assert_resolution(result, question)
        self.assertEqual(self.jev_requests[-1]["state"]["applied_context"], {})

    def test_relative_periods_are_preserved_instead_of_replaced_by_latest_year(self):
        for period in ("今年", "去年", "最近三年"):
            with self.subTest(period=period):
                question = f"{period}營收是多少？"
                result = self.router(scopes=requirements("specified", year_evidence=period)).route(
                    {"original_question": question, "latest_year": 2025, "current_company": "台積電"})
                self.assert_resolution(result, question, company="台積電")

    def test_quarter_without_year_retains_quarter_and_receives_year(self):
        question = "第2季營收是多少？"
        result = self.router().route(
            {"original_question": question, "latest_year": 2025, "current_company": "台積電"})
        self.assert_resolution(result, question, year=2025, company="台積電")
        self.assertIn("第2季", result.preprocessing.question_used)

    def test_year_shaped_amount_is_not_treated_as_an_explicit_year(self):
        question = "營收有超過2025元嗎？"
        result = self.router().route(
            {"original_question": question, "latest_year": 2026, "current_company": "台積電"})
        self.assert_resolution(result, question, year=2026, company="台積電")
        self.assertIn("超過2025元", result.preprocessing.question_used)

    def test_search_for_year_is_not_narrowed_to_latest_year(self):
        question = "哪一年營收最高？"
        result = self.router(scopes=requirements("not_required")).route(
            {"original_question": question, "latest_year": 2025, "current_company": "台積電"})
        self.assert_resolution(result, question, company="台積電")

    def test_cross_company_search_does_not_become_current_company_only(self):
        question = "2025年營收最高的上市公司有哪些？"
        scopes = requirements("specified", "not_required", year_evidence="2025年")
        result = self.router(scopes=scopes).route(
            {"original_question": question, "latest_year": 2025, "current_company": "台積電"})
        self.assert_resolution(result, question)

    def test_raw_question_and_contextless_object_skip_extra_model_call(self):
        for frontend in (QUESTION, {"original_question": QUESTION}, GroupAInput(original_question=QUESTION)):
            with self.subTest(frontend=frontend):
                start = len(self.openai_requests)
                result = self.router().route(frontend)
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.context_resolution.status, "skipped")
                self.assertEqual(result.preprocessing.contextual_question, QUESTION)
                self.assertEqual(result.preprocessing.question_used, QUESTION)
                self.assertEqual([r["text"]["format"]["name"] for r in self.openai_requests[start:]],
                                 ["QuestionTemplate", "StatusResult"])

    def test_single_available_default_is_enough_when_other_scope_is_not_required(self):
        cases = [
            ({"latest_year": 2025}, requirements("missing", "not_required"),
             "上市公司營收最高的是哪一家？", 2025, None),
            ({"current_company": "台積電"}, requirements("not_required", "missing"),
             "哪一年營收最高？", None, "台積電"),
        ]
        for defaults, scopes, question, year, company in cases:
            with self.subTest(defaults=defaults):
                result = self.router(scopes=scopes).route({"original_question": question, **defaults})
                self.assert_resolution(result, question, year=year, company=company)

    def test_required_default_missing_stops_before_preprocessing_and_dispatch(self):
        for defaults in ({"latest_year": 2025}, {"current_company": "台積電"}):
            with self.subTest(defaults=defaults):
                seen = []
                start_oa, start_jev = len(self.openai_requests), len(self.jev_requests)
                result = GroupASystem(self.router(), AgentDispatcher(database_agent=lambda request:
                    seen.append(request), full_report_agent=lambda request: seen.append(request))).run(
                        {"original_question": QUESTION, **defaults})
                self.assertEqual(result.status, "routing_error")
                self.assertEqual(result.routing.error_stage, "context_resolution")
                self.assertEqual(result.routing.context_resolution.status, "error")
                self.assertEqual(result.routing.error_type, "MissingContextParameters")
                self.assertIsNone(result.routing.route)
                self.assertIsNone(result.routing.needs_full_report)
                self.assertEqual(len(self.openai_requests) - start_oa, 1)
                self.assertEqual(len(self.jev_requests), start_jev)
                self.assertEqual(seen, [])

    def test_context_model_errors_refusal_and_invalid_schema_never_dispatch(self):
        cases = [
            {"oa_error": "ContextRequirements"},
            {"refusal": "ContextRequirements"},
            {"scopes": {**requirements(), "year_scope": "guess"}},
            {"scopes": {**requirements(), "reason": " "}},
        ]
        for options in cases:
            with self.subTest(options=options):
                seen = []
                start_oa, start_jev = len(self.openai_requests), len(self.jev_requests)
                recorder = CallRecorder()
                result = GroupASystem(self.router(**options), AgentDispatcher(database_agent=lambda request:
                    seen.append(request), full_report_agent=lambda request: seen.append(request))).run(
                        {"original_question": QUESTION, "latest_year": 2025, "current_company": "台積電"}, recorder)
                self.assertEqual(result.status, "routing_error")
                self.assertEqual(result.routing.context_resolution.status, "error")
                self.assertEqual(result.routing.error_stage, "context_resolution")
                self.assertEqual(len(self.openai_requests) - start_oa, 1)
                self.assertEqual(len(self.jev_requests), start_jev)
                self.assertEqual(seen, [])
                self.assertNotIn("private secret", result.model_dump_json())
                self.assertNotIn("private secret", json.dumps(recorder.calls))

    def test_context_evidence_must_be_a_raw_original_fragment(self):
        for scopes in (
            requirements("specified", year_evidence="2024年"),
            requirements(company="specified", company_evidence="聯發科"),
            requirements("missing", year_evidence="營收"),
        ):
            with self.subTest(scopes=scopes):
                start_jev = len(self.jev_requests)
                result = self.router(scopes=scopes).route(
                    {"original_question": QUESTION, "latest_year": 2025, "current_company": "台積電"})
                self.assertEqual(result.status, "error")
                self.assertEqual(result.error_stage, "context_resolution")
                self.assertEqual(len(self.jev_requests), start_jev)

    def test_local_guard_rejects_model_that_overwrites_explicit_year_or_current_company(self):
        for question in ("2024年營收是多少？", "台積電營收是多少？"):
            with self.subTest(question=question):
                seen = []
                start_jev = len(self.jev_requests)
                result = GroupASystem(self.router(), AgentDispatcher(database_agent=lambda request:
                    seen.append(request))).run(
                        {"original_question": question, "latest_year": 2025, "current_company": "台積電"})
                self.assertEqual(result.status, "routing_error")
                self.assertEqual(result.routing.error_stage, "context_resolution")
                self.assertEqual(len(self.jev_requests), start_jev)
                self.assertEqual(seen, [])

    def test_bad_frontend_input_is_rejected_before_api_calls(self):
        bad_inputs = [
            {"original_question": " "}, {"original_question": None},
            {"original_question": 123}, {"original_question": QUESTION, "latest_year": "2025"},
            {"original_question": QUESTION, "latest_year": True},
            {"original_question": QUESTION, "latest_year": 2025.5},
            {"original_question": QUESTION, "latest_year": 0},
            {"original_question": QUESTION, "current_company": " "},
            {"original_question": QUESTION, "current_company": 2330},
            {"original_question": QUESTION, "unknown_parameter": "台積電"},
        ]
        for frontend in bad_inputs:
            with self.subTest(frontend=frontend), self.assertRaises(ValueError):
                self.router().route(frontend)
        self.assertEqual(self.openai_requests + self.jev_requests, [])

    def test_verified_rewrite_uses_enriched_baseline_and_retains_raw_original(self):
        rewritten = contextual().replace("營收", "營業收入")
        result = self.router(no_rewrite=False, rewritten=rewritten).route(
            {"original_question": QUESTION, "latest_year": 2025, "current_company": "台積電"})
        self.assertEqual(result.preprocessing.question_used, rewritten)
        self.assertEqual(result.preprocessing.contextual_question, contextual())
        self.assertEqual(result.preprocessing.original_question, QUESTION)
        validation_request = next(r for r in self.openai_requests
                                  if r["text"]["format"]["name"] == "RewriteValidationResult")
        self.assertEqual(request_payload(validation_request)["original_question"], contextual())
        self.assertEqual(request_payload(validation_request)["rewritten_question"], rewritten)
        state = self.jev_requests[-1]["state"]
        self.assertEqual(state["original_question"], QUESTION)
        self.assertEqual(state["contextual_question"], contextual())
        self.assertEqual(state["question_used"], rewritten)

    def test_rewrite_failure_or_null_keeps_applied_context(self):
        cases = [
            {"no_rewrite": False, "rewritten": None},
            {"no_rewrite": False, "oa_error": "RewriteResult"},
            {"no_rewrite": False, "oa_error": "StatusResult"},
            {"no_rewrite": False, "oa_error": "QuestionTemplate"},
            {"no_rewrite": False, "rewritten": contextual().replace("營收", "營業收入"),
             "validation": {**VALIDATION, "company_preserved": False}},
            {"no_rewrite": False, "rewritten": "營業收入是多少？"},
        ]
        for options in cases:
            with self.subTest(options=options):
                result = self.router(**options).route(
                    {"original_question": QUESTION, "latest_year": 2025, "current_company": "台積電"})
                self.assert_resolution(result, QUESTION, year=2025, company="台積電")
                self.assertNotIn("private secret", result.model_dump_json())

    def test_database_escalation_preserves_context_in_both_agent_requests(self):
        seen = []
        def database(request):
            seen.append(request)
            return {"status": "needs_full_report", "reason": "指定條件資料不足。"}
        def full_report(request):
            seen.append(request)
            return {"status": "completed", "answer": "完整財報答案"}
        result = GroupASystem(self.router(), AgentDispatcher(
            database_agent=database, full_report_agent=full_report)).run(
                {"original_question": QUESTION, "latest_year": 2025, "current_company": "台積電"})
        self.assertEqual(result.status, "completed")
        self.assertEqual([r.route for r in seen], ["database", "full_report"])
        for request in seen:
            self.assertEqual(request.original_question, QUESTION)
            self.assertEqual(request.contextual_question, contextual())
            self.assertEqual(request.question_used, contextual())
            self.assertEqual(request.applied_year, 2025)
            self.assertEqual(request.applied_company, "台積電")
        self.assertEqual(seen[-1].previous_attempts[0].response.reason, "指定條件資料不足。")

    def test_cli_year_company_flags_and_log_preserve_frontend_and_applied_conditions(self):
        router = self.router()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, \
                patch("main_a.route_group_a", side_effect=lambda q, c, config, log: router.route(q, log)), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            path = Path(directory) / "route.json"
            code = main_a.main([QUESTION, "--latest-year", "2025", "--current-company", "台積電", "--log", str(path)])
            payload = json.loads(stdout.getvalue())
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["original_question"], QUESTION)
        self.assertEqual(payload["context_resolution"]["contextual_question"], contextual())
        self.assertEqual(saved["context_resolution"]["latest_year"], 2025)
        self.assertEqual(saved["context_resolution"]["current_company"], "台積電")
        self.assertEqual(saved["context_resolution"]["applied_year"], 2025)
        self.assertEqual(saved["context_resolution"]["applied_company"], "台積電")
        self.assertEqual(len(saved["calls"]), 4)
        self.assertIsNotNone(saved["calls"][0]["usage"])
        self.assertIsNotNone(saved["total_cost_usd"])

    def test_cli_frontend_json_file_is_accepted_without_positional_question(self):
        router = self.router()
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, \
                patch("main_a.route_group_a", side_effect=lambda q, c, config, log: router.route(q, log)), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            path = Path(directory) / "frontend.json"
            path.write_text(json.dumps({"original_question": QUESTION, "latest_year": 2025,
                                       "current_company": "台積電"}, ensure_ascii=False), encoding="utf-8")
            code = main_a.main(["--input-json", str(path)])
            payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["context_resolution"]["contextual_question"], contextual())
        self.assertEqual(payload["original_question"], QUESTION)

    def test_cli_context_resolution_error_has_nonzero_exit_and_null_route(self):
        router = self.router(oa_error="ContextRequirements")
        with patch("main_a.route_group_a", side_effect=lambda q, c, config, log: router.route(q, log)), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            code = main_a.main([QUESTION, "--latest-year", "2025", "--current-company", "台積電"])
            payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["context_resolution"]["status"], "error")
        self.assertEqual(payload["error_stage"], "context_resolution")
        self.assertIsNone(payload["route"])
        self.assertIsNone(payload["needs_full_report"])

    def test_cli_json_file_cannot_be_mixed_with_positional_or_default_flags(self):
        for extra in ([QUESTION], ["--latest-year", "2025"], ["--current-company", "台積電"]):
            with self.subTest(extra=extra), patch("main_a.route_group_a") as route, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main_a.main(["--input-json", str(ROOT / "unused-frontend.json"), *extra])
            self.assertEqual(error.exception.code, 2)
            route.assert_not_called()

    def test_cli_invalid_frontend_json_stops_before_api_calls(self):
        for contents in ("{", "[]", json.dumps({"original_question": QUESTION, "latest_year": "2025"})):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory(dir=ROOT) as directory, \
                    patch("main_a.route_group_a") as route, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                path = Path(directory) / "frontend.json"
                path.write_text(contents, encoding="utf-8")
                code = main_a.main(["--input-json", str(path)])
                self.assertEqual(code, 1)
                route.assert_not_called()


if __name__ == "__main__":
    unittest.main()
