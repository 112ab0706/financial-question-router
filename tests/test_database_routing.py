import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import OpenAI
from pydantic import ValidationError

import main
import prompts
from database_catalog import load_catalog
from database_routing import DatabaseQuestionRouter, needs_full_report
from models import RoutingConfig
from pipeline import PipelineError
from test_pipeline import ROOT, openai_response

CATALOG_PATH = ROOT / "database" / "2025q4_accounting_items.md"


class DatabaseCatalogTests(unittest.TestCase):
    def test_jev_view_keeps_all_account_meanings_and_statement_memberships(self):
        catalog = load_catalog(CATALOG_PATH)
        with_ids, without_ids = catalog.to_judgment_state(), catalog.to_jev_state()
        for statement, text in with_ids["statements"].items():
            self.assertEqual(without_ids["statements"][statement].splitlines(),
                             [line.split("|", 1)[1] for line in text.splitlines()])
        self.assertEqual(without_ids["item_count"], len(catalog.items))
        self.assertEqual(without_ids["source_sha256"], with_ids["source_sha256"])
        self.assertEqual(without_ids["item_columns"], ["label", "element_if_ambiguous"])
        self.assertIn("第一欄為會計科目名稱", prompts.DATABASE_ROUTING_JEV_INSTRUCTIONS)
        self.assertNotIn("第一欄為以逗號分隔的 item_id", prompts.DATABASE_ROUTING_JEV_INSTRUCTIONS)

    def test_judgment_view_keeps_every_id_label_statement_and_ambiguous_element(self):
        catalog = load_catalog(CATALOG_PATH)
        view = catalog.to_judgment_state()
        seen = {}
        elements_by_label = {}
        for item in catalog.items:
            elements_by_label.setdefault(item.label, set()).add(item.element)
        for statement, text in view["statements"].items():
            for line in text.splitlines():
                cells = line.split("|")
                for item_id in cells[0].split(","):
                    self.assertNotIn(item_id, seen)
                    item = catalog.by_id[item_id]
                    self.assertEqual(item.statement, statement)
                    self.assertEqual(item.label, cells[1])
                    if len(elements_by_label[item.label]) > 1:
                        self.assertEqual(item.element, cells[2])
                    seen[item_id] = item.qname
        self.assertEqual(seen, {i.item_id: i.qname for i in catalog.items})
        self.assertEqual(view["item_count"], 1743)

    def test_all_four_statements_and_qnames_survive_compaction(self):
        catalog = load_catalog(CATALOG_PATH)
        self.assertEqual(dict(Counter(item.statement for item in catalog.items)), {
            "資產負債表": 863, "綜合損益表": 555, "現金流量表": 272, "權益變動表": 53,
        })
        self.assertEqual(len(catalog.by_id), 1743)
        state = catalog.to_state()
        reconstructed = {
            (statement, row[0]): "{" + state["namespaces"][row[3]] + "}" + row[2]
            for statement, rows in state["statements"].items() for row in rows
        }
        self.assertEqual(reconstructed, {(i.statement, i.item_id): i.qname for i in catalog.items})
        self.assertIn("聯集", catalog.source_scope)
        self.assertEqual(state["catalog_kind"], "observed_items_union")

    def test_truncated_or_duplicate_catalog_fails_before_any_model_call(self):
        original = CATALOG_PATH.read_text(encoding="utf-8-sig")
        row = next(line for line in original.splitlines() if line.startswith("| CashEquivalents"))
        for text in (original.replace(row + "\n", "", 1), original.replace(row, row + "\n" + row, 1)):
            with self.subTest(text_length=len(text)), tempfile.TemporaryDirectory(dir=ROOT) as directory:
                path = Path(directory) / "items.md"
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_catalog(path)


class DatabaseRoutingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_catalog(CATALOG_PATH)
        self.config = RoutingConfig.model_validate_json((ROOT / "routing_config.json").read_text())
        self.requests = []
        self.item = next(i for i in self.catalog.items if i.element == "Revenue")

    def judge_mock(self, payload, *, question="甲公司2025年的營收是多少？", check=None, refusal=False):
        def handler(request):
            body = json.loads(request.content)
            self.requests.append(body)
            return httpx.Response(200, json=openai_response(payload, model=body["model"], refusal=refusal))
        with OpenAI(api_key="offline-test", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            router = DatabaseQuestionRouter(self.config, client)
            result = router.judge(question, self.catalog, check)
            return result, router.recorder

    def test_original_sent_once_with_full_catalog_and_new_semantics(self):
        question = "as of september 27 , 2014 , what was revenue ?"
        result, recorder = self.judge_mock({
            "needs_full_report": False, "required_item_ids": [self.item.item_id], "reason": "營業收入科目足夠。",
        }, question=question)
        self.assertIs(result.needs_full_report, False)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request["model"], "gpt-6-luna")
        self.assertEqual(request["reasoning"], {"effort": "low"})
        self.assertFalse(request["store"])
        self.assertEqual(request["input"][0]["content"], prompts.DATABASE_ROUTING_SYSTEM_PROMPT)
        payload = json.loads(request["input"][1]["content"].split("\n", 1)[1])
        self.assertEqual(set(payload), {"original_question", "database_catalog"})
        self.assertEqual(payload["original_question"], question)
        self.assertEqual(payload["database_catalog"], self.catalog.to_judgment_state())
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0]["step"], "DatabaseRoutingResult")
        self.assertEqual(recorder.calls[0]["routing"]["data_availability"], "unchecked")
        self.assertEqual(set(request["text"]["format"]["schema"]["properties"]), {
            "needs_full_report", "required_item_ids", "reason",
        })

    def test_missing_detail_routes_to_full_report_without_answering(self):
        result, _ = self.judge_mock({
            "needs_full_report": True, "required_item_ids": [],
            "reason": "缺少兩大客戶應收帳款明細，應收帳款合計不能代替。",
        })
        self.assertIs(result.needs_full_report, True)

    def test_data_check_receives_exact_question_and_qnames(self):
        for available in (True, False):
            with self.subTest(available=available):
                seen = []
                def check(question, items):
                    seen.append((question, items))
                    return available
                result, recorder = self.judge_mock({
                    "needs_full_report": False, "required_item_ids": [self.item.item_id], "reason": "科目足夠。",
                }, question="公司甲的2014年營收？", check=check)
                self.assertEqual(seen, [("公司甲的2014年營收？", [self.item])])
                self.assertIs(result.needs_full_report, not available)
                self.assertEqual(recorder.calls[0]["routing"]["needs_full_report"], not available)

    def test_unknown_item_or_invalid_model_result_cannot_bypass_full_report(self):
        invalid = [
            {"needs_full_report": False, "required_item_ids": ["invented"], "reason": "科目足夠。"},
            {"needs_full_report": False, "required_item_ids": [], "reason": "科目足夠。"},
            {"needs_full_report": "false", "required_item_ids": [self.item.item_id], "reason": "科目足夠。"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises((PipelineError, ValidationError)):
                self.judge_mock(payload)

    def test_refusal_remains_error(self):
        with self.assertRaises(PipelineError):
            self.judge_mock({}, refusal=True)

    def test_empty_question_makes_no_api_call(self):
        with self.assertRaises(ValueError):
            self.judge_mock({}, question="   ")
        self.assertEqual(self.requests, [])

    def test_router_only_requires_openai_key(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "offline-test", "TYPESAFE_API_KEY": ""}), \
                patch("database_routing.OpenAI") as client, \
                patch.object(DatabaseQuestionRouter, "run", return_value=False) as run:
            self.assertIs(needs_full_report("原題", self.catalog, self.config), False)
            self.assertEqual(client.call_count, 1)
            run.assert_called_once_with("原題", self.catalog, None)

    def test_cli_damaged_catalog_does_not_call_model(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / "bad.md"
            path.write_text("只有部分科目", encoding="utf-8")
            with patch("main.needs_full_report") as judge, \
                    redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                code = main.main(["原題", "--catalog", str(path)])
            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), "")
            judge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
