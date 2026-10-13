"""專題 A 組入口：保護問題原意、Jev 分流、由外部 agent 查資料並回答。"""

import os
from collections.abc import Callable
from contextlib import ExitStack

from openai import OpenAI
from typesafe_sdk import RetryPolicy, TypeSafeClient

from database_catalog import DatabaseCatalog
from database_routing import JevDatabaseQuestionRouter
from models import (
    AgentAttempt, AgentRequest, AgentResponse, GroupAExecutionResult, GroupARoutingResult,
    PipelineConfig, PreparedQuestion,
)
from pipeline import QuestionPipeline
from telemetry import CallRecorder

AgentHandler = Callable[[AgentRequest], AgentResponse | dict]


class GroupARouter:
    """注入既有 client，供專題服務重用連線；每題各自記錄呼叫。"""

    def __init__(self, config: PipelineConfig, catalog: DatabaseCatalog,
                 openai_client: OpenAI, jev_client: TypeSafeClient) -> None:
        self.config = config
        self.catalog = catalog
        self.openai = openai_client
        self.jev = jev_client

    def route(self, question: str, recorder: CallRecorder | None = None) -> GroupARoutingResult:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("USER 原始問題不得為空")
        recorder = recorder if recorder is not None else CallRecorder()
        pipeline = QuestionPipeline(self.config, self.openai, self.jev, recorder)
        prepared = PreparedQuestion.model_validate({
            **pipeline.prepare_question(question, verify_rewrite=True), "original_question": question,
        })
        try:
            judgment = JevDatabaseQuestionRouter(self.config.step3, self.jev, recorder).judge(
                prepared.question_used, self.catalog, original_question=question,
            )
        except Exception as error:
            # 前處理錯誤可使用原題；最終服務錯誤必須停止 agent 調用。
            return GroupARoutingResult(
                status="error", original_question=question, preprocessing=prepared,
                error_type=type(error).__name__, error_stage="routing_judgment",
            )
        return GroupARoutingResult(
            status="completed", original_question=question, preprocessing=prepared,
            needs_full_report=judgment.needs_full_report,
            route="full_report" if judgment.needs_full_report else "database", judgment=judgment,
        )


def route_group_a(question: str, catalog: DatabaseCatalog, config: PipelineConfig,
                  recorder: CallRecorder | None = None) -> GroupARoutingResult:
    """單題便利入口；長期運行的服務可直接重用 GroupARouter。"""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("USER 原始問題不得為空")
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    typesafe_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not openai_key or not typesafe_key:
        raise ValueError("請設定 OPENAI_API_KEY 與 TYPESAFE_API_KEY")
    with ExitStack() as stack:
        openai = stack.enter_context(OpenAI(
            api_key=openai_key, timeout=config.timeout_seconds, max_retries=0,
        ))
        jev = stack.enter_context(TypeSafeClient(
            api_key=typesafe_key, timeout=config.timeout_seconds, retry=RetryPolicy(max_retries=0),
        ))
        return GroupARouter(config, catalog, openai, jev).route(question, recorder)


class AgentDispatcher:
    """只調用選中的 agent；資料不足最多轉交完整財報一次。"""

    def __init__(self, *, database_agent: AgentHandler | None = None,
                 full_report_agent: AgentHandler | None = None) -> None:
        self.handlers = {"database": database_agent, "full_report": full_report_agent}

    def dispatch(self, routing: GroupARoutingResult) -> GroupAExecutionResult:
        if routing.status == "error":
            return GroupAExecutionResult(routing=routing, status="routing_error", reason="分流服務失敗，未調用 agent。")
        route = routing.route
        attempts = []
        for _ in range(2):
            handler = self.handlers[route]
            if handler is None:
                return GroupAExecutionResult(
                    routing=routing, status="awaiting_agent", final_route=route, attempts=attempts,
                    reason=f"尚未接入 {route} agent。",
                )
            task = (
                "以原題為準，改寫題輔助理解。自行查核公司、期間、科目及明細資料；"
                "資料足夠才回答，缺少必要資料時回報 needs_full_report；無法取得的數值不可當成零。"
                if route == "database" else
                "以原題為準，改寫題輔助理解。查閱完整財報及附註回答；仍缺資料時回報 insufficient_data。"
            )
            request = AgentRequest(
                route=route, original_question=routing.original_question,
                question_used=routing.preprocessing.question_used, routing=routing, task=task,
                previous_attempts=list(attempts),
            )
            try:
                raw = handler(request)
                response = AgentResponse.model_validate(raw)
            except Exception as error:
                response = AgentResponse(status="error", reason="agent 調用或回傳格式失敗。",
                                         error_type=type(error).__name__)
            attempts.append(AgentAttempt(route=route, response=response))
            if response.status == "completed":
                return GroupAExecutionResult(
                    routing=routing, status="completed", final_route=route,
                    answer=response.answer, reason=response.reason, attempts=attempts,
                )
            if response.status == "error":
                return GroupAExecutionResult(routing=routing, status="agent_error", final_route=route,
                                             reason=response.reason, attempts=attempts)
            if route == "database":
                route = "full_report"
                continue
            return GroupAExecutionResult(routing=routing, status="insufficient_data", final_route=route,
                                         reason=response.reason, attempts=attempts)
        raise RuntimeError("agent 交接次數不正確")


class GroupASystem:
    def __init__(self, router: GroupARouter, dispatcher: AgentDispatcher) -> None:
        self.router = router
        self.dispatcher = dispatcher

    def run(self, question: str, recorder: CallRecorder | None = None) -> GroupAExecutionResult:
        return self.dispatcher.dispatch(self.router.route(question, recorder))
