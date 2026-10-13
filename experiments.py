"""ABC 資料庫分流實驗：AB 共用前處理，C 直接判斷原題。"""

import argparse
import os
import random
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import TypeAdapter, ValidationError
from typesafe_sdk import RetryPolicy, TypeSafeClient

import prompts
from database_catalog import DatabaseCatalog, load_catalog
from database_routing import DatabaseQuestionRouter, JevDatabaseQuestionRouter
from legacy_experiments import costs, digest, result_label, summarize as legacy_summarize, write_json
from models import ExperimentConfig, PipelineConfig, RoutingConfig, RoutingExperimentQuestion
from pipeline import QuestionPipeline
from telemetry import CallRecorder, load_pricing

ROOT = Path(__file__).resolve().parent
GROUPS = "ABC"
BOOL_SEMANTICS = "needs_full_report"
ARCHITECTURE = "optional_rewrite_database_routing"


def run_paired(
    pipeline: QuestionPipeline, item: RoutingExperimentQuestion, catalog: DatabaseCatalog,
    config: ExperimentConfig, pricing: dict, *, repeat: int = 1,
    judge_order: str = GROUPS,
) -> dict:
    """同題三組；共用前處理只記帳一次，金標不傳給任何模型。"""
    if sorted(judge_order) != list(GROUPS):
        raise ValueError("judge_order 必須包含 A、B、C 各一次")
    if pipeline.recorder.calls:
        raise ValueError("每題每輪必須使用新的 CallRecorder")
    if pipeline.jev is None:
        raise ValueError("A/C 必須設定 Jev client")
    shared = pipeline.prepare_question(item.question)
    shared_ids = list(range(len(pipeline.recorder.calls)))
    shared["call_indices"] = shared_ids
    jev_router = JevDatabaseQuestionRouter(pipeline.config.step3, pipeline.jev, pipeline.recorder)
    openai_router = DatabaseQuestionRouter(
        RoutingConfig(judge=config.openai_judge, timeout_seconds=pipeline.config.timeout_seconds),
        pipeline.openai, pipeline.recorder,
    )
    results = {}
    for group in judge_order:
        question = shared["question_used"] if group in "AB" else item.question
        entry = {
            "result": None, "outcome": "error", "question_used": question,
            "question_sha256": digest(question), "bool_semantics": BOOL_SEMANTICS,
            "judge_call_indices": [], "shared_call_indices": shared_ids if group in "AB" else [],
        }
        results[group] = entry
        start = len(pipeline.recorder.calls)
        try:
            if group in "AC":
                answer = jev_router.run(question, catalog)
            else:
                detail = openai_router.judge(question, catalog)
                answer = detail.needs_full_report
                entry["routing_detail"] = detail.model_dump(mode="json")
            entry.update(result=answer, outcome="completed")
        except Exception as error:
            # API / 模型錯誤不是 False，也不影響其他組繼續判斷。
            entry["error_type"] = type(error).__name__
        finally:
            entry["judge_call_indices"] = list(range(start, len(pipeline.recorder.calls)))
        entry["correct"] = (
            entry["result"] == item.gold_needs_full_report
            if item.gold_needs_full_report is not None and entry["result"] is not None else None
        )
    ledger = pipeline.recorder.summary(pricing)
    for entry in results.values():
        judge_calls = [ledger["calls"][i] for i in entry["judge_call_indices"]]
        deployment_calls = [ledger["calls"][i] for i in entry["shared_call_indices"] + entry["judge_call_indices"]]
        entry.update(
            judge_cost=costs(judge_calls), standalone_cost_estimate=costs(deployment_calls),
            api_latency_seconds=sum(c["latency_seconds"] for c in deployment_calls),
        )
    return {
        "result_schema_version": 4, "pipeline_architecture": ARCHITECTURE,
        "bool_semantics": BOOL_SEMANTICS, "data_availability": "unchecked",
        "catalog_representation": {
            "A": "all_items_labels_without_ids_scope_only_v2", "B": "all_items_labels_with_disambiguation_v1",
            "C": "all_items_labels_without_ids_scope_only_v2",
        },
        "routing_judgment_version": {"A": prompts.JEV_ROUTING_VERSION, "B": "openai_database_routing_v1", "C": prompts.JEV_ROUTING_VERSION},
        "question_id": item.question_id, "original_question": item.question,
        "gold_needs_full_report": item.gold_needs_full_report, "repeat": repeat,
        "judge_order": list(judge_order), "catalog_sha256": catalog.source_sha256,
        "shared_preprocessing": shared, "groups": results, **ledger,
    }


def summarize(records: list[dict]) -> dict:
    if any(record.get("bool_semantics") != BOOL_SEMANTICS for record in records):
        raise ValueError("不能混用舊版 answerable 與新版 needs_full_report 結果")
    # 只重用數值統計；顯式對應新金標，不改動原始結果或舊人工標籤。
    numeric_records = [{**r, "gold_answerable": r["gold_needs_full_report"]} for r in records]
    summary = legacy_summarize(numeric_records, GROUPS)
    summary.update(
        result_schema_version=4, bool_semantics=BOOL_SEMANTICS,
        pipeline_architecture=ARCHITECTURE, data_availability="unchecked",
        positive_class="需要完整財報", gold_field="gold_needs_full_report",
    )
    for metrics in summary["groups"].values():
        for key in ("stopped", "rejected", "cannot_rewrite", "rejection_rate"):
            metrics.pop(key, None)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ABC 資料庫分流：True=需要完整財報，False=資料庫科目足夠")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--question", help="只跑一題原始問題")
    source.add_argument("--dataset", type=Path, help="JSON 陣列；人工答案欄位為 gold_needs_full_report")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--experiment-config", type=Path, default=ROOT / "experiment_config.json")
    parser.add_argument("--catalog", type=Path, default=ROOT / "database/2025q4_accounting_items.md")
    parser.add_argument("--pricing", type=Path, default=ROOT / "pricing.json")
    parser.add_argument("--output", type=Path, help="新建目錄，不覆寫既有結果")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat 必須至少為 1")
    load_dotenv(ROOT / ".env", override=False)
    try:
        config = PipelineConfig.model_validate_json(args.config.read_text(encoding="utf-8-sig"))
        experiment = ExperimentConfig.model_validate_json(args.experiment_config.read_text(encoding="utf-8-sig"))
        catalog = load_catalog(args.catalog)
        pricing = load_pricing(args.pricing)
        items = (
            TypeAdapter(list[RoutingExperimentQuestion]).validate_json(args.dataset.read_text(encoding="utf-8-sig"))
            if args.dataset else [RoutingExperimentQuestion(question_id="q001", question=args.question)]
        )
        if not items or len({i.question_id for i in items}) != len(items):
            raise ValueError("資料集不得為空或有重複題目 ID")
        openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        typesafe_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not openai_key or not typesafe_key:
            raise ValueError("缺少 API 金鑰")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = args.output or ROOT / "test_results" / f"ABC_database_{stamp}"
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / "manifest.json", {
            "result_schema_version": 4, "pipeline_architecture": ARCHITECTURE,
            "bool_semantics": BOOL_SEMANTICS, "groups": list(GROUPS), "data_availability": "unchecked",
            "result_semantics": "True=需要完整財報；False=資料庫科目及粒度足夠。最終錯誤為 error/null；前處理失敗使用原題繼續。",
            "created_at": stamp, "repeat": args.repeat, "seed": args.seed,
            "pipeline_config": config.model_dump(mode="json"), "experiment_config": experiment.model_dump(mode="json"),
            "database_catalog": catalog.to_state(), "judgment_catalog": catalog.to_judgment_state(),
            "jev_judgment_catalog": JevDatabaseQuestionRouter.build_state("", catalog)["database_catalog"],
            "jev_scope_policy": prompts.JEV_ROUTING_SCOPE, "jev_checks": prompts.JEV_ROUTING_CHECKS,
            "jev_routing_judgment_version": prompts.JEV_ROUTING_VERSION,
            "dataset": [i.model_dump(mode="json") for i in items],
            "pricing": pricing, "prompts": {k: v for k, v in vars(prompts).items() if k.isupper() and isinstance(v, str)},
        })
        records = []
        rng = random.Random(args.seed)
        with ExitStack() as stack:
            openai = stack.enter_context(OpenAI(api_key=openai_key, timeout=config.timeout_seconds, max_retries=0))
            jev = stack.enter_context(TypeSafeClient(api_key=typesafe_key, timeout=config.timeout_seconds,
                                                   retry=RetryPolicy(max_retries=0)))
            for repeat in range(1, args.repeat + 1):
                for index, item in enumerate(items, 1):
                    order = list(GROUPS)
                    rng.shuffle(order)
                    pipeline = QuestionPipeline(config, openai, jev, CallRecorder())
                    record = run_paired(pipeline, item, catalog, experiment, pricing,
                                        repeat=repeat, judge_order="".join(order))
                    write_json(output / f"q{index:04d}_r{repeat}.json", record)
                    records.append(record)
                    write_json(output / "summary.json", summarize(records))
                    answers = " ".join(f"{g}={result_label(record['groups'][g])}" for g in GROUPS)
                    print(f"{item.question_id} 第{repeat}輪：{answers}")
        print(f"結果：{output.resolve()}")
        return 1 if any(r["groups"][g]["outcome"] == "error" for r in records for g in GROUPS) else 0
    except Exception as error:
        print(f"實驗未完成：{type(error).__name__}；請檢查設定、金鑰、科目清單及輸出目錄。", file=sys.stderr)
        if isinstance(error, ValidationError):
            print("新版資料集請使用 gold_needs_full_report，不接受舊版 gold_answerable。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
