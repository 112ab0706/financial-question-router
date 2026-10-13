"""A 組正式單題入口；輸出結構化分流 JSON，其他 agent 由 Python 介面接入。"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from database_catalog import load_catalog
from group_a import route_group_a
from models import PipelineConfig
from telemetry import CallRecorder, load_pricing

ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A 組：保護改寫原意後交由 Jev 分流，輸出 JSON")
    parser.add_argument("question", help="USER 原始問題")
    parser.add_argument("--catalog", type=Path, default=ROOT / "database/2025q4_accounting_items.md")
    parser.add_argument("--config", type=Path, default=ROOT / "group_a_config.json")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--pricing", type=Path, default=ROOT / "pricing.json")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    recorder = CallRecorder()
    try:
        config = PipelineConfig.model_validate_json(args.config.read_text(encoding="utf-8-sig"))
        catalog = load_catalog(args.catalog)
        pricing = load_pricing(args.pricing) if args.log else None
        result = route_group_a(args.question, catalog, config, recorder)
        if args.log:
            recorder.save(args.log, pricing, **result.model_dump(mode="json"))
    except Exception as error:
        print(f"A 組未完成：{type(error).__name__}；請檢查設定、金鑰、科目清單及輸出路徑。", file=sys.stderr)
        return 1
    print(result.model_dump_json(indent=2))
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
