# 組員取得與協作

這個專案已建立 Git 版本庫，預設分支為 `main`。專題使用 `main_a.py` 與 `group_a.py` 的 A 組流程。問題改寫與 agent 交接在本專案處理；SQLite 查核及實際答案由組員的 agent 負責。

## 取得專案

共同的 GitHub 公開版本庫：[112ab0706/financial-question-router](https://github.com/112ab0706/financial-question-router)。組員可直接下載，或在 PowerShell 中取得含版本歷史的專案：

```powershell
git clone https://github.com/112ab0706/financial-question-router.git
cd financial-question-router
```

需要推送修改的組員，請由版本庫擁有者在 GitHub 的 Settings → Collaborators 加入協作者；也可以先 fork，再送出 Pull Request。

### 離線分享

`output/share/financial-question-router.zip` 是程式原始碼壓縮包，解壓即可閱讀及執行。它不包含 Git 歷史。

`output/share/financial-question-router.bundle` 包含 Git 歷史，適合組員建立自己的版本庫。在 PowerShell 中執行，將路徑換成收到檔案的位置：

```powershell
git clone "C:\收到檔案的位置\financial-question-router.bundle" financial-question-router
cd financial-question-router
```

bundle 是建立當下的版本快照，不會自動同步。從 bundle clone 的組員，`origin` 起初是 bundle 檔案位置；請改成共同的 GitHub 網址：

```powershell
git remote set-url origin https://github.com/112ab0706/financial-question-router.git
```

## 安裝與設定

需 Python 3.11+。每位組員建立自己的環境及金鑰設定：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

在自己的 `.env` 填入 `OPENAI_API_KEY` 與 `TYPESAFE_API_KEY`。分享版本只提供空白的 `.env.example`；`.env`、其他 `.env.*`、私鑰檔、虛擬環境、SQLite、實驗輸出及個人 Codex 設定已列入 Git 忽略規則。不要使用 `git add -f` 強制加入這些檔案。

專題 A 入口（會呼叫模型 API）：

```powershell
.\.venv\Scripts\python.exe main_a.py "甲公司2025年的營收是多少？"
```

agent 接入方式見 [README](README.md) 與 [接入範例](examples/group_a_integration.py)。

## 提交與協作

開始一項修改時建立分支；分支名稱依實際工作選擇：

```powershell
git switch -c feature/agent-integration
git status
```

完成修改後跑離線測試，檢查差異，再提交：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
git diff
git add group_a.py
git commit -m "Improve agent integration"
```

上述檔案名稱及提交訊息是範例，請換成自己實際修改的內容。有協作者權限時可推送自己的工作分支，再到 GitHub 建立 Pull Request，請組員審查並合併：

```powershell
git push -u origin feature/agent-integration
```

開始新的工作前先更新 `main`：

```powershell
git switch main
git pull --ff-only origin main
```

需要離線交換修改時，可由各組員產生新的 bundle 交換提交。更新 bundle 時只輸出已提交內容：

```powershell
git bundle create "自己的分享檔案.bundle" --all
```

## 重新產生分享檔案

原始碼壓縮包與 bundle 都取自 Git 已提交版本；未提交的修改不會包含在內。更新程式並提交後再執行：

```powershell
New-Item -ItemType Directory -Force output/share | Out-Null
git archive --format=zip --output=output/share/financial-question-router.zip HEAD
git bundle create output/share/financial-question-router.bundle --all
git bundle verify output/share/financial-question-router.bundle
```

Git 管理檔案版本，金鑰與執行環境由每位組員各自設定。歷史實驗結果留在維護者本機；需要分享實驗成果時另外挑選所需報告，避免混入個人設定或認證資訊。
