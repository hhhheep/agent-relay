# Agent Relay

**讓本地 Codex 與遠端 Linux 工作環境協作，並把任務進度、文件和結果帶回本地。**

Agent Relay 是一套連接本地助手與遠端 Worker 的工具。你可以在本地安排工作，把命令或 Codex 任務交給指定的遠端環境執行，再查詢進度、檢視結果和接續處理。它適合工作主要在本地安排、程式與運算環境卻放在遠端主機上的使用者，尤其是需要反覆執行、整理證據與撰寫報告的工作。

## 這個工具包的特色

- **本地安排，遠端執行。** 透過 Harness MCP 連接已配置的 workplace，使用遠端既有的程式、依賴與工作目錄；本地助手可以提交終端命令或遠端 Codex 工作，並取回執行結果。
- **中斷後可以接續查詢。** 每次執行留下 command ID、狀態與回執。連線中斷或傳送結果不確定時，可以先查原 ID，確認工作是否已執行，避免把同一件事重做一遍。
- **文件和結果有來源可查。** Harness 以 artifact 與回執交付文件和輸出，支援雜湊核對、差異檢視及受控提交，讓助手與使用者能確認收到的是哪一份結果。
- **大型輸出也能保留證據。** Token Guard 擷取有界的輸出摘要，保留完整紀錄的雜湊與回執，方便後續核對，不必把大量終端文字反覆塞進對話。
- **報告工具共用模型配置。** Report Writer 根據整理好的證據撰寫 Markdown，Background 管理背景規則與工作佇列；兩者共用 Provider 配置與版本化模型 preset，減少重複維護。
- **任務與對話有明確對應。** Lifecycle 與路由元件串接任務狀態和訊息，透過 Codex 原生查詢與既有路由 ID 核對來源與去向，讓工作有可追蹤的對話上下文。
- **工具與你的工作環境分開配置。** 共用程式可以打包、安裝和重建；SSH、workplace、金鑰與工作資料由你自己的環境管理。換一個工作環境時，使用對應的私有配置接線。

## 可以怎麼用

Worker session 重用正在隔離工程分支開發：可選的固定 worker 配置沿既有 command store 預留容量，由程式核對前單證據後精確續接 session；管理迴圈僅派送已批准且完成 admission 的任務。合法但錯的 session ID 在模型請求前拒絕。Controller CLI 支援整批 scope 交接，先等待舊 manager 釋放原 queue owner，再原子轉移 epoch。三個 manager 程序／十任務的合成故障情境已有本地證據；目前仍需處置最新 Windows CI 失敗及核驗後續修復。這些功能尚未部署；真實 Desktop 自動換 chat 尚未驗收。範圍、回退與測試見[工程紀錄](docs/engineering/worker-session-binding-20261016/README.md)。

例如，你在 Windows 上與 Codex 討論一項程式修改，而程式的 Linux 依賴和測試環境位於遠端主機。Agent Relay 可以把測試工作交給那台主機，讓你在本地查詢進度、取回日誌與產物，再根據結果修改程式。需要形成報告時，可以整理證據後交給 Report Writer。

一次典型工作流程：

1. 配置並選擇自己的 workplace 和 Worker，核對兩端身份與工具版本。
2. 本地助手提交遠端命令或 Codex 工作，取得可持續查詢的任務 ID。
3. 查詢進度，遇到中斷時用原 ID 恢復查詢。
4. 取回文件、日誌或產物，核對回執與雜湊後進行檢視、修改或後續工作。

## 工具包裡有哪些部分

| 工具 | 提供的能力 |
| --- | --- |
| Harness / Worker | 遠端命令、Codex 工作派送、狀態查詢與 artifact 交付 |
| Lifecycle / Routing / XATS | 任務生命週期與訊息路由接線 |
| Report Writer | 證據檢查、Markdown 報告撰寫與交付 |
| Background | 背景工作規則、佇列與狀態管理 |
| Report Provider | Writer 與 Background 共用的模型傳輸、配置與 preset |
| Token Guard | 大型輸出的有界擷取與可核對紀錄 |

## 目前版本與驗證

2026-10-15 的維護修復已整合至工程分支 [`codex/phase-one-restructure`](https://github.com/hhhheep/agent-relay/tree/codex/phase-one-restructure)，程式提交為 [`91c88cd`](https://github.com/hhhheep/agent-relay/commit/91c88cd7c8181f3c21b2dc084f6ba26cdd737387)。本批改善原命令恢復、等待參數驗證、終端租約診斷、工作單元完成後的處理、MCP 斷線狀態及 Manager 健康判定，並修正 Windows 路徑別名造成的 Markdown 同步回執不一致。詳細範圍見[整合紀錄](docs/engineering/harness-integration-20261015/README.md)及[回饋處置索引](docs/HARNESS_V2_FIELD_FEEDBACK.md)。

該提交的 [GitHub Windows CI 已通過](https://github.com/hhhheep/agent-relay/actions/runs/38743576311)：Python 325 項通過、2 項既有 POSIX 平台跳過，Node 114 項通過；隱私掃描、來源追溯、原生依賴建構與來源候選包檢查也通過。

這批新 runtime 尚未重新執行 Linux 原生及 M1–M4 新舊版本安裝／更新矩陣驗收；先前 P1 的隔離驗收結果仍對應其記錄中的版本。本批尚未合併至 `main` 或部署至現用環境。

後續未結回饋核對補上了終端工作目錄身分檢查、互動輸入前的讀取視窗驗證，以及 acquisition／路由測試的公開來源依賴。本地擴充核心選集為 278 項通過、2 項既有平台跳過；候選沿[既有 GitHub CI](https://github.com/hhhheep/agent-relay/actions/workflows/p1-candidate.yml)驗證後才整合工程分支。詳見[逐項處置紀錄](docs/engineering/feedback-followup-20261015/README.md)，其中區分可重現的源碼缺陷、已有修復，以及仍需現場配置或安裝驗收的項目。

14 項歷史未結回饋的[逐項卡點與責任分類](docs/engineering/feedback-classification-20261015/README.md)區分源碼缺陷、未完成的交接／整合需求，以及環境／驗收缺口；後續源碼修復包含空命令輸出的預覽判定與 pending MCP 連線競態，不代表原現場已完成驗收。

文件發布現在可沿既有 `artifact_apply` 完成[宣告、草稿／packet、審閱、CAS 發布與讀回核對](docs/engineering/hf002-document-handoff-20261015/README.md)。支援本地 Markdown 全文或明確 SYNC 區塊，writer 忙碌時保留原 plan／審閱等待；ResearchFlow 仍交由原本的 record owner 發布，不能以本地回執冒充同步成功。

SSH 新連線現在沿用同一個[啟動限流與失敗冷卻來源](docs/engineering/hf019-start-budget-20261016/README.md)：多 chat 保持獨立 MCP stdio，既有維護 SSH／SCP 入口共用額度；不因限流或逾時自動改走其他通道。預設每 endpoint 十秒最多三次啟動，連續三次網路啟動失敗後停止新連線五分鐘。這是工程來源防護，尚未部署，也不代表已驗證學校封鎖政策或解除封鎖。

## 開始使用

目前提供 **P1 工程候選版**，主要部署組合是 Windows 客戶端與 Linux Worker。使用前需要配置自己的遠端連線、workplace 和插件入口；報告撰寫另需配置 Provider。

從來源重建 Windows 客戶端與安裝包，需要 Python 3.13、Node 24、Git、npm 和 pnpm 11.25。在 repository 根目錄執行：

```powershell
python scripts/release/install_dependencies.py --role windows-client
python scripts/release/build_candidate.py
```

產物位於 `dist/p1-candidate/`，包含 Windows 客戶端來源包、Linux Worker 包、獨立 Report Writer 插件包，以及安裝／更新工具。依照[安裝指南](docs/engineering/phase-one/INSTALL.md)完成依賴安裝、私有配置、兩端配對與啟動。

先前 P1 版本的 Windows／Linux 乾淨安裝及 Harness 更新／回退已完成隔離驗收。具體版本、實機覆蓋範圍及仍未執行的項目，見[工程狀態](docs/engineering/phase-one/STATUS.md)；目前工程分支的驗證範圍見上節，候選版仍需依自己的環境配置與驗證。

## 進一步閱讀

- [安裝、配置、更新與回退](docs/engineering/phase-one/INSTALL.md)
- [驗收結果與目前狀態](docs/engineering/phase-one/STATUS.md)
- [維護規則與程式來源](AGENTS.md)
- [開發計劃](docs/planning/README.md)
