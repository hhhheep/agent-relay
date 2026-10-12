# P1 engineering delivery

P1 S1–S5 is verified for the declared Windows x64 client / Linux x64 Worker combination and the exact supported old Harness baseline below. This is an engineering delivery: reproducible source, installation packages, configuration examples, actual isolated installation/update/rollback and shared-module consumers. Native message delivery, paid report generation and live research deployment are not part of this verified claim.

## Source, version and supported targets

- Source: `codex/phase-one-restructure` in the public `hhhheep/agent-relay` repository. Main received only its separately authorized project README; candidate engineering code has not been merged into main.
- Tested runtime repair commit: `d0481c629085a7b09bc4483f82bfa3ba1040788c`. Reviewed documentation/configuration commit: `691ba2cc49c0ddb104e5dc59b353783430a94906`, with successful [CI 38230874861](https://github.com/hhhheep/agent-relay/actions/runs/38230874861). Each rebuilt package records its actual producer commit and hashes in `candidate.json`.
- Current runtime: `harness-0.6-bd6c131ec6a770e5`, source root `bd6c131ec6a770e579937e4da52cfde08ed2fa149dee86b582b9d51ff3b6b591`, protocol 0.6, MCP schema hash `0e4f66158a663c674a761cf70253cb6cda09b34167c99e75643892ae6556b4b4`.
- Windows: Windows 11 x64, build 26200, Python 3.13.12 and Node 24.19.0; a new same-host installation with fresh dependency stores. GitHub clean-host CI independently rebuilds the declared Windows dependencies.
- Linux: Ubuntu 22.04.5 LTS x86_64, Python 3.13.16; optional Node components rebuilt with Node 24.19.0 and target-native SQLite.
- Supported old starting point: `harness-0.6-cdf4c9cbb9e48fb2`, immutable original ZIP SHA256 `067da359bf8ee832fc03a0c4a81e8da8cf90a9fd58c6eb3da22d5b0d505f8d1d`. Its original private package/configuration remains retained privately. No arbitrary historical release or different OS/architecture is certified.

## Requirement-by-requirement result

| Requirement | Completion evidence and practical limit |
| --- | --- |
| S1: unique sources, owners, entrypoints, targets and baseline | Compact owner table below, actual tool surfaces and builders; immutable 904-file source map plus current change provenance. Cancellation and all nine legacy fixture failures are repaired with behavior assertions retained. |
| S2: portable installation inputs and real shared module | Per-machine profile injection; absolute entrypoints from arbitrary cwd; exact Python pins, registry Node locks and target-native rebuild. Shared Provider is consumed by both Writer and Background through real SDK requests with mock HTTP. No credential or research checkout is needed to build. |
| S3: clean Windows/Linux install and configuration | Actual client/Worker ZIP installs, new no-cache Python environments, original clean npm/pnpm stores, native SQLite and stdio surfaces. Manager/XATS examples validate and match the real production connection tick. [INSTALL](INSTALL.md) covers requirements, outputs, configuration and manual checks. |
| S4 / M1 | Fresh client environment with matching supported old client/Worker identities and original command recovery. Original old package is unchanged; no mixed-release identity bypass. |
| S4 / M2 | New bd client/Worker pair, real remote cwd probe, original ID lookup and artifact hash/diff-review/create-only CAS. |
| S4 / M3 | Existing client adds the bd binding; all prior bindings and three original profile/receipt/SQLite hashes remain intact. |
| S4 / M4 | Real loaded old → bd → old in the path-preserving private Linux namespace. Hash-bound conflict-free forward/reverse plans, SQLite-consistent backup, 16 preserved hashes, old result read on bd and bd result read after rollback. No command resubmission. |
| S5: documentation, checks, privacy, version and extension | Install/update/rollback instructions and real configuration examples; public project README; necessary Windows CI; current publication/provenance checks; exact source/runtime/artifact identities. Provider environment/config override has two actual consumers and restores its test environment, without paid requests or a new framework. |
| Genuine integration | Production default-adapter Manager simultaneously connects official native Codex App identity/health read, own loopback XATS `/mcp`, and strictly paired bd Harness; zero pending work, then clean shutdown. Standalone Writer's own SSH adapter validates synthetic evidence and rejects an outside-root input. |

The current source repair passed 97 Windows core tests (two native POSIX skips) and 194 Linux core/legacy/install/namespace/Token Guard tests with no skips. Writer/Background/Provider have 23 consumer tests; Lifecycle/goal have 77 tests. CI also rebuilds Router/XATS and checks standalone Writer stdio. [Workplace acceptance](WORKPLACE_ACCEPTANCE.md) records actual matrix identities, update/rollback hashes and retained failed acceptance configurations. [STATUS](STATUS.md) retains the chronological evidence and exclusions.

## Owners and usable entrypoints

| Owner | Maintained source / entry |
| --- | --- |
| Harness core and client | `solution_agent/`; eleven-tool `solution_agent.harness.mcp_server`; client `plugins/solution-agent-harness/scripts/remote_launcher.py` and generated paired manifest |
| Lifecycle / goal routing | `components/lifecycle/`, `components/goal-routing-integration/`; `manager-cli.ts`, `resume-rpc.py`, `build-router-package.mjs` |
| XATS integration and upstream | `components/lifecycle/production-xats-service-20261008.ts`; fixed upstream/license under `components/xats-routing-lab/upstream/` |
| Writer | `plugins/research-report-writer/server.mjs`, `lib/`, `remote/`; six MCP tools; build with `scripts/release/build_writer_plugin.mjs` |
| Shared Provider | `packages/report-provider/index.mjs`, versioned `profiles/report-v1.json`; actual Writer and Background consumers |
| Background | `tools/research-background/cli.mjs`, `service.mjs`, `provider.mjs`; explicit private vault/rules/state configuration |
| Token Guard | `plugins/research-token-guard/skills/research-token-guard/scripts/`; existing five CLI helpers and task-scoped receipts |
| Installation / release | `scripts/release/install_dependencies.py`, `build_candidate.py`; existing `scripts/harness/build_bootstrap_release.py`, `install_bootstrap_release.py`, `install_overlay.py` |

Modify these sources, rebuild, and revalidate the affected consumers. Installed plugin/cache copies and generated bindings are not permanent edit locations. `solution_agent`, tool names, protocol/workplace pairing and command/receipt/artifact/CAS/lease semantics remain preserved. Routing uses native Codex queries and existing IDs; no transport-owner execution lock was introduced.

## Deliverables and reproduction

Follow [INSTALL](INSTALL.md) and the complete [client/Worker configuration example](CLIENT_CONFIGURATION.md) from a separate checkout with your own private configuration. Build from a clean reviewed commit:

```powershell
python scripts/release/install_dependencies.py --role windows-client
python scripts/release/build_candidate.py
```

`dist/p1-candidate/` contains `windows-client-source.zip`, `harness-worker.zip`, `research-report-writer.zip`, `install_bootstrap_release.py`, `install_overlay.py`, and `candidate.json`. Verify the recorded hashes before installation. The immutable tested bd package set is retained privately; later documentation-only rebuilds are compared byte-for-byte against it. Runtime, contract and business code remain identical; reviewed documentation/configuration examples and generated archive metadata may differ.

For an existing deployment, retain its supported old package and state ownership, take an SQLite-consistent backup, review the installer plan, verify the selected loaded identity and look up original IDs. Roll back code/binding using the exact retained old artifact; do not overwrite state or resend uncertain commands. A mismatched identity, unknown ownership, conflict or failed check stops that action. No live switch is implied by this delivery.

The original design inventory and extraction verifier still check the immutable 941/904-file baseline snapshot. They intentionally reject implemented source changes when run against the current checkout. Current engineering uses `scripts/release/provenance.py`, preserving the original map and explicitly recording 27 changed extracted files, plus publication/CI/runtime checks. Neither the historical map nor its checker was weakened to make implementation pass.

NOT RUN: native cross-chat send and paid report drafting. NOT AVAILABLE: another physical Windows device. Live activation, hooks/startup registration, research tasks and broader platform/release compatibility require their own scope and actual verification. These limits do not substitute for any S1–S5 or M1–M4 requirement listed above.
