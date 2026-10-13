"""四組配對實驗：AB 共用前處理，CD 直接判斷原題。"""

import argparse
import hashlib
import json
import os
import random
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import TypeAdapter
from typesafe_sdk import RetryPolicy, TypeSafeClient

import prompts
from models import ExperimentConfig, ExperimentQuestion, FinancialReport, PipelineConfig
from pipeline import QuestionPipeline
from telemetry import CallRecorder, load_pricing

ROOT = Path(__file__).resolve().parent
GROUPS = "ABCD"


def digest(value) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def costs(calls: list[dict]) -> dict:
    known = sum(c["cost_usd"] for c in calls if c["cost_usd"] is not None)
    unknown = sum(c["cost_usd"] is None for c in calls)
    return {"known_cost_usd": known, "total_cost_usd": None if unknown else known,
            "unknown_cost_calls": unknown}


def decision_result(entry: dict) -> bool | None:
    """舊版 stopped/False 紀錄也不視為判斷器的布林答案。"""
    value = entry.get("result")
    return value if entry.get("outcome") == "completed" and type(value) is bool else None


def result_label(entry: dict) -> str:
    answer = decision_result(entry)
    if answer is not None:
        return str(answer)
    return entry.get("stop_reason", entry["outcome"]) if entry["outcome"] == "stopped" else entry["outcome"]


def run_paired(
    pipeline: QuestionPipeline, item: ExperimentQuestion, report: FinancialReport,
    config: ExperimentConfig, pricing: dict, *, repeat: int = 1,
    judge_order: str = GROUPS,
) -> dict:
    """每題每輪只前處理一次；各呼叫只記帳一次，部署試算另行分攤。"""
    if sorted(judge_order) != list(GROUPS):
        raise ValueError("judge_order 必須包含 A、B、C、D 各一次")
    if pipeline.recorder.calls:
        raise ValueError("每題每輪必須使用新的 CallRecorder")
    shared = pipeline.prepare_question(item.question)
    shared_ids = list(range(len(pipeline.recorder.calls)))
    shared["call_indices"] = shared_ids
    results = {}
    for group in judge_order:
        question = shared["question_used"] if group in "AB" else item.question
        entry = {"result": None, "outcome": "error", "question_used": question,
                 "question_sha256": digest(question) if question is not None else None,
                 "judge_call_indices": [],
                 "shared_call_indices": shared_ids if group in "AB" else []}
        results[group] = entry
        start = len(pipeline.recorder.calls)
        try:
            if group in "AC":
                answer = pipeline.is_answerable(question, report)
            else:
                answer = pipeline.is_answerable_openai(question, report, config.openai_judge)
            entry.update(result=answer, outcome="completed")
        except Exception as error:
            entry["error_type"] = type(error).__name__
        finally:
            entry["judge_call_indices"] = list(range(start, len(pipeline.recorder.calls)))
        entry["correct"] = (
            entry["result"] == item.gold_answerable
            if item.gold_answerable is not None and entry["result"] is not None else None
        )
    ledger = pipeline.recorder.summary(pricing)
    for entry in results.values():
        judge_calls = [ledger["calls"][i] for i in entry["judge_call_indices"]]
        deployment_calls = [ledger["calls"][i] for i in (
            entry["shared_call_indices"] + entry["judge_call_indices"]
        )]
        entry["judge_cost"] = costs(judge_calls)
        entry["standalone_cost_estimate"] = costs(deployment_calls)
        entry["api_latency_seconds"] = sum(c["latency_seconds"] for c in deployment_calls)
    return {
        "result_schema_version": 3, "pipeline_architecture": "optional_rewrite",
        "question_id": item.question_id, "original_question": item.question,
        "gold_answerable": item.gold_answerable, "repeat": repeat,
        "judge_order": list(judge_order), "report_sha256": digest(report.model_dump(mode="json")),
        "shared_preprocessing": shared, "groups": results, **ledger,
    }


def summarize(records: list[dict], group_names: str = GROUPS) -> dict:
    if not group_names or len(set(group_names)) != len(group_names):
        raise ValueError("彙總組別不得為空或重複")
    ledger = costs([call for record in records for call in record["calls"]])
    groups = {}
    for group in group_names:
        rows = [r["groups"][group] for r in records]
        pairs = [(r["gold_answerable"], decision_result(r["groups"][group])) for r in records
                 if r["gold_answerable"] is not None]
        valid = [(gold, predicted) for gold, predicted in pairs if predicted is not None]
        labeled_errors = sum(r["gold_answerable"] is not None and r["groups"][group]["outcome"] == "error"
                             for r in records)
        rejected = sum(result_label(row) == "rejected" for row in rows)
        cannot_rewrite = sum(result_label(row) == "cannot_rewrite" for row in rows)
        decisions = [decision_result(row) for row in rows]
        decision_count = sum(answer is not None for answer in decisions)
        tp = sum(gold and predicted for gold, predicted in valid)
        tn = sum(not gold and not predicted for gold, predicted in valid)
        fp = sum(not gold and predicted for gold, predicted in valid)
        fn = sum(gold and not predicted for gold, predicted in valid)
        def ratio(numerator, denominator):
            return numerator / denominator if denominator else None
        groups[group] = {
            "evaluations": len(rows), "errors": sum(r["outcome"] == "error" for r in rows),
            "stopped": rejected + cannot_rewrite,
            "rejected": rejected, "cannot_rewrite": cannot_rewrite,
            "true_count": sum(answer is True for answer in decisions),
            "false_count": sum(answer is False for answer in decisions),
            "decisions": decision_count, "no_decision": len(rows) - decision_count,
            "rewritten_inputs": sum(r["shared_preprocessing"]["outcome"] == "rewritten" for r in records) if group in "AB" else 0,
            "original_inputs": sum(r["shared_preprocessing"]["outcome"] == "original" for r in records) if group in "AB" else len(rows),
            "decision_coverage": ratio(decision_count, len(rows)),
            "rejection_rate": ratio(rejected, len(rows)),
            "labeled_evaluations": len(pairs), "valid_labeled_evaluations": len(valid),
            "labeled_decision_coverage": ratio(len(valid), len(pairs)),
            "accuracy": ratio(tp + tn, len(valid)),
            # rejected / cannot_rewrite 沒有路由決策，不列入分類分母。
            "accuracy_including_errors": ratio(tp + tn, len(valid) + labeled_errors),
            "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
            "f1": ratio(2 * tp, 2 * tp + fp + fn),
            "confusion_matrix": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
            "judge_cost": combine_costs([r["judge_cost"] for r in rows]),
            "standalone_cost_estimate": combine_costs([r["standalone_cost_estimate"] for r in rows]),
            "mean_api_latency_seconds": ratio(sum(r["api_latency_seconds"] for r in rows), len(rows)),
        }
    return {"result_schema_version": 3, "question_rounds": len(records), "group_evaluations": len(records) * len(group_names),
            "preprocessing_errors": sum(r["shared_preprocessing"].get("reason") == "preprocessing_error" for r in records),
            "actual_experiment_cost": ledger, "groups": groups}


def combine_costs(items: list[dict]) -> dict:
    known = sum(i["known_cost_usd"] for i in items)
    unknown = sum(i["unknown_cost_calls"] for i in items)
    return {"known_cost_usd": known, "total_cost_usd": None if unknown else known,
            "unknown_cost_calls": unknown}


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="四組實驗：AB 共用改寫，CD 直接判斷原題")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--question", help="只跑一題的原始問題")
    source.add_argument("--dataset", type=Path, help="ExperimentQuestion 陣列 JSON，可附人工 gold_answerable")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42, help="固定判斷器的隨機執行順序")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--experiment-config", type=Path, default=ROOT / "experiment_config.json")
    parser.add_argument("--report", type=Path, default=ROOT / "financial_report_fields.json")
    parser.add_argument("--pricing", type=Path, default=ROOT / "pricing.json")
    parser.add_argument("--output", type=Path, help="新建結果目錄；不覆寫既有紀錄")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat 必須至少為 1")
    load_dotenv(ROOT / ".env", override=False)
    try:
        config = PipelineConfig.model_validate_json(args.config.read_text(encoding="utf-8-sig"))
        experiment = ExperimentConfig.model_validate_json(args.experiment_config.read_text(encoding="utf-8-sig"))
        report = FinancialReport.model_validate_json(args.report.read_text(encoding="utf-8-sig"))
        pricing = load_pricing(args.pricing)
        items = (
            TypeAdapter(list[ExperimentQuestion]).validate_json(args.dataset.read_text(encoding="utf-8-sig"))
            if args.dataset else [ExperimentQuestion(question_id="q001", question=args.question)]
        )
        if not items or len({i.question_id for i in items}) != len(items):
            raise ValueError("資料集不得為空或有重複題目 ID")
        openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        typesafe_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not openai_key or not typesafe_key:
            raise ValueError("缺少 API 金鑰")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = args.output or ROOT / "test_results" / stamp
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / "manifest.json", {
            "result_schema_version": 3, "pipeline_architecture": "optional_rewrite",
            "result_semantics": "status 只有 rewrite/no_rewrite；無法改寫或前處理失敗時改用原題，仍嘗試最終判斷。只有 completed 的 result 是布林路由判斷，最終判斷失敗記為 error/null。",
            "created_at": stamp, "seed": args.seed, "repeat": args.repeat,
            "pipeline_config": config.model_dump(mode="json"),
            "experiment_config": experiment.model_dump(mode="json"),
            "financial_report": report.model_dump(mode="json"),
            "dataset": [i.model_dump(mode="json") for i in items],
            "pricing": pricing,
            "prompts": {name: value for name, value in vars(prompts).items()
                        if name.isupper() and isinstance(value, str)},
        })
        records = []
        rng = random.Random(args.seed)
        with ExitStack() as stack:
            # 不自動重試；所有可觀察請求只記一次，未知成本保留 null。
            openai = stack.enter_context(OpenAI(api_key=openai_key, timeout=config.timeout_seconds, max_retries=0))
            jev = stack.enter_context(TypeSafeClient(api_key=typesafe_key, timeout=config.timeout_seconds,
                                                   retry=RetryPolicy(max_retries=0)))
            for repeat in range(1, args.repeat + 1):
                for index, item in enumerate(items, 1):
                    order = list(GROUPS)
                    rng.shuffle(order)
                    pipeline = QuestionPipeline(config, openai, jev, CallRecorder())
                    record = run_paired(pipeline, item, report, experiment, pricing,
                                        repeat=repeat, judge_order="".join(order))
                    write_json(output / f"q{index:04d}_r{repeat}.json", record)
                    records.append(record)
                    write_json(output / "summary.json", summarize(records))
                    answers = " ".join(f"{g}={result_label(record['groups'][g])}" for g in GROUPS)
                    print(f"{item.question_id} 第{repeat}輪：{answers}")
        print(f"結果：{output.resolve()}")
        return 1 if any(r["groups"][g]["outcome"] == "error" for r in records for g in GROUPS) else 0
    except Exception as error:
        # SDK 錯誤與驗證訊息可能含敏感輸入，僅輸出類型。
        print(f"實驗未完成：{type(error).__name__}；請檢查設定、金鑰及輸出目錄。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
