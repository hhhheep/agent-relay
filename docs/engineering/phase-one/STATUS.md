# Current P1 acceptance — 2026-10-12

Stage: **P1 engineering candidate**. Current Harness M1–M4 isolated acceptance passed; whole-system P1 is still partial. Live plugins, hooks, deployment, research state and schedules were not changed. This page supersedes the chronological [earlier rounds](ACCEPTANCE_HISTORY.md).

## Requirements and evidence

| Requirement | Current result | Authoritative evidence |
| --- | --- | --- |
| S1 owners, entrypoints, supported baseline | Source owners and public entries recorded; original Harness baseline retained | [Entrypoints](../../extraction/ENTRYPOINTS.md), [decisions](DECISIONS.md), original package hash in workplace record |
| Known cancellation and nine legacy failures | Fixed and passing with original assertions; synthetic private-input fixtures | Core/legacy regression receipts; historical classification and fixes in earlier rounds |
| S2 portable source and dependency inputs | Provider shared by two real consumers; explicit private profiles; full registry locks; exact target pins | Unique source owners, install script, provenance and publication gates |
| S3 Windows clean archive install | PASSED on the same Windows x64 host in a new directory; no-cache Python and empty round-local npm/pnpm stores | Private windows-package checks and native-summary; 23 consumers, 77 Lifecycle/goal, SQLite/XATS builds, 11 Harness tools |
| S3 Linux clean archive install | PASSED in the fixed isolated Linux x64 workplace | Delivered installer dry-run/apply; 324 payload checks; no-cache Python/pip check; loaded identity; standalone Writer six-tool stdio |
| Linux components and native dependencies | PASSED: 153 core/legacy/install/namespace + 37 Token Guard; 23 Node consumers + 77 Lifecycle/goal, zero skips | Retrieved component logs, summary hashes, native SQLite and Router/XATS builds |
| M1 clean client / supported old Worker | PASSED, isolated Harness | package-M1.local.json; exact retained old pairing and original command lookup |
| M2 clean client / clean Worker | PASSED, isolated Harness | package-M2.local.json; matching current identity, original receipt and reviewed artifact CAS |
| M3 existing client / clean Worker | PASSED, isolated Harness | package-M3.local.json; additive binding; original profile/receipt/SQLite history hashes unchanged |
| M4 supported old / current / old | PASSED, isolated Harness | Current forward/reverse hash-bound plans, four recovery receipts, 13 preserved hashes, SQLite-consistent backup and zero transient units |
| Real Router Harness adapter | PASSED against isolated Worker using installed SDK | router-harness-real.local.json; original M2 command recovered without resubmission |
| S5 documentation, privacy, provenance, extension | Installation/update steps, unique owners and real Writer/Background Provider consumers documented; publication/provenance gates required before packaging | [Install](INSTALL.md), [workplace record](WORKPLACE_ACCEPTANCE.md), source-changes.json, build_candidate.py |

Private receipts remain ignored under `.verification/full-acceptance/`; the published workplace record contains bounded methods and hashes, without private profiles or research data. The current tested runtime is `harness-0.6-6f9532ef91a5cdae`, source root `6f9532ef91a5cdaec9cfc345f888039b64f29f39472d4c1288f3ecc0f44efe91`. Original supported baseline is `harness-0.6-cdf4c9cbb9e48fb2`, ZIP SHA256 `067da359bf8ee832fc03a0c4a81e8da8cf90a9fd58c6eb3da22d5b0d505f8d1d`. Strict protocol/workplace pairing remains unchanged; arbitrary old/new release interoperability is not claimed.

## Unverified coverage and concrete next checks

| Item | Status and reason | Completion evidence needed |
| --- | --- | --- |
| Native Codex App bridge | NOT RUN end-to-end. NativeAppPort requires a declared genuine App stdio server path plus trusted caller/pipe context. The source archive does not supply that host service; isolated integration tests use an App fixture. A direct native read_thread call alone does not prove this adapter. | Bootstrap production NativeAppPort through the declared host server, verify genuine caller identity and read route; any send requires an explicitly authorized destination. No pipe discovery, impersonation or transport-owner lock. |
| Writer remote business adapter | NOT RUN end-to-end through its own SSH adapter. Standalone MCP handshake, local validation and shared Provider consumers passed; Harness cross-end receipts do not prove Writer's separate remote path. | Run status/validation against an explicit isolated remote Writer profile with synthetic evidence, verify result/ownership and no paid model call. |
| Another physical Windows device | NOT AVAILABLE. Evidence is same-host clean installation, explicitly allowed as the clean-install scenario; it does not prove another OS image or machine. | An additional declared device if broader hardware coverage is requested. |
| GitHub Actions | NOT RUN remotely. Workflow is configured; no push/run is claimed. | Actual workflow receipt after authorized publication. |
| Live deployment and research work | NOT RUN; outside this isolation task. | Separate deployment/task authorization and loaded-session verification. |

The first two items limit the whole-system claim. M1–M4 Harness results are complete for their tested scope and are no longer marked pending because of unavailable physical hardware or intentionally untouched live configuration. All source fixes stay in the repository authority; no installed copy is a permanent fix.
