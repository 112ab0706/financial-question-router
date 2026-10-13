"""可確定的文字保護；其餘語意由獨立複核處理。"""

import re
import unicodedata

from models import QuestionTemplate, RewriteValidationResult


def validate_template_fragments(question: str, template: QuestionTemplate) -> None:
    """提取欄位只允許原文片段；不得把模型猜測送往改寫。"""
    for value in template.model_dump().values():
        if value is None:
            continue
        fragments = re.split(r"[；;]", value)
        if any(not fragment.strip() or fragment.strip() not in question for fragment in fragments):
            raise ValueError("template 必須為原文片段或 null")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    # 民國年可換算，但必須是相同實際年度。
    text = re.sub(r"民[國国]\s*(\d{2,3})\s*年",
                  lambda match: str(int(match.group(1)) + 1911) + "年", text)
    return re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)


def local_rewrite_check(original: str, rewritten: str) -> RewriteValidationResult | None:
    """確定遺失／替換年份、數字或幣別即拒絕，不以文字相似度宣稱語意正確。"""
    before, after = _normalize(original), _normalize(rewritten)
    issues = {}
    reasons = []
    years = r"(?<!\d)(?:19|20|21)\d{2}(?!\d)"
    if set(re.findall(years, before)) != set(re.findall(years, after)):
        issues["period_preserved"] = False
        reasons.append("新增、刪除或替換明確年份")
    numbers = r"(?<!\d)\d+(?:\.\d+)?"
    if not set(re.findall(numbers, before)).issubset(set(re.findall(numbers, after))):
        issues["filters_preserved"] = False
        reasons.append("原題的明確數字未保留")
    currencies = {
        "TWD": r"新[臺台]幣|[Nn][Tt]\$|\bTWD\b|New Taiwan dollars?",
        "USD": r"美元|美金|US\$|\bUSD\b|U\.?S\.? dollars?",
        "EUR": r"歐元|欧元|\bEUR\b|\beuros?\b",
        "CNY": r"人民幣|人民币|\bCNY\b|\bRMB\b",
    }
    before_currencies = {name for name, pattern in currencies.items() if re.search(pattern, before, re.I)}
    after_currencies = {name for name, pattern in currencies.items() if re.search(pattern, after, re.I)}
    if before_currencies != after_currencies:
        issues["units_preserved"] = False
        reasons.append("幣別被新增、刪除或替換")
    if not issues:
        return None
    values = {name: None for name in RewriteValidationResult.model_fields if name != "reason"}
    return RewriteValidationResult(**{**values, **issues}, reason="；".join(reasons) + "，保留原題。")
