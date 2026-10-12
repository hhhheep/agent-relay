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

## 開始使用

目前提供 **P1 工程候選版**，主要部署組合是 Windows 客戶端與 Linux Worker。使用前需要配置自己的遠端連線、workplace 和插件入口；報告撰寫另需配置 Provider。

從來源重建 Windows 客戶端與安裝包，需要 Python 3.13、Node 24、Git、npm 和 pnpm 11.25。在 repository 根目錄執行：

```powershell
python scripts/release/install_dependencies.py --role windows-client
python scripts/release/build_candidate.py
```

產物位於 `dist/p1-candidate/`，包含 Windows 客戶端來源包、Linux Worker 包、獨立 Report Writer 插件包，以及安裝／更新工具。依照[安裝指南](docs/engineering/phase-one/INSTALL.md)完成依賴安裝、私有配置、兩端配對與啟動。

Windows／Linux 的乾淨安裝及 Harness 更新／回退已完成隔離驗收。實機覆蓋範圍、版本相容性與仍未執行的項目，見[工程狀態](docs/engineering/phase-one/STATUS.md)；候選版仍需依自己的環境配置與驗證。

## 進一步閱讀

- [安裝、配置、更新與回退](docs/engineering/phase-one/INSTALL.md)
- [驗收結果與目前狀態](docs/engineering/phase-one/STATUS.md)
- [維護規則與程式來源](AGENTS.md)
- [開發計劃](docs/planning/README.md)
