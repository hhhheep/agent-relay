# Agent Relay — independent source baseline

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

## 正規化設計（2026-10-12）

最新版：[正規化施工任務書](docs/planning/normalization-20261012/TASKBOOK.md)。已完成指定聊天的四輪架構及維護審查、941基線檔案遷移計劃（904映射來源）、接口/相容/交付包規格及擴展台帳。以固定模組邊界和漸進更新維護；uv/Hatchling、pnpm/esbuild，Nx後延。

這是Design Ready文件，尚未執行source migration、正式locks/updater、Architecture Freeze或livecutover。[歷史計劃索引](docs/planning/README.md)保留原四份文件，舊結構與估算由最新版取代。

下一個 agent 的必讀規範：[根 AGENTS.md](AGENTS.md)及[維護準則](docs/planning/normalization-20261012/MAINTENANCE_RULES.md)，包含最小完整修復、唯一來源、契約/state保護、按影響驗證、完成/發布 gates 和交接要求；僅此專案適用，專案經驗不自動上升全域規則。
