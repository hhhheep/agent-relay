# Extracted entrypoints

All source below is in this project. No live profiles, credentials or state were copied.

| Component | Source/build entry | Verification |
| --- | --- | --- |
| Core/Harness | `solution_agent/`, `plugins/solution-agent-harness/scripts/server.py --check` | isolated Python and MCP self-check, 11 tools |
| Harness packaging | `scripts/harness/build_deployment_bundle.py` + `deploy/overlay-files.txt` | 160-file local ZIP; no deployment |
| Lifecycle/router | `components/lifecycle/build-router-package.mjs` | 43-file bundle, 16 dependency licenses; 77 router/goal tests |
| XATS service | `components/lifecycle/production-xats-service-20261008.ts` + selected upstream | `scripts/extraction/build-local.mjs`; native SQLite in memory; no production service started |
| Writer/provider | `plugins/research-report-writer/server.mjs`, `lib/provider.mjs` | 9 tests; current installed helper/profile preserved |
| Token Guard | `plugins/research-token-guard/skills/research-token-guard/scripts/` | 37 tests |
| Background | `tools/research-background/service.mjs`, `cli.mjs`, `provider.mjs` | 12 mock tests; synthetic empty vault config |
| Windows launcher | `tools/research-background/NoConsoleLauncher.cs` | compiled WinExe only; no scheduling/startup registration |

The development Windows script uses a separate task/startup identity and was not executed. RPC paths now discover this project's single Python package. Third-party Node dependency junctions point only inside this project.

## 整套系統的功能範圍（2026-10-11 補充核對）

`agent-relay` 是本次共用系統來源的暫定 project 名稱。範圍包含 Python 核心、MCP servers、路由／管理器、worker、資料交付、Writer、Guard 與背景工具。來源納入不等於現用安裝已遷移，也不等於每項 legacy 或第三方能力已驗收。

| 功能 | 已納入的來源／入口 | 本次驗證與界線 |
| --- | --- | --- |
| Harness MCP | `solution_agent/harness/mcp_server.py`、`plugins/solution-agent-harness/.mcp.json` | 11 個工具可自檢；stage_execute/status/collect、terminal_open/exec/send/read/close、artifact_inspect/store/apply。現用 SSH profile 不納入，不能由自檢推定遠端執行通過 |
| Writer MCP | `plugins/research-report-writer/server.mjs`、`.mcp.json`、`lib/`、`remote/` | 6 個工具：provider_status、remote_writer_status、validate_evidence_packet、draft_research_report、validate_remote_evidence_packet、draft_remote_research_report。9 項隔離測試；不呼叫付費 API／SSH。通用 MCP 宣告已補上，實際 host、roots、credential override 需在後续安裝配置 |
| Lifecycle／manager／session 接回 | `components/lifecycle/` | 路由、controller authority、inbox、help mailbox、publication、resume、App adapter／RPC 與 SessionStart 入口來源；router／goal 77 項測試。現用 hooks、lifecycle/service config 未搬移或改綁 |
| XATS 通訊與 MCP 上游 | `components/lifecycle/production-xats-service-20261008.ts`、`components/xats-routing-lab/upstream/` | 已納入 daemon、MCP transport、CLI/App dispatch 相關來源及 channel plugin／授權。僅驗證本機 service 構建與 SQLite memory smoke；沒有宣稱所有上游 transport/channel 能力均已構建／端到端驗收 |
| 遠端 Codex worker／命令管理 | `solution_agent/harness/codex_worker.py`、`terminal_commands.py`、`remote_terminal.py` 等 | 含傳輸、worker、等待／lease／恢復所需來源與部署腳本。未重連遠端驗證或遷移 Linux runtime；取消命令仍有 1 項已記錄失敗 |
| Artifact Clipboard／Markdown 交付 | `solution_agent/harness/artifact_runtime.py`、`artifact_apply.py`、`task_capsule.py`、`task_md_sync.py`、`md_snapshot.py`、`md_sync.py` | 保留 task-scoped artifact、hash／provenance、CAS／apply 及任務 Markdown 同步程式。它與 OS clipboard 不同；研究 Markdown Phase 0 snapshot／sync plan 不能稱為完整 vault 雙向同步 |
| Token Guard | `plugins/research-token-guard/skills/research-token-guard/` | Skill／Python CLI、bounded receipt、原始輸出保存與 stage ledger；37 項測試。它不是另一個獨立 MCP server |
| 背景整理／provider／Windows launcher | `tools/research-background/`、Writer provider helper | 12 項 mock 測試；Windows launcher 只編譯，開發配置只指向本 project 空白 fixture。現用 DB、vault、排程與 startup 綁定保持原位 |
| 其他 core／adapter | `solution_agent/agent/`、`orchestrator/`、`continuation/`、`execution_port/`、`remote_materializer/`、`small_task_worker/`、`registry/` 等 | 來源保全，不代表全部舊 CLI／dashboard／研究 prototype 已驗收或承諾持續維護；正式正規化時再明定現用／legacy 邊界 |
| 版本／構建／部署 | `scripts/harness/`、`deploy/`、`scripts/extraction/` | 保留 manifest／build／profile／overlay helpers；本次只產生隔離候選構建，未安裝／註冊／部署 |

### 刻意保留在原環境的部分

- Codex hooks/config、插件註冊與 cache、workplace 身份、實際本機／遠端路徑與 SSH／provider 設定。
- 運行中的資料庫、queue、leases、task/command/artifact 狀態、研究 vault／原文和 credentials。
- Codex／Obsidian／SSH／tmux／systemd 等宿主軟體；其適配來源不等於宿主本身已被收進 project。
- 平台提供或另外安裝的 GitHub、Drive、clipboard 等 MCP connectors，以及其他個人 skills；它們不是本次已盤點的共用程式維護來源，未整批複製。

因此本次交付是上述來源的獨立工程基線，完整現用系統接線與切換仍屬後續階段。正式整理應按功能／協議／部署元件建立索引，不能只以 `solution_agent/` 目錄代替全系統清單。
