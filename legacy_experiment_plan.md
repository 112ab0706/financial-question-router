# 歷史四組配對實驗

> 2026-10-07：main.py 已改為依 database/2025q4_accounting_items.md 對原題做資料庫分流，True=需要完整財報。以下 A～E 為舊版簡明財報可回答性實驗（True=可回答），保留作重現用途；未重新實驗，也不能直接沿用舊人工標籤評估新分流。

目標：比較「只靠提供的臺灣簡明財報欄位能否完整回答」的正確性、費用與延遲。四組只判斷可回答性，不計算財務答案。

| 組別 | 判斷器接收的問題 | 判斷器 | 前處理 |
| --- | --- | --- | --- |
| A | 前處理選定的改寫題或原題 | Jev | 提取 → 是否改寫 → 必要時改寫 |
| B | 與 A 完全相同的題目 | OpenAI | 共用 A 的同一次前處理 |
| C | 原始 USER 問題 | Jev | 無 |
| D | 原始 USER 問題 | OpenAI | 無 |

A/B 比較改寫後的兩種判斷器，C/D 比較原題的兩種判斷器；A/C 及 B/D 比較整個前處理流程的效果。前處理包含是否改寫的選擇與必要改寫，兩條路徑都嘗試最終判斷；不能將所有差異只歸因於改寫文字。

## 程式行為

`legacy_experiments.py` 每題每輪先前處理一次，然後交錯執行四組判斷。A/B 共用同一個實際使用字串，可能是改寫題或原題，留下內容與 SHA-256；不分別呼叫改寫模型。每輪重新前處理，因此不同輪的選擇與改寫可能不同，但同一輪 A/B 一定一致。

status 只有 rewrite（改寫）與 no_rewrite（不改寫），沒有 rejected。rewrite 時改寫，成功則使用改寫題；no_rewrite 時直接使用原題。改寫回傳 null、格式錯誤、拒答或任何前處理 API 失敗時，回退原題繼續。每題四組都會嘗試最後判斷，最終 True/False 只來自判斷器，用於未來 agent 路由。

共用前處理記錄 question_used、outcome=rewritten/original，以及 reason=no_rewrite/cannot_rewrite/preprocessing_error 等回退原因，不能把這些原因直接當成 False。個別最終判斷器失敗記為 error/null，不影響其餘組。只有最終判斷器錯誤會讓該組沒有 bool；不保證外部服務永遠成功。

四組判斷器只收到問題與同一份財報欄位清單。B 不會額外收到原題、template 或 status。OpenAI 與 Jev 共用 `prompts.py` 的欄位可回答性規則；Jev 使用 Noul 與同一門檻，OpenAI 使用嚴格的 answerable 布林輸出。

## 固定設定與標註

- A/C 的 Jev 模型與門檻共用 `config.json` 的 step3；B/D 的模型與推理強度共用 `experiment_config.json` 的 openai_judge。預設 B/D 為 gpt-5.4-mini、medium，可自行修改。
- A/B 前處理維持 config.json：4.1 mini 提取、6 Luna low 選擇是否改寫、5.4 mini medium 改寫。
- 四組同題同範圍。每批次存財報、題目、模型設定、提示詞與費率快照；每次呼叫保存實際回傳版本、usage、提示雜湊與延遲。
- 人工根據原題及同一份欄位清單先填 gold_answerable。模型輸出不當成標準答案；另檢核改寫是否保留原意。JSON 省略 gold 或填 null 時只記錄結果與成本，不計正確率。
- 20 題可先試跑，再擴大涵蓋直接取值、比率、跨期比較、術語整理、只存在附註或客戶明細的問題，以及概念不明的題目。可回答及不可回答題都要納入。
- 重複測試用同一批題目，每輪 A/B 共用前處理。20 題 × 4 組 × 3 輪 = 240 次組別評估；所有題目通過改寫時共 420 次 API 呼叫，每題每輪是共用前處理 3 次加四個判斷器各 1 次。不改寫時每題每輪少一次改寫呼叫，共 6 次；前處理錯誤可能減少前處理呼叫，但仍執行四個最終判斷。
- 判斷器順序由固定 seed 隨機化並記錄。seed 只固定執行順序，不保證 LLM 輸出相同；先前處理仍必須先完成。比較完整子集時，同時報告所有原題的結果與實際改寫／保留原題的子集結果，避免只挑成功改寫的題目。

## 執行

單題執行四組：

```powershell
.\.venv\Scripts\python.exe legacy_experiments.py --question "截至2014年9月27日,公司兩大客戶合計佔應收貿易帳款總額的百分比是多少?"
```

批次 JSON 格式：

```json
[
  {"question_id": "q001", "question": "甲公司2025年的營業收入是多少？", "gold_answerable": null},
  {"question_id": "q002", "question": "公司兩大客戶占應收帳款的百分比是多少？", "gold_answerable": null}
]
```

把 null 改為人工標註的 true 或 false，然後執行：

```powershell
.\.venv\Scripts\python.exe legacy_experiments.py --dataset examples/experiment_questions.json --repeat 3 --output test_results/legacy_experiment_001
```

輸出目錄必須是新目錄，避免覆寫紀錄；省略 --output 時自動建立時間命名目錄。--report、--config、--experiment-config、--pricing 可指定替代檔；--seed 預設 42。執行會實際呼叫 API 並產生費用。

輸出包含 manifest.json、每題每輪的 q0001_r1.json 等檔案及 summary.json。每題結束就存檔及更新彙總。前處理錯誤會記錄並回退原題；個別最終判斷器錯誤會記錄並繼續測試。有最終判斷器錯誤時結束碼為 1；前處理失敗但四組最終判斷都成功時結束碼為 0。設定、金鑰或寫檔失敗會停止批次。

## 成本與指標

每題紀錄的 calls 是實際呼叫總帳，共用前處理只出現一次。total_cost_usd 與彙總的 actual_experiment_cost 才是整次實驗的費率試算。

每組另列 judge_cost（該組判斷器費用）及 standalone_cost_estimate（單獨部署該組的參考費用）。A/B 的部署試算各包含同一份前處理成本，C/D 僅包含各自判斷器；四組部署費用相加會重複計入前處理，不能當作本批次實際花費。部署試算沿用本次觀察到的 usage 與快取價格，沒有重跑冷啟動或預測未來快取命中率。

費用使用 pricing.json 及回傳 usage，推理 tokens 不重複加計。缺少 usage 或未知模型費率時成本為 null，同時顯示 known_cost_usd 與 unknown_cost_calls。停用 SDK 自動重試，避免隱藏請求漏算。費用屬費率試算，實際扣款以供應商帳單為準。

summary.json 彙總每組 Accuracy、Precision、Recall、F1、TP/TN/FP/FN、True/False 數、最終判斷錯誤數、費用及平均 API 延遲。Accuracy 與混淆矩陣只計有人工標註且 completed 的 bool。另列 decision_coverage、labeled_decision_coverage、preprocessing_errors、rewritten_inputs、original_inputs。前處理錯誤後若最終判斷成功，該 bool 正常參與評分；不因前處理失敗把結果直接視為錯誤分類。

第 3 版結果在 manifest 及逐題紀錄標示 pipeline_architecture=optional_rewrite。原有 rejected / cannot_rewrite 停止指標僅供解讀歷史紀錄，新流程不再產生這些停止狀態。無人工標註或分母為零時相關指標為 null。人工 bool 只用於離線評分，不傳入任何模型。

api_latency_seconds 是該組需用的 API 呼叫耗時總和，A/B 包含共用前處理；不包含等候其他組的時間，也不是整批次牆鐘時間。重複輪次是同題的穩定性觀察，不能當作彼此獨立的新題樣本。若要宣稱模型優劣，應以題目配對分析，並報告不確定性。

## 單獨增測 E 組

E 為「原始英文問題 → GPT-6 Luna（low）最終判斷」，使用同一人工答案、財報欄位及 C/D 的判斷提示詞。experiment_luna.py 每題只有一次 OpenAI 呼叫，單獨記錄 usage、費用及評分，A～D 不重跑。可與先前同一批題目的結果對照；須標明不是同時執行，也不是多輪平均結果。
