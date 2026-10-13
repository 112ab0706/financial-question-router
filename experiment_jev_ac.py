"""重用保存題目，僅呼叫 Jev 比較 A/C；前處理及 B 不重跑。"""
import argparse
import hashlib
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from typesafe_sdk import RetryPolicy, TypeSafeClient

import prompts
from database_catalog import load_catalog
from database_routing import JevDatabaseQuestionRouter
from legacy_experiments import costs, digest, result_label, summarize as numeric_summary, write_json
from models import PipelineConfig
from telemetry import CallRecorder, load_pricing

ROOT = Path(__file__).resolve().parent
GROUPS = "AC"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_saved(source: Path, catalog, gold_path: Path | None = None) -> tuple[list[dict], dict]:
    paths = sorted(source.glob("q*_r1.json"))
    if not paths:
        raise ValueError("來源目錄沒有第一輪逐題紀錄")
    gold_data = read_json(gold_path) if gold_path else None
    gold = {}
    if gold_data is not None:
        if gold_data.get("bool_semantics") != "needs_full_report":
            raise ValueError("人工答案必須以 needs_full_report 為含義")
        for row in gold_data.get("labels", []):
            qid, value = row["question_id"], row["gold_needs_full_report"]
            if type(value) is not bool or qid in gold:
                raise ValueError("人工答案必須是布林值，題號不可重複")
            gold[qid] = value
    items = []
    seen = set()
    for path in paths:
        record = read_json(path)
        qid = record["question_id"]
        if (record.get("bool_semantics") != "needs_full_report" or qid in seen
                or record.get("catalog_sha256") != catalog.source_sha256):
            raise ValueError("來源含義、題號或科目快照不相符")
        selected, original = record["shared_preprocessing"]["question_used"], record["original_question"]
        if (not isinstance(selected, str) or not selected.strip()
                or record["groups"]["A"]["question_used"] != selected
                or record["groups"]["B"]["question_used"] != selected
                or record["groups"]["C"]["question_used"] != original):
            raise ValueError("來源 A/B/C 題目配對不完整")
        source_hash = sha256(path)
        if gold_data is not None:
            expected = gold_data.get("source_record_sha256", {}).get(path.name)
            if expected is not None and expected != source_hash:
                raise ValueError("來源逐題紀錄與人工答案的來源雜湊不相符")
        seen.add(qid)
        items.append({
            "question_id": qid, "original_question": original, "selected_question": selected,
            "gold_needs_full_report": gold.get(qid), "source_file": path.name,
            "source_record_sha256": source_hash, "source_record": record,
        })
    if gold_data is not None and set(gold) != seen:
        raise ValueError("人工答案必須完整對應來源題號")
    return items, {
        "directory": str(source.resolve()),
        "record_sha256": {i["source_file"]: i["source_record_sha256"] for i in items},
        "gold_file": str(gold_path.resolve()) if gold_path else None,
        "gold_sha256": sha256(gold_path) if gold_path else None,
    }


def run_saved(item: dict, catalog, router: JevDatabaseQuestionRouter, pricing: dict,
              order: str = GROUPS) -> dict:
    if sorted(order) != list(GROUPS) or router.recorder.calls:
        raise ValueError("每題使用新的紀錄器，且順序必須含 A/C 各一次")
    entries = {}
    for group in order:
        question = item["selected_question"] if group == "A" else item["original_question"]
        start = len(router.recorder.calls)
        entry = {
            "question_used": question, "question_sha256": digest(question), "result": None,
            "outcome": "error", "correct": None, "shared_call_indices": [],
            "judge_call_indices": [], "input_origin": "saved_preprocessed" if group == "A" else "original",
        }
        entries[group] = entry
        try:
            entry.update(result=router.run(question, catalog), outcome="completed")
        except Exception as error:
            entry["error_type"] = type(error).__name__
        entry["judge_call_indices"] = list(range(start, len(router.recorder.calls)))
        if entry["result"] is not None and item["gold_needs_full_report"] is not None:
            entry["correct"] = entry["result"] == item["gold_needs_full_report"]
    ledger = router.recorder.summary(pricing)
    for entry in entries.values():
        calls = [ledger["calls"][i] for i in entry["judge_call_indices"]]
        entry.update(judge_cost=costs(calls), standalone_cost_estimate=costs(calls),
                     cost_scope="new_jev_judgment_only", api_latency_seconds=sum(c["latency_seconds"] for c in calls))
    return {
        "result_schema_version": 5, "bool_semantics": "needs_full_report",
        "pipeline_architecture": "saved_preprocessing_jev_ac", "repeat": 1,
        "routing_judgment_version": prompts.JEV_ROUTING_VERSION,
        "question_id": item["question_id"], "original_question": item["original_question"],
        "gold_needs_full_report": item["gold_needs_full_report"], "judge_order": list(order),
        "catalog_sha256": catalog.source_sha256, "data_availability": "unchecked",
        "source_file": item["source_file"], "source_record_sha256": item["source_record_sha256"],
        "shared_preprocessing": {
            "outcome": item["source_record"]["shared_preprocessing"]["outcome"],
            "question_used": item["selected_question"], "reason": item["source_record"]["shared_preprocessing"].get("reason"),
            "reused": True, "new_call_indices": [], "new_cost_usd": 0,
        },
        "groups": entries, **ledger,
    }


def summarize(records: list[dict]) -> dict:
    summary = numeric_summary([{**r, "gold_answerable": r["gold_needs_full_report"]} for r in records], GROUPS)
    summary.update(result_schema_version=5, bool_semantics="needs_full_report", positive_class="需要完整財報",
                   routing_judgment_version=prompts.JEV_ROUTING_VERSION, cost_scope="new_jev_judgment_only",
                   preprocessing_reused=True, new_preprocessing_cost_usd=0, data_availability="unchecked")
    for group, metrics in summary["groups"].items():
        for key in ("stopped", "rejected", "cannot_rewrite", "rejection_rate"):
            metrics.pop(key, None)
        metrics["incorrect_question_ids"] = [r["question_id"] for r in records if r["groups"][group]["correct"] is False]
    return summary


def save_report(output: Path, records: list[dict], items: list[dict]) -> None:
    summary = summarize(records)
    old_records = [{**i["source_record"], "gold_answerable": i["gold_needs_full_report"]} for i in items]
    previous = numeric_summary(old_records, GROUPS)
    lines = ["# Jev 三項判斷：重用題目的 AC 實驗", "",
             "True＝需要完整財報；False＝資料庫科目與粒度足夠。", "",
             "A 逐字重用上一輪保存的選定題目；C 逐字使用上一輪原題。每題每組一輪，只呼叫 Jev。", "",
             "每次 Jev 同時判斷缺必要科目、缺必要明細、需要科目以外資訊，任一機率 ≥ 0.5 即為 True。",
             "英文指令與較短的能力範圍說明；完整科目涵義、報表分類及歧義元素保留，不使用候選篩選。",
             "來源快照的公司／期間未查核訊息不傳入能力判斷；原始來源資料與雜湊保存在 manifest。", "",
             "## 評分與新費用", "",
             "| 組別 | 舊正確率 | 新正確率 | 新正確題數 | TP | TN | FP | FN | 本輪 Jev 成本（美元） |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    def percent(value):
        return "未評分" if value is None else f"{value:.1%}"
    def price(value):
        return "未知" if value is None else f"US${value:.6f}"
    for group in GROUPS:
        m, old = summary["groups"][group], previous["groups"][group]
        cm = m["confusion_matrix"]
        lines.append(f"| {group} | {percent(old['accuracy'])} | {percent(m['accuracy'])} | {cm['tp'] + cm['tn']}/{m['valid_labeled_evaluations']} | {cm['tp']} | {cm['tn']} | {cm['fp']} | {cm['fn']} | {price(m['judge_cost']['total_cost_usd'])} |")
    lines.extend(["", f"本輪實際估計費用：{price(summary['actual_experiment_cost']['total_cost_usd'])}；前處理及 OpenAI 新費用均為 US$0。",
                  "本輪成本只包含新 Jev 判斷，未重複收錄上一輪前處理或模型呼叫費用；不等於部署完整 A 流程的成本。",
                  "成本按 API usage 與費率快照估算，未知費用不當成免費。", "",
                  "## 逐題判斷", "", "| 題號 | 人工答案 | 舊 A | 新 A | 舊 C | 新 C |",
                  "| --- | --- | --- | --- | --- | --- |"])
    old_by_id = {i["question_id"]: i["source_record"] for i in items}
    for record in records:
        old = old_by_id[record["question_id"]]
        lines.append("| " + " | ".join([record["question_id"], str(record["gold_needs_full_report"]),
            result_label(old["groups"]["A"]), result_label(record["groups"]["A"]),
            result_label(old["groups"]["C"]), result_label(record["groups"]["C"])]) + " |")
    lines.extend(["", "## 誤判與來源", ""])
    for group in GROUPS:
        m = summary["groups"][group]
        ids = "、".join(str(int(q[1:])) for q in m["incorrect_question_ids"]) or "無"
        lines.append(f"- {group} 誤判題號：{ids}；API／回傳錯誤 {m['errors']} 筆。")
    lines.extend(["", "逐題 JSON 保存三項機率、布林旗標、用量、費率、題目及來源雜湊；沒有合成整體機率。",
                  "人工答案只在呼叫完成後評分，未傳入 Jev。原本題目、前處理、標註與模型結果未修改。",
                  "本輪同時調整指令語言、拆分判斷與能力說明，不能只將差異歸因於其中一項。",
                  "這 20 題是已用於檢視問題的開發資料，每組一輪；本輪結果不代表未見題目的正確率。", ""])
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重用已保存題目，只呼叫 Jev 比較 A/C")
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--catalog", type=Path, default=ROOT / "database/2025q4_accounting_items.md")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--pricing", type=Path, default=ROOT / "pricing.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    try:
        catalog = load_catalog(args.catalog)
        config = PipelineConfig.model_validate_json(args.config.read_text(encoding="utf-8-sig"))
        pricing = load_pricing(args.pricing)
        items, source = load_saved(args.source_run, catalog, args.gold)
        load_dotenv(ROOT / ".env", override=False)
        key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not key:
            raise ValueError("缺少 TYPESAFE_API_KEY")
        args.output.mkdir(parents=True, exist_ok=False)
        write_json(args.output / "manifest.json", {
            "result_schema_version": 5, "bool_semantics": "needs_full_report", "groups": list(GROUPS),
            "source": source, "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "preprocessing_reused": True, "new_openai_calls": 0, "new_preprocessing_calls": 0,
            "seed": args.seed, "repeat": 1, "jev_config": config.step3.model_dump(),
            "routing_judgment_version": prompts.JEV_ROUTING_VERSION,
            "scope_policy": prompts.JEV_ROUTING_SCOPE, "checks": prompts.JEV_ROUTING_CHECKS,
            "combination_rule": "any(p >= threshold)", "data_availability": "unchecked",
            "database_catalog": catalog.to_state(),
            "judgment_catalog": JevDatabaseQuestionRouter.build_state("", catalog)["database_catalog"],
            "dataset": [{k: v for k, v in i.items() if k != "source_record"} for i in items], "pricing": pricing,
        })
        records = []
        rng = random.Random(args.seed)
        with TypeSafeClient(api_key=key, timeout=config.timeout_seconds, retry=RetryPolicy(max_retries=0)) as client:
            for index, item in enumerate(items, 1):
                order = list(GROUPS)
                rng.shuffle(order)
                router = JevDatabaseQuestionRouter(config.step3, client, CallRecorder())
                record = run_saved(item, catalog, router, pricing, "".join(order))
                write_json(args.output / f"q{index:04d}_r1.json", record)
                records.append(record)
                write_json(args.output / "summary.json", summarize(records))
                print(f"{item['question_id']} " + " ".join(f"{g}={result_label(record['groups'][g])}" for g in GROUPS), flush=True)
        for item in items:
            if sha256(args.source_run / item["source_file"]) != item["source_record_sha256"]:
                raise ValueError("來源紀錄在執行期間被修改")
        save_report(args.output, records, items)
        print(json.dumps({"output": str(args.output.resolve()), "actual_experiment_cost": summarize(records)["actual_experiment_cost"]}, ensure_ascii=False))
        return 1 if any(r["groups"][g]["outcome"] == "error" for r in records for g in GROUPS) else 0
    except Exception as error:
        print(f"AC 實驗未完成：{type(error).__name__}；請檢查來源、人工答案、科目快照、金鑰及輸出目錄。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
