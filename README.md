# Agent Relay — P1 engineering candidate

First restructuring is implemented on the independent source, with existing plugin/MCP identities and state contracts retained. Isolated Linux Worker installation and Windows × Linux Harness checks now pass for M1–M3; whole-system acceptance remains partial and original-package M4 activation is blocked by its hash-bound deployment root. See [workplace evidence](docs/engineering/phase-one/WORKPLACE_ACCEPTANCE.md). No live installation was switched.

Start with [installation/update/extension instructions](docs/engineering/phase-one/INSTALL.md), [engineering status and acceptance matrix](docs/engineering/phase-one/STATUS.md) and [authority/migration decisions](docs/engineering/phase-one/DECISIONS.md). Run `python scripts/release/build_candidate.py` for checked source-only client/Worker artifacts; dependencies are rebuilt at the destination. Shared provider authority is `packages/report-provider/`, consumed by Writer and Background.

Historical extraction details follow; their test results and limitations describe the earlier baseline, not the new engineering candidate.

This is a separate development project for the shared system: Python core/Harness MCP, lifecycle/router, XATS messaging/MCP upstream, remote-worker and artifact/Markdown delivery code, Writer MCP/provider, Token Guard, and background/Windows tooling. Existing services and plugin bindings remain on their original installation. See [the functional inventory](docs/extraction/ENTRYPOINTS.md) for each capability and its verification boundary.

## Included source
- `solution_agent/`, `scripts/harness/`, `tests/`: release-matched Python core/Harness and existing source-only additions.
- `components/lifecycle/`: router/manager and lifecycle entry sources.
- `components/goal-routing-integration/vendor/`, `components/xats-routing-lab/upstream/`: required selected upstream source and licenses.
- `plugins/`: Harness, current report writer/provider and Token Guard maintenance source.
- `tools/research-background/`: background worker and Windows launcher source, synthetic development config.
- `docs/extraction/`: source mapping, adjustments and verification results.
- `docs/planning/`: later architecture and maintenance task specifications.

No research corpus, active state, credentials, old venv/node_modules or existing Git history is copied into the source tree.
Development dependencies are reconstructed separately under ignored `.venv/` and `.deps/`; no external links to the original runtime are allowed.

## Scope
Independent source baseline for private GitHub version control. Source publication does not register plugins, change hooks, deploy services, or migrate the live system.
Original runtime and research locations are preserved. Do not execute installation/start/stop scripts against the current system.
The background `config.json` addresses only this project's empty fixture vault and a nonexistent local credential file.

## Verification
See `docs/extraction/VERIFICATION.md` and machine-readable receipts.
Python uses `.venv/Scripts/python.exe` on Windows; invoke project scripts from this root.
Router build: `node components/lifecycle/build-router-package.mjs` (writes candidate artifacts inside this project).
Writer: `node --test plugins/research-report-writer/tests/core.test.mjs`.
Background: `node --test tools/research-background/tests.mjs` (mock provider, synthetic state only).
Formal Nx/uv/pnpm modularization, GitHub publication, Linux-native validation and live cutover are subsequent phases.

## Extraction results

904 mapped publication source files (40 private historical files excluded from the original 944-file extraction); router/writer/background/Guard checks pass. The initial Python core check has one cancellation failure and two skips; additional legacy checks have nine failures involving workspace configuration, documented in verification; see [verification](docs/extraction/VERIFICATION.md). Linux-native validation and online dependency reinstall remain unverified.



## Publication boundary

The source project is named **Agent Relay**; existing Python namespaces, MCP tool identities and plugin names remain compatible. Private historical receipts are outside this source tree. Dependencies, local configuration and generated artifacts are excluded by Git rules.

Only variable names, relative paths and clearly synthetic examples belong in the source release. Configure actual workplace/SSH/provider paths and credentials through per-machine overrides outside Git. Security tests assemble explicitly fake credential-shaped values to exercise redaction.

Run `python scripts/extraction/check-publication.py` before staging. Run `python scripts/extraction/package-source.py` to create a checked source-only ZIP under ignored `.verification/`; do not archive the entire development directory. This check supplements review of staged changes and any reachable Git history.


## Baseline publication — 2026-10-12

Agent Relay coordinates MCP tools, task routing, remote workers and artifact delivery, with shared writer, output-guard and background utilities. This repository preserves the independent extraction baseline before formal architecture normalization.

The known initial cancellation failure, nine additional legacy workspace-configuration failures and two initial skips remain documented in `docs/extraction/VERIFICATION.md`. Online dependency reinstall, Linux-native rebuild and live cutover have not been verified. Local receipts and dependency environments are excluded from the repository.

Private repository: https://github.com/hhhheep/agent-relay

Baseline tag: `extraction-baseline-2026-10-12` (source commit `458b54db0326`). A clean GitHub clone reproduced all 941 baseline source files byte for byte; clone-owned offline dependencies passed the Harness 11-tool check, router/XATS build, and Writer/background tests. Online reinstall and Linux-native validation remain unverified.

## 正規化與分期（2026-10-12）

當前工程範圍：[第一期與按需分期](docs/planning/normalization-20261012/PHASED_DELIVERY.md)。先整理來源、Windows客戶端/Linux Worker兩端可重建安裝，驗證新/舊、新/新、舊/新、舊/舊更新；保留真實擴展位置及原插件/MCP用法。

第一期保留現有主要目錄；原941完整遷移map/apps/packages布局只是候選分析，非搬檔指令。[主任务書](docs/planning/normalization-20261012/TASKBOOK.md)與[ADR-002](docs/planning/normalization-20261012/ADR-002-PHASED-DELIVERY.md)已分清P1範圍。功能擴展、更新自動化、研究工作台各按實際使用需求觸發，不自動全部做；第一期暫估9–15基礎/約12–19含餘量人日，非日曆承諾。

已完成四輪架構/維護討論及使用者後續分期修訂；最新P1實作與本機驗證見上方工程狀態。四情境跨端安裝及現用切換尚未執行。[歷史計劃索引](docs/planning/README.md)保留追溯。

下一個agent必讀[根AGENTS](AGENTS.md)、[分期交付](docs/planning/normalization-20261012/PHASED_DELIVERY.md)及[維護準則](docs/planning/normalization-20261012/MAINTENANCE_RULES.md)。最小完整修復、唯一來源、原契約/state、必要驗證及交接仍須遵守；專案經驗留所屬project，不自動上升全域或其他repo。
