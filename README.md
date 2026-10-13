# 財務問題資料庫分流

Python 3.11+。專題正式入口採 A 組：保護問題改寫原意、Jev 分流、調用外部資料庫／完整財報 agent。SQLite 查核與財務回答由其他 agent 負責。本專案提供交接介面及錯誤狀態。

GitHub 公開版本庫：[112ab0706/financial-question-router](https://github.com/112ab0706/financial-question-router)。組員取得專案、設定自己的 API key 與 Git 協作方式，請看 [SHARING.md](SHARING.md)。

## 專題正式 A 組入口

`main_a.py` 是單題分流入口；`group_a.py` 提供可接入系統的 `GroupARouter`、`AgentDispatcher` 與 `GroupASystem`。這條流程只執行 A，不會連帶執行 B、C。

1. 前端有傳入年度／公司參數時，先判斷回答是否需要、且原題是否缺漏。只有 `missing` 才由程式補入前端值；不依系統日期推算年度。
2. 以補入後的問題提取原文片段，查核 template 沒有捏造或替換內容。原題沒有需要補入的條件時沿用原題。
3. 判斷是否改寫；需要時產生候選改寫。
4. 本機先檢查明確年份、數字及幣別，再獨立複核公司、期間、單位、所有條件、操作、概念及新增假設。只有七項均為 true 才採用改寫。驗證基準為補入後的問題；改寫失敗仍保留已套用的參數。
5. Jev 同一次呼叫完成三項分流檢查。原題明示條件優先，補入題是查詢基準，驗證後的改寫輔助理解；三份文字都保留。
6. 選中資料庫 agent 或完整財報 agent。資料庫 agent 自行查核實際資料；若回報不足，交給完整財報 agent 一次。完整財報也不足則明確回報，不循環調用。

```powershell
.\.venv\Scripts\python.exe main_a.py "甲公司2025年的營收是多少？"
.\.venv\Scripts\python.exe main_a.py "甲公司2025年的營收是多少？" --log test_results/group_a_single.json
.\.venv\Scripts\python.exe main_a.py "營收是多少？" --latest-year 2025 --current-company "台積電"
.\.venv\Scripts\python.exe main_a.py --input-json examples/group_a_frontend_request.json
```

成功 stdout 是完整 JSON，包含 `status`、`route`、`needs_full_report`、原題、前處理與驗證結果、三項機率／旗標、門檻及科目雜湊。`route=database` 表示交給資料庫 agent 查核及回答，並非已確認 SQLite 資料存在。`data_availability=delegated_to_database_agent` 明示查核責任。最終模型失敗輸出 `status=error`、`route=null`、`needs_full_report=null`，結束碼 1；設定／清單／金鑰錯誤只在 stderr 顯示安全錯誤類型。CLI 只產生分流交接資料，實際 agent 函式由 Python 系統入口接入。

`group_a_config.json` 沿用 A 的模型設定，另有 `rewrite_check` 指定複核模型、`context_check` 指定參數必要性判斷模型。不帶前端參數時，不需要改寫共 3 次呼叫，一般改寫共 5 次，本機提早拒絕改寫共 4 次。帶任一前端參數時增加一次 `ContextRequirements` 判斷；其成本及使用量也包含在 `--log`。SDK 自動重試固定停用。新流程版本為 `group_a_context_v2`；帶前端參數的 Jev 判斷為 `jev_three_checks_frontend_context_v4`，不帶參數沿用 `jev_three_checks_original_context_v3`。歷史實驗結果維持原版本，不能直接當作新流程的成績。

### 前端輸入與參數補入

前端可傳入以下 JSON，Python 入口 `route_group_a`、`GroupARouter.route`、`GroupASystem.run` 及整合範例的 `run_question` 均可接收此 dict 或 `GroupAInput`：

```json
{
  "original_question": "營收是多少？",
  "latest_year": 2025,
  "current_company": "台積電"
}
```

`latest_year` 是前端提供的最新資料年度，使用西元整數（不傳 `"2025"` 字串）；`current_company` 是非空的公司名稱或代號。缺少選項時可傳 `null`，但若判斷為必要而缺漏卻沒有參數值，流程回報 `MissingContextParameters`，不調用 agent。只傳原題字串仍可執行既有流程，會略過參數補入。

| 原題 | 前端年度 2025、公司台積電時的行為 |
| --- | --- |
| 營收是多少？ | 補年度與公司 |
| 鴻海2024年的營收？ | 保留原公司與年度，兩者都不補 |
| 台積電營收是多少？ | 僅補年度 |
| 2024年營收是多少？ | 僅補公司 |
| 第二季營收？ | 補年度與公司，保留第二季 |
| 去年／最近三年營收？ | 保留相對期間，可補公司，不換成單一年度 |
| 哪一年營收最高？ | 保留年度搜尋，可補公司 |
| 哪家公司營收最高？ | 保留公司搜尋，可補年度 |
| EPS是什麼？ | 不補年度與公司 |

必要性由模型按完整語意判斷，不能只看 `Time`／`Filter` 是否空值。程式只附加獲准的前端參數，模型不能自行產生參數值。例如兩者都補入時，`contextual_question` 為：

```text
營收是多少？
（系統補入查詢條件：年度：2025年；公司：台積電）
```

輸出分別保存 `original_question`（原題原字串）、`context_resolution`（前端值、必要性判斷、實際套用值）、`preprocessing.contextual_question`（補入基準）及 `preprocessing.question_used`（改寫成功或回退後的交接題）。未改寫但有補入時 `outcome=contextual`。參數判斷服務失敗、拒答或證據不是原文時，`status=error`、`error_stage=context_resolution`，不猜測套用，也不調用 Jev／agent。

### 接入其他 agent

可直接使用 [examples/group_a_integration.py](examples/group_a_integration.py) 的 `run_question(question, database_agent=..., full_report_agent=...)`；傳入兩個已存在的 Python 函式即可。每個函式接收 `AgentRequest`，以 `request.contextual_question` 為查詢基準，`request.question_used` 輔助理解；`request.original_question` 保留原字串。`request.applied_year`／`request.applied_company` 只包含實際補入的值，不能以未採用的前端值蓋掉原題。另有 `request.task`、`request.routing` 及 `request.previous_attempts`，後者保留前一個 agent 的資料不足原因。

agent 回傳 `AgentResponse` 或相同結構的 dict：

| status | answer／reason | 系統行為 |
| --- | --- | --- |
| completed | answer 必須存在，可為文字或 JSON，0 也是有效答案 | 交付答案 |
| needs_full_report | reason 說明缺少的資料 | 資料庫分支轉完整財報；完整財報分支停止並回報資料不足 |
| insufficient_data | reason 說明缺少的資料 | 與上述資料不足處理相同 |
| error | reason 說明服務錯誤 | 停止，回報 agent_error；服務錯誤不冒充資料缺漏 |

```python
from examples.group_a_integration import run_question

# database_agent 與 full_report_agent 是組員提供的函式。
# 函式接收 request，成功可回傳 {"status": "completed", "answer": "答案文字"}；
# 資料不足可回傳 {"status": "needs_full_report", "reason": "指定年度缺少客戶明細。"}。
result = run_question(
    {"original_question": "兩大客戶的營收差額是多少？", "latest_year": 2025, "current_company": "台積電"},
    database_agent=database_agent,
    full_report_agent=full_report_agent,
)
print(result.model_dump_json(indent=2))
```

長期運行的服務可用既有 OpenAI／TypeSafe client 建立 `GroupARouter`，再組裝 `GroupASystem(router, AgentDispatcher(...))`，重用連線而讓每題各自記錄呼叫。尚未提供被選中的 agent 時，dispatcher 回傳 `awaiting_agent`，不會假稱已取得答案。原始分流保留於 `routing.route`，實際完成／待接入的 agent 路徑在 `final_route`，交接歷程在 `attempts`。

改寫驗證是保護流程，不保證模型永不判錯。本機提早拒絕時，尚未查核的驗證欄位為 null。離線測試涵蓋錯改公司／年份／客戶／計算方式、複核失敗回原題、分流失敗不調用 agent、資料不足轉交、agent 格式／服務錯誤、完整財報不足停止與成本紀錄。

## 保留的原題 GPT 入口

以下流程及 `main.py` 是既有原題 → GPT-6 Luna 入口；專題 A 使用上面的 `main_a.py`。

1. 載入 database/2025q4_accounting_items.md，包含資產負債表 863、綜合損益表 555、現金流量表 272、權益變動表 53，共 1,743 筆科目。
2. 將未改動的原題及完整科目清單交給 GPT-6 Luna（low），只呼叫一次 OpenAI。
3. 檢查模型引用的科目 ID 確實存在，輸出是否需要完整財報。

**True = 需要完整財報；False = 資料庫科目足夠。** 這與歷史實驗的 answerable 方向不同。舊人工答案與正確率不能直接用來評估新分流。

此原題 GPT 入口不提取 template、不判斷 status、不改寫題目。原題的公司、期間、產業、條件、單位與分析操作須保留。缺少客戶、部門、地區等明細時，不能用合計取代。四大報表內的科目可以用於運算、排序、比率及跨期比較。

清單是2025Q4觀察到的科目聯集，不表示每家公司、期間及維度都有資料。此版本先做科目與粒度的分流，尚未連接組員的 SQLite。資料庫 agent 仍須查核實際資料；不能把沒有資料當成零。Python 入口提供 availability_check，可在科目判定足夠後檢查必要資料：回傳 False（缺少或尚無法確認）就改為需要完整財報。未提供查核函式時，成本紀錄標示 data_availability=unchecked。True 表示轉交完整財報，不保證完整財報必定能回答。

## 設定與金鑰

- routing_config.json：judge.model 與 judge.reasoning_effort 設定新入口的判斷模型，預設 gpt-6-luna low。
- prompts.py：DATABASE_ROUTING_SYSTEM_PROMPT / DATABASE_ROUTING_TASK 是資料庫分流規則。reason 固定要求繁體中文，英文原題也相同。
- database/2025q4_accounting_items.md：使用者提供科目清單的專案內快照，保留中文科目、XBRL 元素、命名空間與出現報告數。模型輸入保留全部科目涵義及報表分類，同名而不同義的項目補充 XBRL 元素。OpenAI 另提供科目 ID 以回傳必要科目，相同名稱與元素的 ID 合併一列；Jev 只需二元判斷，因此省略 ID 以降低 token 數。不以關鍵字刪減候選科目。完整 QName 仍由本機 ID 對應，完整版與各模型實際判斷用版本都保存在實驗 manifest。
- .env：新入口只需要 OPENAI_API_KEY，可使用 OPENAI_API_KEY="你的金鑰"。ABC 與歷史 Jev 實驗另需要 TYPESAFE_API_KEY。不要把金鑰寫入程式或提交到版本庫。
- OpenAI reasoning_effort 省略或填 null 時不傳推理參數，使用模型預設。換成不支援此參數的模型時需移除或填 null。

## 保留的 A、B、C 三組

三組均依同一份資料庫科目清單判斷是否需要完整財報，True=需要完整財報，False=資料庫科目與粒度足夠。

| 組別 | 最終判斷題目 | 判斷模型 |
| --- | --- | --- |
| A | 共用前處理選定的改寫題或原題 | Jev（jev-latest） |
| B | 與 A 完全相同的題目 | GPT-5.4 mini（medium） |
| C | USER 原題 | Jev（jev-latest） |

experiments.py 是新版 ABC 入口。A/B 共用一次提取（GPT-4.1 mini）、status（GPT-6 Luna low）與必要改寫（GPT-5.4 mini medium）；no_rewrite 或前處理失敗時，兩組都改用相同原題繼續最終判斷。C 不使用前處理產出的題目。main.py 仍是獨立的原題 → GPT-6 Luna 單題入口，沒有把原本 C 組換成 Luna。

新版 Jev（jev_three_checks_v2）同一次呼叫包含三項 Noul：missing_item（缺必要科目）、missing_detail（缺必要明細）、outside_catalog_information（需要科目以外資訊）。每項機率採 >= config.json 的 step3.yes_threshold，預設 0.5；程式以 OR 合併，任一項成立就轉交完整財報。使用較短英文指令，保留完整科目涵義與報表分類；來源日期與未查核資料列等稽核資訊另存 manifest，明確區分科目能力及實際資料可用性。三項機率不合成整體機率。B 仍使用原有 OpenAI 指令及繁體中文理由，兩種判斷器分別版本化。

## 只重測 Jev 的 A/C

experiment_jev_ac.py 重用來源實驗的選定題目：A 使用保存的 shared_preprocessing.question_used，C 使用 original_question。來源 20 題中有 16 題改寫、4 題保留原題，重測均維持原字串。此入口只需 TYPESAFE_API_KEY，不提取、不判斷 status、不改寫、不執行 B，也不呼叫 OpenAI。

```powershell
.\.venv\Scripts\python.exe experiment_jev_ac.py --source-run test_results/ABC_database_20_en_20261007_01 --gold test_results/ABC_database_20_en_20261007_01/evaluation/gold.json --output test_results/AC_jev_v2_new
```

每題兩次 Jev 呼叫，每次同時問三項，20 題共 40 次 API 呼叫及 120 項子判斷。人工答案只在程式評分，不傳給 Jev。新費用只計新 Jev 呼叫，前處理沿用原紀錄且不重複計費；此費用不等於完整 A 流程的部署成本。來源紀錄保持不變，以 SHA-256 核對；新結果保存每項機率、旗標、OR 規則、用量、成本、人工評分及舊結果對照。

人工標準欄位改為 gold_needs_full_report，true=需要完整財報。舊 gold_answerable 資料集會被拒絕，避免套用方向及資訊範圍不同的人工答案；不自動反轉舊答案。範例 examples/database_experiment_questions.json 先填 null，未標註時只記錄結果與成本，不計正確率。

```powershell
.\.venv\Scripts\python.exe experiments.py --question "公司兩大客戶占應收帳款的百分比是多少？"
.\.venv\Scripts\python.exe experiments.py --dataset examples/database_experiment_questions.json --output test_results/ABC_database_new
```

以上指令將進行付費實驗，須指定新的輸出目錄保留歷史結果。第 4 版 ABC 及第 5 版 AC 結果與 manifest 都明示 bool_semantics=needs_full_report，個別錯誤保留 error/null，不能當成 False。ABC 共用前處理成本在實際總帳只計一次，A/B 獨立部署試算各包含它；AC 重測只記新 Jev 成本。詳細規則見 experiment_plan.md。

## 執行單題

本專案已備有 .venv；一般環境先安裝 requirements.txt 的依賴，並從 .env.example 建立 .env。

```powershell
.\.venv\Scripts\python.exe main.py "甲公司2025年的營業收入是多少？"
.\.venv\Scripts\python.exe main.py "甲公司2025年的營業收入是多少？" --log test_results/routing_q001.json
```

成功 stdout 只有 True 或 False，結束碼 0。模型拒答、格式錯誤、引用不存在的科目、設定或清單錯誤時，stderr 顯示錯誤，stdout 為空，結束碼 1；不能將服務錯誤當成 False 而錯誤跳過完整財報。

可用 --catalog 指定相同格式的 Markdown 科目清單、--config 指定分流模型設定。--log 保留 usage、成本與分流理由，stdout 仍只有 bool。實際執行 main.py 會呼叫付費 API；Jev 的 AC 重測使用 experiment_jev_ac.py。

## 歷史流程：status 與共用題目

以下為舊版 answerable 的 A～E 重現入口及 prompt。四組舊版入口改存 legacy_experiments.py；歷史結果沒有改寫或重跑。config.json 的 step1/status_check/step2/step3 依序為 gpt-4.1-mini、gpt-6-luna low、gpt-5.4-mini medium、jev-latest，供新版 ABC 及舊流程使用；experiment_config.json 的 openai_judge 為 GPT-5.4 mini medium。

歷史 QuestionPipeline 為提取 → 選擇是否改寫 → 必要改寫 → 最終判斷。answerable=True 表示簡明財報足夠，False 表示不足。所有有效原題仍嘗試最終判斷；前處理失敗使用原題繼續。

status 回傳格式：

```json
{"status": "no_rewrite", "Context_Gap": "原題已適合直接使用，保留原文交給最終判斷器。"}
```

rewrite 才會呼叫改寫模型，格式為：

```json
{"rewritten_question": "甲公司2025年度的營業收入是多少？", "Context_Gap": "營收整理為營業收入，保留原公司及年度。"}
```

rewritten_question 可為 null，表示使用原題繼續。QuestionPipeline.prepare_question() 回傳實際使用的 question_used、template、classification、rewrite、outcome 及回退原因。outcome 為 rewritten 或 original；reason 可為 no_rewrite、cannot_rewrite 或 preprocessing_error。記錄回退原因不等於做可回答性的布林判斷。

## 歷史四組實驗

| 組別 | 最終輸入 | 判斷器 |
| --- | --- | --- |
| A | 共用前處理選定的改寫題或原題 | Jev |
| B | 與 A 完全相同的題目 | OpenAI |
| C | 原題 | Jev |
| D | 原題 | OpenAI |

A/B 每題每輪只提取、選擇是否改寫及必要改寫一次，留下相同內容與雜湊。所有四組均嘗試最終判斷，包括 no_rewrite、改寫失敗或前處理錯誤的題目；個別判斷器錯誤不影響其他組。

```powershell
.\.venv\Scripts\python.exe legacy_experiments.py --question "甲公司2025年的營業收入是多少？"
.\.venv\Scripts\python.exe legacy_experiments.py --dataset examples/experiment_questions_20_en_labeled.json --repeat 1 --output test_results/legacy_new_experiment
```

輸出目錄必須是新目錄；省略 --output 時建立時間命名的目錄。資料集為 question_id、question、gold_answerable 的 JSON 陣列；gold_answerable 可以省略或為 null。人工標準只有最後的 bool 即可，僅用於評分，不傳給模型。

結果包含 manifest.json、每題每輪 JSON 與 summary.json。第 3 版結果記錄 pipeline_architecture=optional_rewrite、實際使用問題、改寫或保留原題的原因，以及前處理錯誤。成功的最終判斷為 completed / bool，判斷器失敗為 error / null。本輪不會新產生 rejected 或 cannot_rewrite 停止狀態；仍可解讀過去實驗的停止紀錄，歷史報告不代表新流程的表現。

正確率、Precision、Recall、F1 及混淆矩陣只計有人工答案且完成布林判斷的題目。另列判斷覆蓋率、最終判斷錯誤數、前處理錯誤數、改寫輸入數與原題輸入數。舊版設計存於 legacy_experiment_plan.md，其中舊四組命令請使用 legacy_experiments.py。

### 單獨的 E 組

E 直接把英文原題交給 GPT-6 Luna（low）做最終判斷，與 C/D 使用相同欄位及判斷提示詞。此入口只需要 OPENAI_API_KEY，不呼叫提取、status、改寫或 Jev，也不重跑 A～D。

```powershell
.\.venv\Scripts\python.exe experiment_luna.py --output test_results/E_new_run
```

預設使用 examples/experiment_questions_20_en_labeled.json；--dataset 可替換題目。experiment_config_E.json 設定 E 的模型與推理強度，可用 --judge-config 指定其他設定檔。結果與費用另存在新目錄。

## 歷史實驗的財報欄位範圍

financial_report_fields.json 是本專案採用的保守清單，選取政府資料開放平臺[上市公司綜合損益表（一般業）](https://data.gov.tw/dataset/91998)及[上市公司資產負債表（一般業）](https://data.gov.tw/dataset/94831)的欄位子集，並非所有行業或所有簡明報表的完整標準。

預設排除現金流量表、附註、股價、客戶明細及產業別等資訊。模型判斷只依欄位及 scope，不假設未列出的欄位，也不要求提供實際數值或查詢特定公司期間的資料列。examples/report_fields.json 另示範限定範圍；可替換成自己的實際欄位清單。標準答案亦應與同一資訊範圍對齊。

## 成本紀錄

每次呼叫記錄實際回傳版本、提示與設定雜湊、原始 usage、延遲及結果或錯誤類型，不保存金鑰或 HTTP 原始錯誤 body。費用按 pricing.json 試算並存費率快照。

新入口的紀錄增加 bool_semantics=needs_full_report、pipeline_architecture=original_database_routing、必要科目 ID、繁體中文理由、科目清單 SHA-256 及資料可用性查核狀態，以區分新舊布林含義。整份科目清單會增加輸入 tokens，不能直接套用先前 E 組的小清單費用。

整批實驗的 actual_experiment_cost 只計共用前處理一次。每組 judge_cost 是判斷器費用，standalone_cost_estimate 是每組獨立執行的參考費用，A/B 各含前處理，四組參考費用不可直接加總當作實際花費。前處理失敗的成本同樣記錄。

費用依輸入、快取讀取／寫入及輸出 tokens 計算，推理 tokens 已包含在總輸出，不重複加計。缺少 usage 或未知版本費率時成本為 null，另保留已知小計及未知成本呼叫數。使用 --log 或實驗入口時停用 SDK 自動重試。費用為 usage 試算，非帳單確認金額。

## Python 入口與離線驗證

```python
from pathlib import Path
from dotenv import load_dotenv
from models import RoutingConfig
from database_catalog import load_catalog
from database_routing import needs_full_report

load_dotenv()
config = RoutingConfig.model_validate_json(Path("routing_config.json").read_text(encoding="utf-8"))
catalog = load_catalog(Path("database/2025q4_accounting_items.md"))
use_full_report = needs_full_report("甲公司2025年的營業收入是多少？", catalog, config)
# 科目足夠的 False 分支交給資料庫；True 分支交給完整財報 agent。
# 若已有資料庫查核函式，可傳 availability_check=your_database_check。
# 該函式接收原題與必要 AccountingItem 清單，只有資料確認齊全才回傳 True。
```

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

測試使用真正 SDK 與離線 HTTP 回應，驗證四大報表科目完整性、QName 身分保留、原題只傳一次、新布林方向、資料缺少時改交完整財報、無效結果與成本紀錄，並保留歷史流程的回歸檢查。沒有付費 API 呼叫。這些檢查驗證程式行為，不代表新 prompt 的判斷準確率。
