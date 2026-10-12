# P1 engineering status — 2026-10-12

Scope: authorized first restructuring in this independent repository. Stage: P1 engineering candidate, not P1 Verified. No installed plugins, live profiles, hooks, research state, scheduler or SSH destination changed.

## S1 owners and supported entrypoints

| Source owner | Entrypoint / consumer | P1 treatment |
| --- | --- | --- |
| solution_agent/harness | Harness plugin server.py, existing scripts/harness CLI helpers, managed Worker | Required, solution_agent namespace and protocol 0.6 unchanged |
| plugins/research-report-writer | server.mjs, report schema, remote business adapter | Required MCP surface retained; shared provider externalized |
| packages/report-provider | index.mjs + versioned JSON preset | Actual Writer/Background consumers; one SDK owner, environment/env-file precedence retained |
| tools/research-background | service/cli/provider, Windows launcher | Required local mock behavior; no startup/schedule activation |
| components/lifecycle + goal-routing-integration | existing manager/router/session/mailbox/RPC and builders | Required existing consumers; component working directory/PYTHONPATH documented |
| components/xats-routing-lab/upstream | selected third-party source and native SQLite | Version/license retained; local build and memory smoke only; Linux native pending |
| plugins/research-token-guard | five existing Python CLI scripts and skill | Retained actual receipt/stage ledger owner; no new MCP |
| root cli.py legacy orchestrator/continuation commands | remote_status/run_remote_materialized_execution and l72 staging/launch prototypes | Source retained. Outside the P1 managed Harness/plugin deployment acceptance surface; excluded original workspace/bootstrap/planner config remains externally required |

The last row is a deployment boundary, not a claim that all legacy source is broken or removed. Evidence: cli.py imports solution_agent.cli, which imports orchestrator/continuation; current managed dispatch is solution_agent/harness/codex_worker.py through terminal_exec. Historical source inventory explicitly did not copy research workspace policies. The three failing legacy remote bootstrap/materializer tests require workspace/bootstrap/conda settings, while six l72 tests read .solution/continuation/planner_policy.yaml from their synthetic workspace. Reproduction: 40 passed / 9 failed. Tests remain intact; no assertions lowered or live configuration imported. Do not use these legacy commands as supported P1 remote dispatch or claim they pass.

Windows x64 / Python 3.13 / Node 24 client is locally exercised. Linux x64 Worker is a candidate target with pending native installation. Source extraction is not a supported deployed old release. Old install ZIP/manifest/hash and isolated Linux host are needed for full deployment acceptance; no special separate "isolation package" is required. The original local-to-remote install artifacts can be installed unchanged in isolated directories/targets. The preparation chat located the preferred old Harness installation package at the user's request. It is the original local-to-remote installation artifact, not a new isolation package.

## Implemented engineering

Compact real-consumer layout, Provider authority migration, fixed preset and safe numeric usage; external profiles path injection shared by terminal/worker; repaired valid generic profile template; early cancellation handshake and deterministic pre-spawn regression; source-only client/Worker candidates using existing builder; frozen Node install inputs, Windows exact pins, explicitly unverified Linux pins; dependency setup and update/rollback instructions; immutable extraction map plus new change provenance. XATS build replaces an absolute native-module URI with a repo-relative runtime bridge.

## Verified and pending

Detailed machine receipts stay ignored under .verification. Public summaries are engineering evidence, not remote acceptance.

- Core cancellation, protocol, identity, artifact apply, TaskCapsule, bootstrap/install, profiles, Codex worker and remote-terminal synthetic tests: 90 passed / 2 existing Windows POSIX-only skips before the final configuration regression. Additional config/bootstrap check: 11 passed.
- Writer 9 and Background 12 tests pass. Shared provider golden consumer tests: 2 pass, mock HTTP through the real SDK, no paid API.
- Source-only clean client package: locked Provider npm, Writer frozen pnpm and lifecycle npm install; 23 Node consumer tests pass. Dependencies acquired from registries; Writer pnpm reports cache reuse for its three packages, not proof of cache-free download for every package.
- Fresh Python 3.13 venv acquired exact Windows pins from public PyPI (mixed downloads/cache), pip check passes. Clean source package Harness self-check returns 11 canonical tools, profile unavailable as expected; release identity equals the built candidate. This is same-host isolated installation, not a new Windows computer/M2.
- Router candidate build and XATS source build pass; native SQLite memory smoke passes using the repo dependency environment. Linux-native and newly downloaded XATS native closure remain NOT RUN.
- Lifecycle/goal integration: 77 passed using component cwd and repo PYTHONPATH; no assertion changes.
- Initial sandbox temporary-directory restrictions and incorrect lifecycle cwd/PYTHONPATH caused transient verification failures; record corrected final checks below rather than weakening tests.

| Scenario | Result | Missing evidence |
| --- | --- | --- |
| M1 new client / existing Worker | NOT RUN | isolated client + authorized existing Worker copy + matching install artifacts |
| M2 new client / new Worker | NOT RUN | isolated Linux target and true two-end configuration/identity/dispatch/receipt test |
| M3 existing client / new Worker | NOT RUN | isolated client configuration/state copy, new Linux target, binding-switch evidence |
| M4 existing client / existing Worker update | NOT RUN | actual isolated Windows × Linux deployments; old Harness artifact and local component update/rollback evidence are now available |

Local builder dry-run/install, original-command recovery, cancellation/lease/CAS tests cannot stand in for these rows. Unknown peer remains blocked by unchanged verify_pair. No arbitrary old/new software-version interoperability is promised.

## Next acceptance steps

The old Harness install artifact is identified and locally rehearsed; retain its immutable ZIP and paired Client manifest for target testing. Obtain an explicitly authorized isolated Linux target. Repeat local installation documentation on the declared targets, run each M row including preserved original IDs/receipts/configuration, consistent SQLite backup and backward-read/rollback. Resolve any target-native or required-consumer failures. Only then freeze P1 layout and advertise a supported release. No paid resources or live cutover are included.


## Final local component evidence

Final affected Python + Token Guard suite: **130 passed, 2 existing Windows POSIX-only skips**. Final Node consumer suite: **23 passed**. Lifecycle/goal suite: **77 passed**. Synthetic managed install/update/rollback plus bootstrap installer and delivered-CLI checks pass. Publication and historical-map/new-provenance checks pass. Git whitespace review uses cr-at-eol for original CRLF source files; no content trailing whitespace remains.

Preferred original installation: `harness-0.6-cdf4c9cbb9e48fb2`, SHA256 `067da359bf8ee832fc03a0c4a81e8da8cf90a9fd58c6eb3da22d5b0d505f8d1d`, 320 original payload hashes verified. Paired Client manifest is retained privately; no old Git commit is inferred. Local rehearsal installed that exact artifact, performed hash-reviewed update, and restored the old identity. Original completed command receipt/hash and execution release were recovered before and after rollback without re-submission. Synthetic config hash and SQLite WAL data stayed unchanged, with SQLite-consistent backup. Old/candidate MCP schema fingerprint stays `0e4f66158a663c674a761cf70253cb6cda09b34167c99e75643892ae6556b4b4`. Archived profiles are inert; no profile loader, SSH or credential file was used. This is Harness component-only local evidence, not M4 or whole-system old/new support.

Standalone Writer plugin closure builds from the unique shared Provider source, with preset bundled and dependency licenses/source hashes. Real stdio handshake from an unrelated working directory lists exactly the original six tools and reports no provider credential configured. It does not need adjacent source packages/node_modules. The new Windows-only CI workflow is configured for required local candidate gates; remote Actions execution remains pending until pushed/run. No Linux/remote acceptance claim is derived from CI configuration.

Historical design checker `build-plan-inventory.py --check` was executed and intentionally reports worktree/baseline differences once engineering changes exist. Its immutable design maps were not rewritten. The replacement engineering provenance gate verifies the original source-map against the baseline Git blob and records every changed extraction hash; candidate builder additionally verifies Git publication candidates and artifact bytes. This preserves the old check as historical evidence rather than pretending the worktree is unchanged.
