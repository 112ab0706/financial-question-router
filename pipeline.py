"""提取 → 選擇是否改寫 → 使用改寫題或原題做最終判斷。"""

import json
import math
import os
from contextlib import ExitStack
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel
from typesafe_sdk import Noul, NoulCriteria, RetryPolicy, TypeSafeClient
from telemetry import CallRecorder

import prompts
from models import (
    AnswerabilityResult, FinancialReport, OpenAIStep, PipelineConfig, QuestionTemplate,
    RewriteResult, RewriteValidationResult, StatusResult,
)
from rewrite_validation import local_rewrite_check, validate_template_fragments

ParsedModel = TypeVar("ParsedModel", bound=BaseModel)


class PipelineError(RuntimeError):
    """模型未提供有效結果；與判斷結果 False 區分。"""


def ask_openai(
    client: OpenAI, recorder: CallRecorder, step: OpenAIStep,
    system_prompt: str, task: str, payload: dict, output_type: type[ParsedModel],
    *, compact_payload: bool = False,
) -> ParsedModel:
    """共用結構化呼叫；路由與歷史實驗使用相同 usage / 錯誤處理。"""
    options = {}
    if step.reasoning_effort is not None:
        options["reasoning"] = {"effort": step.reasoning_effort}
    prompt_identity = json.dumps({
        "system": system_prompt, "task": task, "payload": payload,
        "schema": output_type.model_json_schema(), "settings": step.model_dump(),
    }, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with recorder.call("openai", step.model, output_type.__name__, prompt_identity) as event:
        response = client.responses.parse(
            model=step.model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task + "\n" + json.dumps(
                    payload, ensure_ascii=False, allow_nan=False,
                    **({"separators": (",", ":")} if compact_payload else {}),
                )},
            ],
            text_format=output_type, store=False, **options,
        )
        recorder.response(event, response)
        if response.status != "completed" or response.output_parsed is None:
            raise PipelineError("OpenAI 拒絕、未完成或未回傳有效結構化結果")
        event["result"] = response.output_parsed.model_dump(mode="json", by_alias=True)
        return response.output_parsed


class QuestionPipeline:
    """可注入 SDK client，方便離線測試；client 的生命週期由呼叫者管理。"""

    def __init__(
        self, config: PipelineConfig, openai_client: OpenAI,
        jev_client: TypeSafeClient | None,
        recorder: CallRecorder | None = None,
    ) -> None:
        self.config = config
        self.openai = openai_client
        self.jev = jev_client
        self.recorder = recorder if recorder is not None else CallRecorder()

    def _ask_openai(
        self, step: OpenAIStep, system_prompt: str, task: str,
        payload: dict, output_type: type[ParsedModel],
    ) -> ParsedModel:
        return ask_openai(
            self.openai, self.recorder, step, system_prompt, task, payload, output_type,
        )

    def extract_template(self, question: str) -> QuestionTemplate:
        return self._ask_openai(
            self.config.step1, prompts.STEP1_SYSTEM_PROMPT,
            prompts.STEP1_TASK, {"user_question": question}, QuestionTemplate,
        )

    def classify_status(
        self, question: str, template: QuestionTemplate,
    ) -> StatusResult:
        return self._ask_openai(
            self.config.status_check, prompts.STATUS_SYSTEM_PROMPT,
            prompts.STATUS_TASK,
            {"user_question": question, "OriginalTemplate": template.model_dump()},
            StatusResult,
        )

    def rewrite_question(
        self, question: str, template: QuestionTemplate, classification: StatusResult,
    ) -> RewriteResult:
        if classification.status != "rewrite":
            raise ValueError("只有 status=rewrite 才呼叫改寫模型；no_rewrite 使用原題")
        return self._ask_openai(
            self.config.step2, prompts.STEP2_SYSTEM_PROMPT,
            prompts.STEP2_TASK,
            {
                "user_question": question, "OriginalTemplate": template.model_dump(),
                "Status": classification.status, "Status_Context_Gap": classification.reason,
            },
            RewriteResult,
        )

    def is_answerable(self, rewritten_question: str, report: FinancialReport) -> bool:
        prompt_identity = json.dumps({
            "instructions": prompts.JEV_INSTRUCTIONS, "question": rewritten_question,
            "report": report.model_dump(mode="json"), "settings": self.config.step3.model_dump(),
            "true": "提供的簡明財報資訊足以完整回答問題，無須外部資訊。",
            "false": "至少一項必要資訊不在提供的簡明財報範圍或內容中。",
        }, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self.recorder.call("typesafe", self.config.step3.model, "answerable", prompt_identity) as event:
            return self._ask_jev(rewritten_question, report, event)

    def _ask_jev(self, rewritten_question: str, report: FinancialReport, event: dict) -> bool:
        if self.jev is None:
            raise ValueError("尚未設定 Jev client")
        response = self.jev.system_one(
            model=self.config.step3.model,
            state={
                "question": rewritten_question,
                "financial_report": report.model_dump(mode="json"),
            },
            questions={
                "answerable": Noul(
                    instructions=prompts.JEV_INSTRUCTIONS,
                    criteria=NoulCriteria(
                        true="提供的簡明財報資訊足以完整回答問題，無須外部資訊。",
                        false="至少一項必要資訊不在提供的簡明財報範圍或內容中。",
                    ),
                ),
            },
        )
        self.recorder.response(event, response)
        answer = response.nouls.get("answerable")
        if answer is None:
            raise PipelineError("Jev 未回傳 answerable 的 Noul 結果")
        probability = answer.noul
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (int, float))
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            raise PipelineError("Jev 的 Noul 機率必須是 0 至 1 的有限數值")
        event["probability"] = probability
        result = bool(probability > self.config.step3.yes_threshold)
        event["result"] = result
        return result

    def is_answerable_openai(
        self, question: str, report: FinancialReport, judge: OpenAIStep,
    ) -> bool:
        return self._ask_openai(
            judge, prompts.ANSWERABILITY_SYSTEM_PROMPT, prompts.ANSWERABILITY_TASK,
            {"question": question, "financial_report": report.model_dump(mode="json")},
            AnswerabilityResult,
        ).answerable

    def validate_rewrite(self, original: str, rewritten: str) -> RewriteValidationResult:
        local_result = local_rewrite_check(original, rewritten)
        if local_result is not None:
            return local_result
        return self._ask_openai(
            self.config.rewrite_check or self.config.step2,
            prompts.REWRITE_VALIDATION_SYSTEM_PROMPT, prompts.REWRITE_VALIDATION_TASK,
            {"original_question": original, "rewritten_question": rewritten},
            RewriteValidationResult,
        )

    def prepare_question(self, question: str, *, verify_rewrite: bool = False) -> dict:
        """共用前處理；所有非空題目都有可送給最終判斷器的文字。"""
        if not isinstance(question, str) or not question.strip():
            raise ValueError("USER 問題不得為空")
        prepared = {
            "template": None, "classification": None, "rewrite": None,
            "question_used": question, "outcome": "original", "reason": None,
        }
        if verify_rewrite:
            prepared["rewrite_validation"] = None
        stage = "extract_template"
        try:
            template = self.extract_template(question)
            prepared["template"] = template.model_dump(mode="json")
            if verify_rewrite:
                stage = "validate_template"
                validate_template_fragments(question, template)
            stage = "classify_status"
            classification = self.classify_status(question, template)
            prepared["classification"] = classification.model_dump(mode="json", by_alias=True)
            if classification.status == "no_rewrite":
                prepared["reason"] = "no_rewrite"
                return prepared
            stage = "rewrite_question"
            rewrite = self.rewrite_question(question, template, classification)
            prepared["rewrite"] = rewrite.model_dump(mode="json", by_alias=True)
            if rewrite.can_rewrite:
                if verify_rewrite and rewrite.rewritten_question == question:
                    prepared["reason"] = "unchanged_rewrite"
                    return prepared
                if verify_rewrite and rewrite.rewritten_question != question:
                    stage = "validate_rewrite"
                    validation = self.validate_rewrite(question, rewrite.rewritten_question)
                    prepared["rewrite_validation"] = validation.model_dump(mode="json")
                    if not validation.is_faithful:
                        prepared["reason"] = "rewrite_validation_failed"
                        return prepared
                prepared.update(question_used=rewrite.rewritten_question, outcome="rewritten")
            else:
                prepared["reason"] = "cannot_rewrite"
        except Exception as error:
            # 保留原題；錯誤原因只存類型，不存 HTTP body 或敏感訊息。
            prepared.update(reason="preprocessing_error", error_type=type(error).__name__, error_stage=stage)
        return prepared

    def run(self, question: str, report: FinancialReport) -> bool:
        prepared = self.prepare_question(question)
        return self.is_answerable(prepared["question_used"], report)


def evaluate_question(
    question: str, report: FinancialReport, config: PipelineConfig,
    recorder: CallRecorder | None = None,
) -> bool:
    """歷史 answerable 入口；True=簡明財報足夠，不是新版 A 的分流入口。"""
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    typesafe_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not openai_key or not typesafe_key:
        raise ValueError("請設定 OPENAI_API_KEY 與 TYPESAFE_API_KEY")
    with ExitStack() as stack:
        openai_client = stack.enter_context(OpenAI(
            api_key=openai_key, timeout=config.timeout_seconds,
            max_retries=0 if recorder is not None else 2,
        ))
        jev_client = stack.enter_context(TypeSafeClient(
            api_key=typesafe_key, timeout=config.timeout_seconds,
            retry=RetryPolicy(max_retries=0) if recorder is not None else RetryPolicy(),
        ))
        return QuestionPipeline(config, openai_client, jev_client, recorder).run(question, report)
