"""單獨執行 E 組：原題 → GPT-6 Luna；不呼叫前處理或 Jev。"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import TypeAdapter

import prompts
from legacy_experiments import costs, digest, summarize, write_json
from models import ExperimentConfig, ExperimentQuestion, FinancialReport, PipelineConfig
from pipeline import QuestionPipeline
from telemetry import CallRecorder, load_pricing

ROOT = Path(__file__).resolve().parent


def run_one(pipeline, item, report, judge, pricing) -> dict:
    if pipeline.recorder.calls:
        raise ValueError("每題必須使用新的 CallRecorder")
    entry = {"result": None, "outcome": "error", "question_used": item.question,
             "question_sha256": digest(item.question), "shared_call_indices": []}
    try:
        entry.update(result=pipeline.is_answerable_openai(item.question, report, judge), outcome="completed")
    except Exception as error:
        entry["error_type"] = type(error).__name__
    ledger = pipeline.recorder.summary(pricing)
    entry.update(
        judge_call_indices=list(range(len(ledger["calls"]))),
        judge_cost=costs(ledger["calls"]), standalone_cost_estimate=costs(ledger["calls"]),
        api_latency_seconds=sum(c["latency_seconds"] for c in ledger["calls"]),
        correct=entry["result"] == item.gold_answerable
        if entry["result"] is not None and item.gold_answerable is not None else None,
    )
    return {
        "result_schema_version": 3, "pipeline_architecture": "original_openai",
        "question_id": item.question_id, "original_question": item.question,
        "gold_answerable": item.gold_answerable, "repeat": 1,
        "report_sha256": digest(report.model_dump(mode="json")),
        "shared_preprocessing": {"outcome": "original", "reason": "direct_judgment", "call_indices": []},
        "groups": {"E": entry}, **ledger,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只執行 E 組：原題 → GPT-6 Luna")
    parser.add_argument("--dataset", type=Path, default=ROOT / "examples/experiment_questions_20_en_labeled.json")
    parser.add_argument("--judge-config", type=Path, default=ROOT / "experiment_config_E.json")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--report", type=Path, default=ROOT / "financial_report_fields.json")
    parser.add_argument("--pricing", type=Path, default=ROOT / "pricing.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    try:
        config = PipelineConfig.model_validate_json(args.config.read_text(encoding="utf-8-sig"))
        experiment = ExperimentConfig.model_validate_json(args.judge_config.read_text(encoding="utf-8-sig"))
        report = FinancialReport.model_validate_json(args.report.read_text(encoding="utf-8-sig"))
        items = TypeAdapter(list[ExperimentQuestion]).validate_json(args.dataset.read_text(encoding="utf-8-sig"))
        pricing = load_pricing(args.pricing)
        if not items or len({i.question_id for i in items}) != len(items):
            raise ValueError("題目不得為空或有重複 ID")
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("缺少 OPENAI_API_KEY")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = args.output or ROOT / "test_results" / f"E_{stamp}"
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / "manifest.json", {
            "result_schema_version": 3, "pipeline_architecture": "original_openai", "group": "E",
            "created_at": stamp, "repeat": 1, "judge_config": experiment.model_dump(mode="json"),
            "timeout_seconds": config.timeout_seconds, "financial_report": report.model_dump(mode="json"),
            "dataset": [i.model_dump(mode="json") for i in items], "pricing": pricing,
            "system_prompt": prompts.ANSWERABILITY_SYSTEM_PROMPT, "task": prompts.ANSWERABILITY_TASK,
        })
        records = []
        with OpenAI(api_key=api_key, timeout=config.timeout_seconds, max_retries=0) as client:
            for index, item in enumerate(items, 1):
                pipeline = QuestionPipeline(config, client, None, CallRecorder())
                record = run_one(pipeline, item, report, experiment.openai_judge, pricing)
                write_json(output / f"q{index:04d}_r1.json", record)
                records.append(record)
                write_json(output / "summary.json", summarize(records, "E"))
                entry = record["groups"]["E"]
                print(f"{item.question_id} E={entry['result'] if entry['outcome']=='completed' else 'error'}")
        print(f"結果：{output.resolve()}")
        return 1 if any(r["groups"]["E"]["outcome"] == "error" for r in records) else 0
    except Exception as error:
        print(f"E 組未完成：{type(error).__name__}；請檢查設定、金鑰及輸出目錄。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
