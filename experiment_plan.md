# ABC 資料庫分流實驗

目標：比較不同流程判斷是否需要完整財報的正確率、使用量、費用與延遲。只判斷資料來源，不產生財務答案，也不直接執行完整財報 agent。

**三組一律 True=需要完整財報，False=資料庫科目及粒度足夠。**

| 組別 | 最終判斷輸入 | 判斷器 | 前處理 |
| --- | --- | --- | --- |
| A | 選定的改寫題或原題 | Jev（jev-latest） | 共用提取 → status → 必要改寫 |
| B | 與 A 完全相同的題目 | GPT-5.4 mini（medium） | 與 A 共用同一次前處理 |
| C | USER 原始題目 | Jev（jev-latest） | 不使用前處理產出的題目 |

A/B 比較相同題目的判斷器；A/C 比較整個前處理流程的效果。前處理含是否改寫的選擇，不能將所有差異只歸因於改寫文字。單題 main.py 的原題 → GPT-6 Luna 入口另行保留，沒有重新定義 C 組。

## 同題、同科目範圍

所有組別依 database/2025q4_accounting_items.md 的完整 1,743 筆科目，包含資產負債表、綜合損益表、現金流量表與權益變動表。範圍是資料庫觀察科目的聯集，不代表特定公司及期間都有資料。三組均標示 data_availability=unchecked，並不宣稱已查詢 SQLite。

判斷輸入保留全部科目名稱及報表分類，同名而不同義的科目保留 XBRL 元素以區分。B 組另提供科目 ID，以便回傳必要科目；A／C 的 Jev 只需輸出二元判斷，省略 ID 以降低 token 數。完整 QName 身分及原始清單另留在本機與 manifest，未截斷或篩除任何科目。各組科目涵義與判斷規則相同，傳送格式的差異亦記錄於 manifest。

OpenAI 使用 DATABASE_ROUTING_RULES 及結構化輸出規則。新版 Jev 使用 JEV_ROUTING_SCOPE 與 JEV_ROUTING_CHECKS 的較短英文指令，三項子判斷分別版本化，沒有使用 OpenAI system prompt。兩種判斷器都禁止以合計取代未提供的客戶、部門、產品或地區明細，也不會為符合清單而替換原公司、年份或題目邏輯。不同版本的輸出與提示詞須分批保留。

只提取、status 與必要改寫一次。A/B 共用 question_used 及相同雜湊。status= no_rewrite、改寫無效、拒答或前處理失敗時使用相同原題，仍執行所有最終判斷。C 保留原始字串。金標不傳給提取、status、改寫或任何最終判斷器。

config.json 設定提取 GPT-4.1 mini、status GPT-6 Luna low、改寫 GPT-5.4 mini medium，以及 A/C 的 Jev 模型及門檻。experiment_config.json 設定 B 的 OpenAI 模型。

Jev 三項 Noul 同批執行：missing_item、missing_detail、outside_catalog_information。每項以 probability >= step3.yes_threshold 判定，預設 0.5，程式 OR 合併；沒有捏造合成後的機率。缺少任何一項回傳均為 error/null。OpenAI 回傳 needs_full_report、required_item_ids 及繁體中文 reason。引用不存在的科目或無效結構為錯誤，不當成資料庫足夠。

## 重用題目的 AC 重測

experiment_jev_ac.py --source-run 指定既有實驗目錄，--gold 指定 needs_full_report 含義的人工答案，--output 指定新目錄。A 逐字重用來源 shared_preprocessing.question_used，C 逐字使用 original_question。此入口只呼叫 Jev，不重跑任何 OpenAI 前處理或 B。

前處理 16 題改寫、4 題保留原題的狀態亦留存。來源檔案與科目快照以 SHA-256 驗證，人工答案只用於後處理評分。新費用只包含 Jev，各組 20 次 API 呼叫，每次 3 項 Noul；每題逐步保存結果，即使一組出錯仍繼續另一組。

此版本同時變更指令語言、拆分判斷與能力說明，保留完整科目涵義，未篩除候選科目或調整 0.5 門檻。與舊版的差異屬於整體版本比較，不能單獨歸因於其中一項。本批已提供人工答案且用於診斷，是開發資料；仍需未見題目評估泛化。

## 人工標註

新版資料集格式：

```json
[
  {"question_id":"q001","question":"甲公司2025年營業收入是多少？","gold_needs_full_report":null},
  {"question_id":"q002","question":"兩大客戶占應收帳款的百分比是多少？","gold_needs_full_report":null}
]
```

標註 true=需要完整財報、false=資料庫科目與粒度足夠，必須以原題及同一份科目清單人工判斷。null 或省略金標時不計正確率。舊 gold_answerable 資料集被拒絕；不自動反轉舊標籤，因新科目範圍也已改變。

## 執行及費用

以下指令會實際呼叫付費 API，新結果逐批保存於 test_results：

```powershell
.\.venv\Scripts\python.exe experiments.py --question "公司兩大客戶占應收帳款的百分比是多少？"
.\.venv\Scripts\python.exe experiments.py --dataset examples/database_experiment_questions.json --repeat 1 --output test_results/ABC_database_new
```

--catalog、--config、--experiment-config、--pricing 可替換輸入；--seed 固定判斷器的執行順序，不能保證模型輸出相同。輸出目錄須為新目錄，不覆寫歷史結果。每題每輪先完成共用前處理，再依隨機順序執行三組並逐題存檔。

每題每輪正常改寫時共 6 次呼叫：3 次共用前處理加 3 次最終判斷。不改寫時共 5 次；前處理失敗可能減少前處理呼叫，但仍嘗試三組最終判斷。三組錯誤彼此隔離，最終錯誤為 error/null，不當作 False。

結果 schema 第 4 版，manifest、逐題及 summary 皆註記 bool_semantics=needs_full_report。保存科目清單及雜湊、題目、設定、提示詞、usage、費率快照及延遲。只有人工金標存在且完成布林判斷的題目計入 Accuracy、Precision、Recall、F1 及混淆矩陣；正類為「需要完整財報」。另列覆蓋率、錯誤數、前處理失敗數及實際使用原題／改寫題數。

actual_experiment_cost 只計共用前處理一次。judge_cost 為單組最終判斷費用；standalone_cost_estimate 為單獨部署的參考費用，A/B 各含共用前處理，C 僅自身判斷。三組部署試算不得相加當成本批實際費用。

SDK 自動重試關閉。費用按 usage 及 pricing.json 試算，未知 usage 或模型費率記為 null，不假定免費。完整科目清單較舊清單大，不能沿用舊實驗的單題費用。

## 舊結果

歷史 A～E 的資料、金標及報告不變。四組舊入口為 legacy_experiments.py，舊 E 入口為 experiment_luna.py；它們仍判斷 answerable，True=簡明財報足夠。舊設計存於 legacy_experiment_plan.md。新 summary 不接受混用舊版結果。
