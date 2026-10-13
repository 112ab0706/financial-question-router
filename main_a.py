"""A 組正式單題入口；輸出結構化分流 JSON，其他 agent 由 Python 介面接入。"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from database_catalog import load_catalog
from group_a import route_group_a
from models import GroupAInput, PipelineConfig
from telemetry import CallRecorder, load_pricing

ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A 組：保護改寫原意後交由 Jev 分流，輸出 JSON")
    parser.add_argument("question", nargs="?", help="USER 原始問題")
    parser.add_argument("--latest-year", type=int, help="前端提供的最新資料年度（西元整數）")
    parser.add_argument("--current-company", help="前端提供的當前公司名稱或代號")
    parser.add_argument("--input-json", type=Path, help="含 original_question/latest_year/current_company 的前端 JSON 檔")
    parser.add_argument("--catalog", type=Path, default=ROOT / "database/2025q4_accounting_items.md")
    parser.add_argument("--config", type=Path, default=ROOT / "group_a_config.json")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--pricing", type=Path, default=ROOT / "pricing.json")
    args = parser.parse_args(argv)
    if args.input_json is not None:
        if args.question is not None or args.latest_year is not None or args.current_company is not None:
            parser.error("--input-json 不能與題目、--latest-year 或 --current-company 同時使用")
    elif args.question is None:
        parser.error("請提供原始問題或 --input-json")
    load_dotenv(ROOT / ".env", override=False)
    recorder = CallRecorder()
    try:
        question = (GroupAInput.model_validate_json(args.input_json.read_text(encoding="utf-8-sig"))
                    if args.input_json is not None else
                    GroupAInput(original_question=args.question, latest_year=args.latest_year,
                                current_company=args.current_company))
        config = PipelineConfig.model_validate_json(args.config.read_text(encoding="utf-8-sig"))
        catalog = load_catalog(args.catalog)
        pricing = load_pricing(args.pricing) if args.log else None
        result = route_group_a(question, catalog, config, recorder)
        if args.log:
            recorder.save(args.log, pricing, **result.model_dump(mode="json"))
    except Exception as error:
        print(f"A 組未完成：{type(error).__name__}；請檢查設定、金鑰、科目清單及輸出路徑。", file=sys.stderr)
        return 1
    print(result.model_dump_json(indent=2))
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
