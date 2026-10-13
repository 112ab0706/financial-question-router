"""命令列成功時只輸出 True / False，錯誤輸出至 stderr 並以 1 結束。"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import APIError
from pydantic import ValidationError
from typesafe_sdk import TypeSafeError

from database_catalog import load_catalog
from database_routing import needs_full_report
from models import RoutingConfig
from pipeline import PipelineError
from telemetry import CallRecorder, load_pricing

ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="依資料庫科目判斷原題是否需要完整財報；True=需要，False=資料庫足夠")
    parser.add_argument("question", help="USER 的原始問題")
    parser.add_argument(
        "--catalog", type=Path, default=ROOT / "database" / "2025q4_accounting_items.md",
        help="資料庫科目清單 Markdown；預設為提供的2025Q4清單快照",
    )
    parser.add_argument("--config", type=Path, default=ROOT / "routing_config.json")
    parser.add_argument("--log", type=Path, help="另存各步 usage、延遲與美元成本 JSON；stdout 仍只有 bool")
    parser.add_argument("--pricing", type=Path, default=ROOT / "pricing.json")
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    recorder = CallRecorder() if args.log else None
    pricing = None
    result = None
    error_type = None
    try:
        if recorder is not None:
            pricing = load_pricing(args.pricing)
        config = RoutingConfig.model_validate_json(args.config.read_text(encoding="utf-8-sig"))
        catalog = load_catalog(args.catalog)
        if recorder is None:
            result = needs_full_report(args.question, catalog, config)
        else:
            result = needs_full_report(args.question, catalog, config, recorder)
    except (OSError, ValueError, ValidationError, PipelineError, APIError, TypeSafeError) as error:
        error_type = type(error).__name__
        # 不輸出 SDK response body，避免意外洩漏輸入資料或認證資訊。
        if isinstance(error, (APIError, TypeSafeError)):
            status = getattr(error, "status_code", None)
            if status is None:
                status = getattr(error, "status", None)
            message = f"模型服務呼叫失敗：{type(error).__name__}"
            if status is not None:
                message += f"（HTTP {status}）"
        elif isinstance(error, ValidationError):
            message = "設定、科目清單或模型回傳格式不正確，請檢查欄位及資料型別"
        else:
            message = str(error)
        # API key 不應出現在任何錯誤文字中。
        for name in ("OPENAI_API_KEY", "TYPESAFE_API_KEY"):
            secret = os.environ.get(name, "").strip()
            if secret:
                message = message.replace(secret, "[REDACTED]")
        print(f"錯誤：{message}", file=sys.stderr)
        return 1
    finally:
        if recorder is not None and pricing is not None:
            try:
                recorder.save(
                    args.log, pricing, question=args.question, result=result, error_type=error_type,
                    bool_semantics="needs_full_report", pipeline_architecture="original_database_routing",
                )
            except OSError:
                print("錯誤：無法寫入成本紀錄，請檢查 --log 路徑與權限", file=sys.stderr)
                return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
