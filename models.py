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


class PreparedQuestion(StrictModel):
    original_question: str
    question_used: str
    outcome: Literal["original", "rewritten"]
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
    pipeline_version: Literal["group_a_guarded_v1"] = "group_a_guarded_v1"
    bool_semantics: Literal["needs_full_report"] = "needs_full_report"
    status: Literal["completed", "error"]
    original_question: str
    preprocessing: PreparedQuestion
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
    routing: GroupARoutingResult
    task: str
    previous_attempts: list[AgentAttempt] = Field(default_factory=list)


class GroupAExecutionResult(StrictModel):
    routing: GroupARoutingResult
    status: Literal["completed", "awaiting_agent", "routing_error", "agent_error", "insufficient_data"]
    final_route: AgentRoute | None = None
    answer: JsonValue = None
    reason: str = ""
    attempts: list[AgentAttempt] = Field(default_factory=list)
