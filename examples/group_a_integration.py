"""由專題系統傳入兩個 agent 函式；本檔不開啟或查詢 SQLite。"""

from pathlib import Path

from dotenv import load_dotenv

from database_catalog import load_catalog
from group_a import AgentDispatcher, AgentHandler, route_group_a
from models import GroupAExecutionResult, PipelineConfig
from telemetry import CallRecorder

ROOT = Path(__file__).resolve().parents[1]


def run_question(question: str, *, database_agent: AgentHandler, full_report_agent: AgentHandler,
                 config_path: Path = ROOT / "group_a_config.json",
                 catalog_path: Path = ROOT / "database/2025q4_accounting_items.md",
                 recorder: CallRecorder | None = None) -> GroupAExecutionResult:
    """agent 函式接收 AgentRequest，回傳 AgentResponse 或相同結構的 dict。"""
    load_dotenv(ROOT / ".env", override=False)
    config = PipelineConfig.model_validate_json(config_path.read_text(encoding="utf-8-sig"))
    catalog = load_catalog(catalog_path)
    routing = route_group_a(question, catalog, config, recorder)
    return AgentDispatcher(database_agent=database_agent, full_report_agent=full_report_agent).dispatch(routing)
