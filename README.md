# Agent Relay

Agent Relay 把 Windows 上的插件、MCP 工具與遠端 Linux Worker 接起來，讓本地助手把工作交給指定的遠端環境執行，再取回可核對的結果。它也整理了報告撰寫、背景工作、任務路由與執行紀錄所需的共用程式，讓這些工具可以從同一個來源維護、打包和重建。

這個 repository 是共用工具的開發來源。研究專案的資料、環境、工作目錄與執行狀態仍由各研究專案管理；安裝包不攜帶研究資料或帳號金鑰。

## 怎麼工作

1. 在 Windows 客戶端選擇已配置的 workplace 與 Linux Worker。Harness 先核對兩端的 release、工具契約和 workplace 身份。
2. 本地助手透過 Harness MCP 提交終端命令或遠端 Codex 工作。Worker 在指定的工作目錄執行，留下 command ID、狀態和回執。
3. 客戶端用同一個 ID 查詢進度、恢復連線並取回結果。傳送結果不確定時，先查原 ID，避免重複執行。
4. 文件與執行結果透過 Harness 的 artifact／回執機制交付，核對來源與雜湊後，再進行本地檢視或後續處理。

Report Writer 可以根據已整理的證據撰寫 Markdown；Background 管理自己的背景規則與佇列。它們共用同一套 Provider 配置和模型 preset。Lifecycle 與路由整合負責工具間的工作生命週期與訊息接線，Token Guard 提供大型命令輸出的有界紀錄與可核對回執。

## 專案裡有哪些部分

| 部分 | 用途 | 維護來源 |
| --- | --- | --- |
| Core / Harness | 遠端命令、Worker 派送、身份檢查、狀態查詢及 artifact 交付 | `solution_agent/`、`scripts/harness/` |
| Harness 插件 | 把既有 Harness 工具提供給本地 MCP 客戶端 | `plugins/solution-agent-harness/` |
| Lifecycle / Routing / XATS | 任務生命週期、路由與訊息服務整合 | `components/lifecycle/`、`components/goal-routing-integration/`、`components/xats-routing-lab/` |
| Report Writer | 證據檢查、報告撰寫與 Markdown 交付 | `plugins/research-report-writer/` |
| Report Provider | Writer 與 Background 共用的傳輸、配置與版本化模型 preset | `packages/report-provider/` |
| Background | 背景工作規則、佇列、狀態及 Windows 啟動入口 | `tools/research-background/` |
| Token Guard | 大型輸出的有界擷取、雜湊與回執 | `plugins/research-token-guard/` |

既有 `solution_agent` namespace、插件名稱與 MCP 工具名稱保留。新增功能時，先找到表中的來源修改，再重新打包；生成的安裝副本不作為永久修改來源。

## 這次整理完成了什麼

原先分散在工具與部署目錄中的程式，現在有獨立、可追溯的開發來源。主要模組沿用既有布局，Writer 與 Background 的 Provider 已抽成真正共用的模組；部署配置可以放在機器私有位置，依賴按 Windows 客戶端與 Linux Worker 分別重建。

目前可以產生客戶端來源包、Worker 安裝包及獨立 Writer 插件包。安裝器會檢查包的雜湊和目標目錄，客戶端包帶有配對所需的 Harness release manifest。取消工作時的競態已修復；原先依賴私人研究目錄的九項測試已改用完整的合成測試資料並通過，保留原有行為檢查。

乾淨安裝也補齊了之前遺漏的依賴：goal-routing 現在有完整的 registry 鎖檔並由客戶端安裝器安裝，XATS 鎖檔與精確版本一致。Linux 已實測重新下載依賴、重建 SQLite 原生模組、建置 Router／XATS，以及獨立 Writer 插件啟動。這讓新的測試目錄可以重建這些元件，不必借原開發目錄的依賴。

## 從來源開始

Windows 開發與打包使用 Python 3.13、Node 24、Git、npm 與 pnpm 11.25。Linux Worker 使用 Python 3.13；SSH、tmux、systemd user 與 Codex CLI 的需求取決於選用的執行方式。XATS 的 SQLite 原生模組需要在目標 OS、架構與 Node ABI 下建置。

取得 repository 後，在根目錄執行：

```powershell
python scripts/release/install_dependencies.py --role windows-client
python scripts/release/build_candidate.py
```

依賴安裝到專案自己的環境。打包前會核對來源追溯與發布邊界，產物位於 `dist/p1-candidate/`：

| 產物 | 使用方式 |
| --- | --- |
| `windows-client-source.zip` | 解壓到新的客戶端目錄，再安裝客戶端依賴並配置插件；包含配對 manifest |
| `harness-worker.zip` | 用隨附的 bootstrap 安裝器安裝到新的 Worker 目錄，再重建 Linux 依賴 |
| `research-report-writer.zip` | 獨立 Writer 插件包，包含共用 Provider、SDK 與依賴授權文件 |
| `install_bootstrap_release.py` / `install_overlay.py` | 分別處理全新目錄安裝與受控 overlay 更新 |
| `candidate.json` | 記錄來源 commit、runtime identity、產物雜湊和驗收狀態 |

這些產物還需要機器配置與插件接線。完整的安裝、dry run、更新和回退步驟見[安裝指南](docs/engineering/phase-one/INSTALL.md)。

## 連線與私有配置

Harness 客戶端需要自己的 workplace 宣告、SSH 連線設定與 Worker binding。兩端的 `.solution-agent/workplace.id` 必須相同，客戶端 manifest 必須對應實際載入的 Worker release；只複製原始插件目錄不足以完成配對。現有 Launcher 使用控制目錄中的 `remote-workplaces/connection-policy.yaml` 與所選 workplace 宣告。

Worker 部署 profile 可參考 `config/harness_remote_deploy_profiles.example.json`。透過執行環境中的 `SOLUTION_AGENT_PROFILES_FILE` 選取私有配置，或使用既有私有 checkout 配置位置；各啟動入口需明確提供所需環境變數。範例值必須換成實際授權的目標。

Writer 需要允許寫入的目錄及 Provider 金鑰／環境檔。Background 需要自己的 vault、state、rules 與 credential 引用。這些設定、workplace 身份、執行 DB、佇列與回執不放進 Git，也不隨共用安裝包分發。

## 目前可以用到哪裡

目前是 **P1 工程候選版**。已完成隔離 Linux Worker 安裝，以及 M1 新客戶端／舊 Worker、M2 新／新、M3 舊客戶端／新 Worker 的 Harness 跨端檢查。這些檢查使用同一台 Windows 的隔離環境，涵蓋 Harness 元件，完整四情境驗收仍未完成。

M4 舊／舊的 Harness 更新與回退已在 Linux 隔離 namespace 跑通：保留原包身份與它看到的部署路徑，用私有 systemd 執行命令，完成原舊版 → 候選版 → 原舊版的啟用核對。原配置、SQLite 歷史與命令回執均保留，回退後也能查回候選版完成的命令。M2、M3 另已通過 artifact 存取、審核與 CAS 提交。Linux 又已通過 23 項 Writer／Background／Provider 與 77 項 Lifecycle／goal 測試，包含合成 mailbox 與 XATS 的實際訊息往返。兩端已從更新後 ZIP 完成乾淨安裝，並通過新身份的 M2 Harness 配對、結果回收與 artifact 提交。M1、M3、M4 也已對目前發布身份完成 Harness 隔離驗收：新客戶端連舊 Worker、既有客戶端新增 binding，以及原舊版 → 目前候選 → 原舊版更新／回退均有實際回執。回退保留十三份配置、歷史及結果檔案，並能查回两版的原命令。Windows 客戶端亦完成 XATS 全新依賴下載、原生 SQLite 與 77 項整合測試。整套支持範圍仍在最後核對；設備證據是同一台 Windows 的全新隔離安裝。現用部署未切換，GitHub 推送與 CI 執行尚未完成。

具體結果與剩餘工作見[工程狀態](docs/engineering/phase-one/STATUS.md)及[workplace 驗收紀錄](docs/engineering/phase-one/WORKPLACE_ACCEPTANCE.md)。維護前讀 [AGENTS.md](AGENTS.md)；後續範圍與分期見[計劃入口](docs/planning/README.md)。歷史抽取結果保存在 `docs/extraction/`，供來源追溯。
