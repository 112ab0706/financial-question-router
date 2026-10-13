"""模型輸出、設定與財報輸入的資料格式。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class QuestionTemplate(StrictModel):
    Time: str | None
    Concept: str | None
    Filter: str | None
    Logic: str | None


class AnswerabilityResult(StrictModel):
    answerable: bool


class ExperimentQuestion(StrictModel):
    question_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    gold_answerable: bool | None = None

    @model_validator(mode="after")
    def nonblank_question(self) -> Self:
        if not self.question_id.strip() or not self.question.strip():
            raise ValueError("題目 ID 與原題不得空白")
        return self


class StatusResult(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    status: Literal["rewrite", "no_rewrite"]
    reason: str = Field(alias="Context_Gap")

    @model_validator(mode="after")
    def nonblank_reason(self) -> Self:
        if not self.reason.strip():
            raise ValueError("Context_Gap 必須說明 status 的判斷依據")
        return self


class RewriteResult(StrictModel):
    # null 表示無法忠實改寫；流程會改用原題繼續最終判斷。
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    rewritten_question: str | None
    reason: str = Field(alias="Context_Gap")

    @property
    def can_rewrite(self) -> bool:
        return self.rewritten_question is not None

    @model_validator(mode="after")
    def consistent_result(self) -> Self:
        if not self.reason.strip():
            raise ValueError("Context_Gap 必須說明改寫調整或失敗原因")
        if self.can_rewrite:
            if not self.rewritten_question or not self.rewritten_question.strip():
                raise ValueError("改寫問題必須為非空的中文問題或 null")
            if any(marker in self.rewritten_question for marker in (
                "{Time:", "{Concept:", "{Filter:", "{Logic:",
            )):
                raise ValueError("改寫問題不得包含語意標籤")
        return self


class ModelStep(StrictModel):
    model: str = Field(min_length=1)

    @model_validator(mode="after")
    def nonblank_model(self) -> Self:
        if not self.model.strip():
            raise ValueError("model 不得空白")
        return self


class OpenAIStep(ModelStep):
    # null 時不傳參數，適用不支援推理設定的模型（例如 gpt-4o-mini）。
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None


class ExperimentConfig(StrictModel):
    # B、D 共用同一設定；A、C 使用 PipelineConfig.step3。
    openai_judge: OpenAIStep


class JevStep(ModelStep):
    # 舊 answerable 使用 >；新版 needs_full_report 使用 >=，0.5 轉完整財報。
    yes_threshold: float = Field(default=0.5, ge=0.0, lt=1.0)


class PipelineConfig(StrictModel):
    step1: OpenAIStep
    status_check: OpenAIStep
    step2: OpenAIStep
    step3: JevStep
    rewrite_check: OpenAIStep | None = None
    context_check: OpenAIStep | None = None
    timeout_seconds: float = Field(default=60.0, gt=0.0, allow_inf_nan=False)


class FinancialReport(StrictModel):
    mode: Literal["fields"]
    scope: str = Field(min_length=1)
    information: str | list[JsonValue] | dict[str, JsonValue]

    @model_validator(mode="after")
    def nonempty_report(self) -> Self:
        if not self.scope.strip() or not self.information:
            raise ValueError("財報範圍和資訊不得為空")
        if isinstance(self.information, str) and not self.information.strip():
            raise ValueError("財報資訊不得只有空白")
        return self


class RoutingConfig(StrictModel):
    judge: OpenAIStep
    timeout_seconds: float = Field(default=60.0, gt=0.0, allow_inf_nan=False)


class RoutingExperimentQuestion(StrictModel):
    question_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    gold_needs_full_report: bool | None = None

    @model_validator(mode="after")
    def nonblank_question(self) -> Self:
        if not self.question_id.strip() or not self.question.strip():
            raise ValueError("題目 ID 與原題不得空白")
        return self


class AccountingItem(StrictModel):
    item_id: str = Field(min_length=1)
    statement: Literal["資產負債表", "綜合損益表", "現金流量表", "權益變動表"]
    label: str = Field(min_length=1)
    element: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    observed_reports: int = Field(gt=0)

    @property
    def qname(self) -> str:
        return "{" + self.namespace + "}" + self.element


class DatabaseRoutingResult(StrictModel):
    # 這與歷史實驗的 answerable 方向相反；不可直接沿用人工標籤。
    needs_full_report: bool
    required_item_ids: list[str]
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_routing(self) -> Self:
        if not self.reason.strip():
            raise ValueError("reason 必須說明分流依據")
        if not self.needs_full_report and not self.required_item_ids:
            raise ValueError("資料庫可回答時，必須列出必要科目")
        if len(set(self.required_item_ids)) != len(self.required_item_ids):
            raise ValueError("必要科目 ID 不得重複")
        return self


class RewriteValidationResult(StrictModel):
    # 本機規則提早拒絕時，未檢查的項目為 null，不能假稱已通過。
    company_preserved: bool | None
    period_preserved: bool | None
    units_preserved: bool | None
    filters_preserved: bool | None
    operations_preserved: bool | None
    concepts_preserved: bool | None
    no_added_assumptions: bool | None
    reason: str = Field(min_length=1)

    @property
    def is_faithful(self) -> bool:
        return all(getattr(self, name) is True for name in type(self).model_fields if name != "reason")

    @model_validator(mode="after")
    def nonblank_reason(self) -> Self:
        if not self.reason.strip():
            raise ValueError("改寫驗證必須說明依據")
        return self


class GroupAInput(StrictModel):
    original_question: str = Field(min_length=1)
    latest_year: int | None = Field(default=None, ge=1900, le=9999)
    current_company: str | None = None

    @model_validator(mode="after")
    def valid_input(self) -> Self:
        if not self.original_question.strip():
            raise ValueError("USER 原始問題不得空白")
        if self.current_company is not None and not self.current_company.strip():
            raise ValueError("當前公司不得空白；沒有值時請傳 null")
        return self


class ContextRequirements(StrictModel):
    # specified 包含相對期間、尋找年度、跨公司範圍，不限於明確年份／公司名。
    year_scope: Literal["specified", "missing", "not_required"]
    company_scope: Literal["specified", "missing", "not_required"]
    year_evidence: str | None
    company_evidence: str | None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_evidence(self) -> Self:
        if not self.reason.strip():
            raise ValueError("上下文判斷必須說明理由")
        for name in ("year", "company"):
            scope, evidence = getattr(self, name + "_scope"), getattr(self, name + "_evidence")
            if scope == "specified":
                if evidence is None or not evidence.strip():
                    raise ValueError("已指定範圍必須提供原文片段")
            elif evidence is not None:
                raise ValueError("缺漏或不需要的範圍不得捏造原文片段")
        return self


def contextualize_question(original: str, year: int | None, company: str | None) -> str:
    conditions = []
    if year is not None:
        conditions.append(f"年度：{year}年")
    if company is not None:
        conditions.append(f"公司：{company}")
    if not conditions:
        return original
    return original + "\n（系統補入查詢條件：" + "；".join(conditions) + "）"


class ContextResolution(StrictModel):
    status: Literal["skipped", "completed", "error"]
    original_question: str
    contextual_question: str
    latest_year: int | None = None
    current_company: str | None = None
    applied_year: int | None = None
    applied_company: str | None = None
    requirements: ContextRequirements | None = None
    error_type: str | None = None

    @model_validator(mode="after")
    def authorized_defaults(self) -> Self:
        GroupAInput(original_question=self.original_question, latest_year=self.latest_year,
                    current_company=self.current_company)
        if self.status == "skipped" and (self.requirements is not None or self.error_type is not None
                                         or self.latest_year is not None or self.current_company is not None):
            raise ValueError("只有未提供前端參數時才略過判斷")
        if self.status == "error" and (self.error_type is None or not self.error_type.strip()):
            raise ValueError("上下文處理失敗必須保存錯誤類型")
        if self.status != "completed" and (self.applied_year is not None or self.applied_company is not None):
            raise ValueError("未完成上下文判斷時不得套入參數")
        if self.status == "completed" and (self.requirements is None or self.error_type is not None):
            raise ValueError("完成上下文判斷必須保留有效判斷")
        for name, supplied in (("year", self.latest_year), ("company", self.current_company)):
            applied = getattr(self, "applied_" + name)
            if applied is not None and (applied != supplied or self.requirements is None
                                        or getattr(self.requirements, name + "_scope") != "missing"):
                raise ValueError("只能補入必要、缺漏且由前端提供的參數")
            if self.status == "completed" and getattr(self.requirements, name + "_scope") == "missing" and applied is None:
                raise ValueError("缺少必要的前端參數")
        if self.contextual_question != contextualize_question(self.original_question, self.applied_year, self.applied_company):
            raise ValueError("補入問題只能附加已核准參數，不得變更原題")
        return self


class PreparedQuestion(StrictModel):
    original_question: str
    contextual_question: str | None = None
    question_used: str
    outcome: Literal["original", "contextual", "rewritten"]
    reason: str | None = None
    template: QuestionTemplate | None = None
    classification: StatusResult | None = None
    rewrite: RewriteResult | None = None
    rewrite_validation: RewriteValidationResult | None = None
    error_type: str | None = None
    error_stage: str | None = None

    @model_validator(mode="after")
    def faithful_selection(self) -> Self:
        if not self.original_question.strip() or not self.question_used.strip():
            raise ValueError("原題與交接題目不得為空")
        if self.outcome == "original" and self.question_used != self.original_question:
            raise ValueError("保留原題時不能替換文字")
        if self.contextual_question is not None:
            if not self.contextual_question.strip():
                raise ValueError("上下文問題不得空白")
            if self.outcome == "original" and self.contextual_question != self.original_question:
                raise ValueError("已補入上下文後不得回退裸原題")
        if self.outcome == "contextual" and (self.contextual_question is None
                                            or self.question_used != self.contextual_question
                                            or self.contextual_question == self.original_question):
            raise ValueError("使用補入題時必須對應上下文基準")
        if self.outcome == "rewritten":
            if self.rewrite is None or self.question_used != self.rewrite.rewritten_question:
                raise ValueError("使用題目必須對應保存的改寫")
            if self.rewrite_validation is None or not self.rewrite_validation.is_faithful:
                raise ValueError("實際改寫必須通過原意驗證")
        return self


RoutingCheckName = Literal["missing_item", "missing_detail", "outside_catalog_information"]
AgentRoute = Literal["database", "full_report"]


class JevRoutingResult(StrictModel):
    needs_full_report: bool
    probabilities: dict[RoutingCheckName, float]
    flags: dict[RoutingCheckName, bool]
    yes_threshold: float
    reason: str
    catalog_sha256: str
    judgment_version: str
    data_availability: Literal["unchecked"] = "unchecked"

    @model_validator(mode="after")
    def consistent_checks(self) -> Self:
        names = {"missing_item", "missing_detail", "outside_catalog_information"}
        if set(self.probabilities) != names or set(self.flags) != names:
            raise ValueError("必須保留三項分流判斷")
        if not 0 <= self.yes_threshold < 1:
            raise ValueError("分流門檻不正確")
        for name, probability in self.probabilities.items():
            if not 0 <= probability <= 1 or self.flags[name] != (probability >= self.yes_threshold):
                raise ValueError("分流機率與旗標不一致")
        if self.needs_full_report != any(self.flags.values()):
            raise ValueError("分流結果必須採三項 OR 規則")
        return self


class GroupARoutingResult(StrictModel):
    pipeline_version: Literal["group_a_context_v2"] = "group_a_context_v2"
    bool_semantics: Literal["needs_full_report"] = "needs_full_report"
    status: Literal["completed", "error"]
    original_question: str
    preprocessing: PreparedQuestion
    context_resolution: ContextResolution | None = None
    route: AgentRoute | None = None
    needs_full_report: bool | None = None
    judgment: JevRoutingResult | None = None
    # 查核責任交給資料庫 agent；分流結果不表示已查過 SQLite。
    data_availability: Literal["delegated_to_database_agent"] = "delegated_to_database_agent"
    error_type: str | None = None
    error_stage: str | None = None

    @model_validator(mode="after")
    def valid_route(self) -> Self:
        if self.preprocessing.original_question != self.original_question:
            raise ValueError("前處理原題不一致")
        if self.context_resolution is not None:
            if (self.context_resolution.original_question != self.original_question
                    or self.preprocessing.contextual_question != self.context_resolution.contextual_question):
                raise ValueError("前處理上下文不一致")
            if self.status == "completed" and self.context_resolution.status == "error":
                raise ValueError("上下文處理失敗不得調用 agent")
        elif self.preprocessing.contextual_question not in (None, self.original_question):
            raise ValueError("補入上下文必須有套用紀錄")
        if self.status == "completed":
            if self.judgment is None or self.needs_full_report is None:
                raise ValueError("完成分流必須有判斷結果")
            expected = "full_report" if self.needs_full_report else "database"
            if self.route != expected or self.needs_full_report != self.judgment.needs_full_report:
                raise ValueError("agent 路徑與判斷不一致")
            if self.error_type is not None or self.error_stage is not None:
                raise ValueError("完成分流不能包含服務錯誤")
        elif self.route is not None or self.needs_full_report is not None or self.judgment is not None:
            raise ValueError("服務錯誤不能變成布林分流結果")
        return self


class AgentResponse(StrictModel):
    status: Literal["completed", "needs_full_report", "insufficient_data", "error"]
    answer: JsonValue = None
    reason: str = ""
    error_type: str | None = None

    @model_validator(mode="after")
    def valid_response(self) -> Self:
        if self.status == "completed" and self.answer is None:
            raise ValueError("agent 完成時必須提供答案")
        if self.status != "completed" and not self.reason.strip():
            raise ValueError("agent 未完成時必須說明原因")
        return self


class AgentAttempt(StrictModel):
    route: AgentRoute
    response: AgentResponse


class AgentRequest(StrictModel):
    route: AgentRoute
    original_question: str
    question_used: str
    contextual_question: str | None = None
    applied_year: int | None = None
    applied_company: str | None = None
    routing: GroupARoutingResult
    task: str
    previous_attempts: list[AgentAttempt] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_context(self) -> Self:
        if (self.original_question != self.routing.original_question
                or self.question_used != self.routing.preprocessing.question_used):
            raise ValueError("agent 題目必須對應分流結果")
        baseline = self.routing.preprocessing.contextual_question or self.original_question
        if self.contextual_question != baseline:
            if self.contextual_question is not None or baseline != self.original_question:
                raise ValueError("agent 的上下文基準不一致")
        context = self.routing.context_resolution
        if (self.applied_year != (context.applied_year if context is not None else None)
                or self.applied_company != (context.applied_company if context is not None else None)):
            raise ValueError("agent 只能接收分流已套用的參數")
        return self


class GroupAExecutionResult(StrictModel):
    routing: GroupARoutingResult
    status: Literal["completed", "awaiting_agent", "routing_error", "agent_error", "insufficient_data"]
    final_route: AgentRoute | None = None
    answer: JsonValue = None
    reason: str = ""
    attempts: list[AgentAttempt] = Field(default_factory=list)
