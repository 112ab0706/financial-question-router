"""僅補入回答必要而原題缺漏的前端參數，不生成公司或年度。"""

import re
import unicodedata

import prompts
from models import ContextRequirements, ContextResolution, GroupAInput, contextualize_question
from pipeline import QuestionPipeline, ask_openai


def parse_group_a_input(value: str | GroupAInput | dict) -> GroupAInput:
    if isinstance(value, GroupAInput):
        return GroupAInput.model_validate(value.model_dump())
    return GroupAInput.model_validate({"original_question": value} if isinstance(value, str) else value)


def validate_requirements(request: GroupAInput, requirements: ContextRequirements) -> None:
    for name in ("year", "company"):
        evidence = getattr(requirements, name + "_evidence")
        if evidence is not None and evidence not in request.original_question:
            raise ValueError("上下文範圍的證據必須取自原文")
    text = unicodedata.normalize("NFKC", request.original_question)
    # 明確時間與搜尋範圍的保守攔截；季度本身仍允許補缺少的年度。
    time_scope = (
        r"(?<!\d)(?:19|20|21)\d{2}\s*(?:年|Q[1-4]\b|[-/]\d{1,2}(?:[-/]\d{1,2})?)|"
        r"\b(?:in|for|FY|fiscal year)\s*(?:19|20|21)\d{2}\b|"
        r"民[國国]\s*\d{2,3}\s*年|"
        r"今年|去年|前年|明年|哪一?年|何年|歷年|历年|各年|每年|所有年度|"
        r"(?:近|最近|過去|过去|未來|未来)[\d一二三四五六七八九十兩两]+年|"
        r"\b(?:this|last|next|which|every|each)\s+year\b|"
        r"\b(?:past|last|recent|next)\s+(?:\d+|one|two|three|five|ten)\s+years\b|\ball\s+years\b"
    )
    if requirements.year_scope == "missing" and re.search(time_scope, text, re.I):
        raise ValueError("原題已有時間條件，不能補入單一年度")
    company_scope = r"(?:所有|全部|各家|每家|哪些|哪家|哪一間|哪一家公司).{0,8}(?:公司|企業|企业)|產業平均|产业平均|\ball companies\b|\bwhich company\b"
    # 裸數字可能是金額或門檻；公司代號由語意判斷，不能只做數字子字串比對。
    current_named = (request.current_company is not None and not request.current_company.strip().isdigit()
                     and request.current_company in request.original_question)
    if requirements.company_scope == "missing" and (current_named or re.search(company_scope, text, re.I)):
        raise ValueError("原題已有公司範圍，不能補入當前公司")


def resolve_context(request: GroupAInput, pipeline: QuestionPipeline) -> ContextResolution:
    values = {**request.model_dump(), "contextual_question": request.original_question}
    if request.latest_year is None and request.current_company is None:
        return ContextResolution(status="skipped", **values)
    try:
        # 只分析原題；未採用的前端值不影響必要性判斷。
        requirements = ask_openai(
            pipeline.openai, pipeline.recorder,
            pipeline.config.context_check or pipeline.config.status_check,
            prompts.CONTEXT_REQUIREMENTS_SYSTEM_PROMPT, prompts.CONTEXT_REQUIREMENTS_TASK,
            {"original_question": request.original_question}, ContextRequirements,
        )
        validate_requirements(request, requirements)
        year = request.latest_year if requirements.year_scope == "missing" else None
        company = request.current_company if requirements.company_scope == "missing" else None
        if ((requirements.year_scope == "missing" and year is None)
                or (requirements.company_scope == "missing" and company is None)):
            return ContextResolution(status="error", requirements=requirements,
                                     error_type="MissingContextParameters", **values)
        return ContextResolution(
            status="completed", requirements=requirements, applied_year=year, applied_company=company,
            **{**values, "contextual_question": contextualize_question(request.original_question, year, company)},
        )
    except Exception as error:
        # 不存服務回應／錯誤訊息，以免輸出敏感資料；也不猜測缺漏的參數。
        return ContextResolution(status="error", error_type=type(error).__name__, **values)
