"""原始題目 → 資料庫科目判斷 → 是否需要完整財報（不執行 agent）。"""

import json
import math
import os
from collections.abc import Callable

from openai import OpenAI
from typesafe_sdk import Noul, NoulCriteria, TypeSafeClient

import prompts
from database_catalog import DatabaseCatalog
from models import AccountingItem, DatabaseRoutingResult, JevRoutingResult, JevStep, RoutingConfig
from pipeline import PipelineError, ask_openai
from telemetry import CallRecorder

# 接入 SQLite 後，以原題、必要科目的完整 QName 查核公司／期間／維度。
# True 表示所有必要資料已確認可取得；False 表示缺少或尚無法確認。
AvailabilityCheck = Callable[[str, list[AccountingItem]], bool]


class JevDatabaseQuestionRouter:
    """A/C 共用：三項 Noul 同批呼叫，由程式 OR 合併。"""
    def __init__(self, config: JevStep, client: TypeSafeClient,
                 recorder: CallRecorder | None = None) -> None:
        self.config = config
        self.client = client
        self.recorder = recorder if recorder is not None else CallRecorder()

    @staticmethod
    def build_state(question: str, catalog: DatabaseCatalog, *, original_question: str | None = None) -> dict:
        # 完整科目涵義仍保留；來源年月、尚未查核資料列等稽核資訊留在 manifest，
        # 避免在能力判斷中被誤當成科目缺漏。查核實際資料是另一個程式步驟。
        view = catalog.to_jev_state()
        state = {
            "original_question": original_question if original_question is not None else question,
            "scope_policy": prompts.JEV_ROUTING_SCOPE,
            "database_catalog": {
                "item_columns": view["item_columns"], "statements": view["statements"],
            },
        }
        if original_question is not None:
            state.update(question_used=question, question_policy=(
                "original_question is authoritative. question_used is a verified wording aid; "
                "keep every condition and information requirement from the original."
            ))
        return state

    def judge(self, question: str, catalog: DatabaseCatalog, *,
              original_question: str | None = None) -> JevRoutingResult:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("判斷問題不得為空")
        if original_question is not None and (not isinstance(original_question, str) or not original_question.strip()):
            raise ValueError("USER 原始問題不得為空")
        state = self.build_state(question, catalog, original_question=original_question)
        version = (prompts.JEV_GROUP_A_ROUTING_VERSION if original_question is not None
                   else prompts.JEV_ROUTING_VERSION)
        checks = prompts.JEV_ROUTING_CHECKS
        questions = {name: Noul(
            instructions=check["instructions"],
            criteria=NoulCriteria(true=check["true"], false=check["false"]),
        ) for name, check in checks.items()}
        identity = json.dumps({
            "state": state, "checks": checks, "version": version,
            "settings": self.config.model_dump(), "threshold_rule": ">=", "combine": "any",
        }, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self.recorder.call("typesafe", self.config.model, "needs_full_report", identity) as event:
            event["catalog_representation"] = "all_items_labels_without_ids_scope_only_v2"
            event["routing_judgment_version"] = version
            response = self.client.system_one(
                model=self.config.model, state=state,
                questions=questions,
            )
            self.recorder.response(event, response)
            probabilities = {}
            for name in checks:
                answer = response.nouls.get(name)
                if answer is None:
                    raise PipelineError(f"Jev 未回傳 {name} 的 Noul 結果")
                probability = answer.noul
                if (isinstance(probability, bool) or not isinstance(probability, (int, float))
                        or not math.isfinite(probability) or not 0 <= probability <= 1):
                    raise PipelineError("Jev 的 Noul 機率必須是 0 至 1 的有限數值")
                probabilities[name] = probability
            flags = {name: p >= self.config.yes_threshold for name, p in probabilities.items()}
            result = any(flags.values())
            labels = {
                "missing_item": "缺少必要科目", "missing_detail": "缺少必要明細",
                "outside_catalog_information": "需要科目以外資訊",
            }
            reason = ("、".join(labels[name] for name, flag in flags.items() if flag) + "，交給完整財報 agent。"
                      if result else "科目與粒度判斷足夠，交給資料庫 agent 查核及回答。")
            detail = JevRoutingResult(
                needs_full_report=result, probabilities=probabilities, flags=flags,
                yes_threshold=self.config.yes_threshold, reason=reason,
                catalog_sha256=catalog.source_sha256, judgment_version=version,
            )
            # 只合併三項布林值；不把 max(p) 冒充整體需要完整財報的機率。
            event.update(probabilities=probabilities, flags=flags, combine="any", result=result, routing={
                "needs_full_report": result, "data_availability": "unchecked",
                "catalog_sha256": catalog.source_sha256,
            })
            return detail

    def run(self, question: str, catalog: DatabaseCatalog) -> bool:
        """實驗 bool 介面；正式 A 入口使用 judge 的完整結果。"""
        return self.judge(question, catalog).needs_full_report


class DatabaseQuestionRouter:
    def __init__(self, config: RoutingConfig, client: OpenAI,
                 recorder: CallRecorder | None = None) -> None:
        self.config = config
        self.client = client
        self.recorder = recorder if recorder is not None else CallRecorder()

    def judge(self, question: str, catalog: DatabaseCatalog,
              availability_check: AvailabilityCheck | None = None) -> DatabaseRoutingResult:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("USER 原始問題不得為空")
        result = ask_openai(
            self.client, self.recorder, self.config.judge,
            prompts.DATABASE_ROUTING_SYSTEM_PROMPT, prompts.DATABASE_ROUTING_TASK,
            {"original_question": question, "database_catalog": catalog.to_judgment_state()},
            DatabaseRoutingResult, compact_payload=True,
        )
        by_id = catalog.by_id
        if any(item_id not in by_id for item_id in result.required_item_ids):
            raise PipelineError("分流模型引用不存在的資料庫科目 ID")
        availability = "unchecked"
        if not result.needs_full_report and availability_check is not None:
            available = availability_check(question, [by_id[i] for i in result.required_item_ids])
            if type(available) is not bool:
                raise ValueError("資料庫查核必須回傳 bool；無法確認時回傳 False")
            availability = "available" if available else "unavailable_or_unverified"
            if not available:
                result = result.model_copy(update={
                    "needs_full_report": True,
                    "reason": result.reason + " 資料庫查核未確認所有必要資料可取得，改交完整財報。",
                })
        self.recorder.calls[-1]["routing"] = {
            **result.model_dump(mode="json"), "data_availability": availability,
            "catalog_sha256": catalog.source_sha256,
        }
        return result

    def run(self, question: str, catalog: DatabaseCatalog,
            availability_check: AvailabilityCheck | None = None) -> bool:
        return self.judge(question, catalog, availability_check).needs_full_report


def needs_full_report(question: str, catalog: DatabaseCatalog, config: RoutingConfig,
                      recorder: CallRecorder | None = None,
                      availability_check: AvailabilityCheck | None = None) -> bool:
    """只需要 OpenAI 金鑰；API 錯誤會拋出，不能當成資料庫足夠。"""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("USER 原始問題不得為空")
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ValueError("請設定 OPENAI_API_KEY")
    with OpenAI(api_key=key, timeout=config.timeout_seconds, max_retries=0) as client:
        return DatabaseQuestionRouter(config, client, recorder).run(question, catalog, availability_check)
