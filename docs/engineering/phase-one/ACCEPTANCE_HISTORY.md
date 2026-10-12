# Historical acceptance rounds

This is a retained chronological record. Early PARTIAL/NOT RUN statements and prior release identities are superseded by [current status](STATUS.md); they are not current blockers or acceptance claims.

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
| components/xats-routing-lab/upstream | selected third-party source and native SQLite | Version/license retained; Windows/Linux x64 native build and memory smoke passed |
| plugins/research-token-guard | five existing Python CLI scripts and skill | Retained actual receipt/stage ledger owner; no new MCP |
| root cli.py legacy orchestrator/continuation commands | remote_status/run_remote_materialized_execution and l72 staging/launch prototypes | Source retained. Outside the P1 managed Harness/plugin deployment acceptance surface; excluded original workspace/bootstrap/planner config remains externally required |

The last row is a deployment boundary, not a claim that all legacy source is broken or removed. The original 40 passed / 9 failed reproduction depended on research checkout inputs that extraction intentionally excluded. The follow-up repairs those test fixtures: temporary synthetic conda profiles/bootstrap inputs and an explicit bounded-probe staging policy. Original assertions remain intact; no private policies are shipped or runtime defaults introduced. The two legacy suites plus missing-bootstrap-policy regressions now pass: 55 passed. This verifies their tested behavior, not authorization or readiness to dispatch a real research project through the legacy CLI.

Windows x64 / Python 3.13 / Node 24 client is exercised in isolated same-host environments. At the user's explicit authorization, an isolated test directory was created in the declared Linux workplace. Python 3.13.16 and exact Worker pins install there; dependency integrity and real Windows-to-Linux Harness stdio checks pass. The immutable original package now activates at its original paths in a private Linux namespace; isolated Harness old/candidate/old update and rollback passed. Detailed scope and private receipt hashes are summarized in WORKPLACE_ACCEPTANCE.md; whole-system P1 remains unverified.

## Implemented engineering

Compact real-consumer layout, Provider authority migration, fixed preset and safe numeric usage; external profiles path injection shared by terminal/worker; repaired valid generic profile template; early cancellation handshake and deterministic pre-spawn regression; source-only client/Worker candidates using existing builder; frozen Node install inputs, Windows exact pins, Linux pins verified by isolated installation; dependency setup and update/rollback instructions; immutable extraction map plus new change provenance. XATS build replaces an absolute native-module URI with a repo-relative runtime bridge.

## Verified and pending

Detailed machine receipts stay ignored under .verification. Remote component evidence is distinguished from whole-system acceptance below.

- Core cancellation, protocol, identity, artifact apply, TaskCapsule, bootstrap/install, profiles, Codex worker and remote-terminal synthetic tests: 90 passed / 2 existing Windows POSIX-only skips before the final configuration regression. Additional config/bootstrap check: 11 passed.
- Writer 9 and Background 12 tests pass. Shared provider golden consumer tests: 2 pass, mock HTTP through the real SDK, no paid API.
- Source-only clean client package: locked Provider npm, Writer frozen pnpm and lifecycle npm install; 23 Node consumer tests pass. Dependencies acquired from registries; Writer pnpm reports cache reuse for its three packages, not proof of cache-free download for every package.
- Fresh Python 3.13 venv acquired exact Windows pins from public PyPI (mixed downloads/cache), pip check passes. Clean source package Harness self-check returns 11 canonical tools, profile unavailable as expected; release identity equals the built candidate. This is same-host isolated installation, not a new Windows computer/M2.
- Router candidate build and XATS source build pass on Windows and Linux. Fresh Linux registry dependency installation, native better-sqlite3 rebuild and memory SQLite smoke pass; see the clean Linux follow-up below.
- Lifecycle/goal integration: 77 passed using component cwd and repo PYTHONPATH; no assertion changes.
- Initial sandbox temporary-directory restrictions and incorrect lifecycle cwd/PYTHONPATH caused transient verification failures; record corrected final checks below rather than weakening tests.

| Scenario | Result | Missing evidence |
| --- | --- | --- |
| M1 new client / existing Worker | PARTIAL; Harness cross-end passed | Same Windows host isolated client, not another physical Windows device; complete required component coverage pending |
| M2 new client / new Worker | PARTIAL; fresh dependencies and Harness cross-end passed | Full required component/runtime coverage and independent Windows device pending |
| M3 existing client / new Worker | PARTIAL; binding switch and preservation passed | Isolated old-client configuration/history exercise, not live-client activation |
| M4 existing client / existing Worker update | PARTIAL whole-system; isolated Harness update/rollback PASSED | Original paths preserved in a private Linux namespace with private systemd; wider component/device coverage remains |

Local builder dry-run/install, original-command recovery, cancellation/lease/CAS tests cannot stand in for these rows. Unknown peer remains blocked by unchanged verify_pair. No arbitrary old/new software-version interoperability is promised.

## Next acceptance steps

The declared Linux workplace now provides an authorized fixed isolated test target. Exact original/candidate/original Harness activation, update and rollback pass in a private namespace. Linux Node component rebuild and direct-consumer coverage now also pass. Next, rebuild deliverable archives with the installation fixes, verify the archive-to-install path, and audit every M1–M4 required behavior against its actual receipt. Another physical Windows device is unavailable; same-host clean installation evidence must remain explicitly identified. Only then freeze supported acceptance scope. No paid research execution or live cutover is included.


## Final local component evidence

Final affected Python + Token Guard suite: **130 passed, 2 existing Windows POSIX-only skips**. Final Node consumer suite: **23 passed**. Lifecycle/goal suite: **77 passed**. Synthetic managed install/update/rollback plus bootstrap installer and delivered-CLI checks pass. Publication and historical-map/new-provenance checks pass. Git whitespace review uses cr-at-eol for original CRLF source files; no content trailing whitespace remains.

Preferred original installation: `harness-0.6-cdf4c9cbb9e48fb2`, SHA256 `067da359bf8ee832fc03a0c4a81e8da8cf90a9fd58c6eb3da22d5b0d505f8d1d`, 320 original payload hashes verified. Paired Client manifest is retained privately; no old Git commit is inferred. Local rehearsal installed that exact artifact, performed hash-reviewed update, and restored the old identity. Original completed command receipt/hash and execution release were recovered before and after rollback without re-submission. Synthetic config hash and SQLite WAL data stayed unchanged, with SQLite-consistent backup. Old/candidate MCP schema fingerprint stays `0e4f66158a663c674a761cf70253cb6cda09b34167c99e75643892ae6556b4b4`. Archived profiles are inert; no profile loader, SSH or credential file was used. This is Harness component-only local evidence, not M4 or whole-system old/new support.

Standalone Writer plugin closure builds from the unique shared Provider source, with preset bundled and dependency licenses/source hashes. Real stdio handshake from an unrelated working directory lists exactly the original six tools and reports no provider credential configured. It does not need adjacent source packages/node_modules. The new Windows-only CI workflow is configured for required local candidate gates; remote Actions execution remains pending until pushed/run. No Linux/remote acceptance claim is derived from CI configuration.

Historical design checker `build-plan-inventory.py --check` was executed and intentionally reports worktree/baseline differences once engineering changes exist. Its immutable design maps were not rewritten. The replacement engineering provenance gate verifies the original source-map against the baseline Git blob and records every changed extraction hash; candidate builder additionally verifies Git publication candidates and artifact bytes. This preserves the old check as historical evidence rather than pretending the worktree is unchanged.

## Namespace follow-up

The original absolute-root blocker is resolved without editing the old payload or weakening protocol checks. Linux namespace/private-systemd old/candidate/old stdio activation, supervised command recovery, unchanged configuration/state/receipts and SQLite-consistent backup passed. The rolled-back old release also recovered a candidate-release command. M2/M3 and the rolled-back M4 environment passed real artifact store/inspect/materialize plan and CAS commit. Linux core/legacy/install/namespace regression: 153 passed; Windows namespace boundary tests: 3 passed. Method and evidence: [WORKPLACE_ACCEPTANCE.md](WORKPLACE_ACCEPTANCE.md), [NAMESPACE_ACCEPTANCE.md](NAMESPACE_ACCEPTANCE.md). Whole-system P1 remains partial.

Linux Token Guard suite also passed: **37 passed** (190 Linux tests across the two suites). Final checks confirmed all seven original hashes and the immutable old ZIP remained unchanged, with no acceptance transient units left running.

## Clean Linux Node component follow-up

The fixed test environment acquired official Node 24.19.0 Linux x64 bytes and verified SHASUMS256. Source came from the clean `f4a28c5` Git archive, followed by explicit source fixes. Empty npm/pnpm caches and a new source checkout were used; Python 3.13.16 dependencies were installed in a new venv with `--no-cache-dir`, and pip check passed. Node dependency setup used npm ci and pnpm 11.25 frozen install; no Windows native binaries were copied.

Three missing installation inputs were found and repaired: XATS lockfile specifiers disagreed with the exact package.json pins; five Python-backed Lifecycle tests selected a Windows-only venv path; goal-routing's separately declared MCP SDK had no registry lock or dependency-install step. The SDK remains 1.29.0; the new goal lock records its full registry closure. Its first generated lock incorrectly inherited existing local junctions, which clean Linux testing caught. The final lock was regenerated from package.json in an empty directory, contains registry integrity entries and no local links, and installs 95 packages successfully. No package identity, test assertion or protocol gate was relaxed.

Linux final results: **23 consumer tests and 77 Lifecycle/goal tests passed, zero failures/skips**. Router candidate build, target-native better-sqlite3 rebuild, SQLite create/insert/select memory check, XATS service bundle, standalone Writer build and actual stdio six-tool check from an unrelated cwd passed. Python-backed mailbox tests start temporary loopback XATS servers with synthetic state and test shutdown; no persistent service or research task was activated. Windows affected fixtures: 10 passed; isolated update/rollback regression: 1 passed.

Private logs/summary JSON remain under `.verification/full-acceptance/`. Final Linux Lifecycle log SHA256: `16fec058cd943db8ea7ad5f6be406140107d1201e07f41f96bc4879a59b4eb8d`; consumers: `b04f8797e58cad2531c126841deb8e19823937e7fe1b7290465197f95c91925a`; native/XATS build: `17898e73b8e145b01d855df75c65886f4fd0c5fab5f85f426e469bd2cd3e03a6`; Writer stdio: `7e47c8c213a34ea38acee26df1461352557091ab64358b7f4ef06ac5000c791c`. Earlier failed attempts are retained and excluded from passed counts. These are actual component installation/runtime checks, not a substitute for archive installation or every deployment-matrix row.

## Current archive matrix closure

The current runtime `harness-0.6-6f9532ef91a5cdae` now has independently audited M1–M4 Harness receipts, including actual original/current/original update and rollback, preserved old/current command execution identities, thirteen unchanged configuration/history/result hashes, SQLite-consistent backup rows and zero acceptance transient units. Current fresh Windows archive XATS installs 255 downloaded/zero-reused packages, passes target-native SQLite and XATS build, and passes all 77 Lifecycle/goal tests. The production HarnessPort and actual installed MCP SDK also connect to the real isolated Linux Worker and recover the original M2 command. Full evidence: [WORKPLACE_ACCEPTANCE.md](WORKPLACE_ACCEPTANCE.md), private matrix-current summary. Previous missing component/native/current-matrix items above describe earlier rounds and are now resolved by these receipts. Whole-system support/entrypoint completion audit remains open before stage promotion; the physical-device limitation remains explicitly disclosed.
