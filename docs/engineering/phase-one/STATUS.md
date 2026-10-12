# Current P1 acceptance — 2026-10-12

Stage: **P1 engineering verified for S1–S5 and the declared target/baseline scope**. [Final delivery review](DELIVERY.md) records all requirements and concrete deliverables. Current bd Harness M1–M4 and production Manager connection acceptance passed. Native cross-chat send, paid execution and live activation remain unverified and excluded from this claim. Live plugins, hooks, deployment, research state and schedules were not changed. This page supersedes the chronological [earlier rounds](ACCEPTANCE_HISTORY.md).

## Requirements and evidence

| Requirement | Current result | Authoritative evidence |
| --- | --- | --- |
| S1 owners, entrypoints, supported baseline | Source owners and public entries recorded; original Harness baseline retained | [Entrypoints](../../extraction/ENTRYPOINTS.md), [decisions](DECISIONS.md), original package hash in workplace record |
| Known cancellation and nine legacy failures | Fixed and passing with original assertions; synthetic private-input fixtures | Core/legacy regression receipts; historical classification and fixes in earlier rounds |
| S2 portable source and dependency inputs | Provider shared by two real consumers; explicit private profiles; full registry locks; exact target pins | Unique source owners, install script, provenance and publication gates |
| S3 Windows clean archive install | PASSED on the same Windows x64 host in a new directory; no-cache Python and empty round-local npm/pnpm stores | Private windows-package checks and native-summary; 23 consumers, 77 Lifecycle/goal, SQLite/XATS builds, 11 Harness tools |
| S3 Linux clean archive install | PASSED in the fixed isolated Linux x64 workplace | Delivered installer dry-run/apply; 324 payload checks; no-cache Python/pip check; loaded identity; standalone Writer six-tool stdio |
| Linux components and native dependencies | PASSED: 194 current core/legacy/install/namespace/Token Guard; 23 unchanged Node consumers + 77 Lifecycle/goal, zero Linux skips | Retrieved component logs, summary hashes, native SQLite and Router/XATS builds |
| M1 clean client / supported old Worker | PASSED, isolated Harness | package-bd-M1-old-before.local.json; exact retained old pairing and original command lookup |
| M2 clean client / clean Worker | PASSED, isolated Harness | package-bd-M2.local.json; matching current identity, original receipt and reviewed artifact CAS |
| M3 existing client / clean Worker | PASSED, isolated Harness | package-bd-M3.local.json; additive binding; original profile/receipt/SQLite history hashes unchanged |
| M4 supported old / current / old | PASSED, isolated Harness | Current forward/reverse hash-bound plans, current recovery receipts, 16 preserved hashes, SQLite-consistent backup and private namespace shutdown |
| Real Router Harness adapter | PASSED against isolated Worker using installed SDK | manager-stack-bd-real.local.json plus package-bd-M2.local.json; real production adapter and strict launcher, no resubmission |
| Native Codex App bridge | PASSED: production NativeAppPort with official installed stdio server, trusted caller/pipe, exact identity and thread read | native-app-real.local.json; no send or pipe discovery |
| Writer remote business adapter | PASSED: standalone MCP and own real SSH route, synthetic evidence SHA256 and outside-root rejection | writer-remote-real.local.json; no credentials/model call |
| S5 documentation, privacy, provenance, extension | Installation/update steps, unique owners and real Writer/Background Provider consumers documented; publication/provenance gates required before packaging | [Install](INSTALL.md), [workplace record](WORKPLACE_ACCEPTANCE.md), source-changes.json, build_candidate.py |

Private receipts remain ignored under `.verification/full-acceptance/`; the published workplace record contains bounded methods and hashes, without private profiles or research data. The current tested runtime is `harness-0.6-bd6c131ec6a770e5`, source root `bd6c131ec6a770e579937e4da52cfde08ed2fa149dee86b582b9d51ff3b6b591`. Original supported baseline is `harness-0.6-cdf4c9cbb9e48fb2`, ZIP SHA256 `067da359bf8ee832fc03a0c4a81e8da8cf90a9fd58c6eb3da22d5b0d505f8d1d`. Strict protocol/workplace pairing remains unchanged; arbitrary old/new release interoperability is not claimed.

## Unverified coverage and concrete next checks

| Item | Status and reason | Completion evidence needed |
| --- | --- | --- |
| Native App send / paid Writer draft | NOT RUN; no messaging destination or paid execution is included in this engineering test. Existing integration/Provider fixtures passed. | Explicit authorized use-case and actual receipt; no claim of live message delivery or paid drafting. |
| Another physical Windows device | NOT AVAILABLE. Evidence is same-host clean installation, explicitly allowed as the clean-install scenario; it does not prove another OS image or machine. | An additional declared device if broader hardware coverage is requested. |
| GitHub Actions | PASSED on published commit `691ba2cc49c0ddb104e5dc59b353783430a94906`; Windows clean-host run completed all required steps. | [Run 38230874861](https://github.com/hhhheep/agent-relay/actions/runs/38230874861); the closing packaging/metadata commit retains its own CI result. |
| Live deployment and research work | NOT RUN; outside this isolation task. | Separate deployment/task authorization and loaded-session verification. |

These exclusions limit deployment and paid business-execution claims; they are not unresolved failures of the completed isolation checks. M1–M4 Harness results are complete for their tested scope and are no longer marked pending because of unavailable physical hardware or intentionally untouched live configuration. All source fixes stay in the repository authority; no installed copy is a permanent fix.

## Genuine host and Writer adapter closure

The installed Codex package supplies `app/resources/plugins/openai-bundled/plugins/codex-app-tools/server.mjs` and official launch scripts. This native-tools MCP entry is distinct from the public `codex app-server` JSON-RPC interface. Installed server SHA256: `264116eb1273ab0e36b177198540afe943b533b89a4f27abfd178f476a4b8afc`. Production NativeAppPort used only the current executor's supplied caller/pipe, verified caller/host identity and read the exact caller thread. No message, hook change, pipe search or substitute server was used.

Standalone Writer exposed its six tools. Provider was unconfigured; the explicit test SSH route validated one synthetic claim, returning evidence SHA256 `8903e789d862e440112d74391bcf6c85c74c71b2ed4c1d275d5d178b3eff53f5`. Installed remote script matches source SHA256 `6e5be0e2a804474e5422ce8726f08abb1d8d77e49b59438b57019c90884302e4`. Private receipt SHA256: `a907d31d710479be693f2747fe523bbe156434a0b8074b88c5be5d98184483c5`. Outside-root input was rejected. No provider credential or report content was sent; roots remain inside the installed script's deployment boundary.

Diagnostics retained: sandbox networking/pipe access returned permission errors; the same read-only tests passed in the authorized normal environment. An acceptance assertion read `remote_writer_status.configured` instead of `remote_writer_status.remote.configured`; only the verifier was corrected. Initial fixture placement was moved inside the deployed Writer boundary before invoking its adapter. A TaskCapsule observation conflict was resolved by querying the original succeeded command ID, without resubmission.

The authorized Linux/native/archive/matrix checks and documentation deliverables are complete. Candidate stage is retained; this record does not authorize live cutover or broaden supported devices/releases.

## Continued delivery review

The next goal covers whole P1 delivery and publication reproducibility, beyond the completed isolation goal. [RELEASE_REVIEW.md](RELEASE_REVIEW.md) fixes the proposed delivery scope and next actions. Windows CI previously omitted Token Guard, native XATS and Lifecycle/goal execution; these required direct-consumer checks are now configured. Local follow-up executes 69 Lifecycle and 8 goal tests, Router build and Token Guard with the same commands. A sandbox-only temporary-directory ACL failure is retained in the tool record; normal-environment results are recorded separately. The reviewed branch is now published; remote Actions passed on commit `004fe29031f6b7f9b0e5807f20050e2806f39367` (run linked below). Installing the candidate in the live environment is not implied by publication.

## Public repository and clean-host CI

The user authorized publishing the candidate branch and running CI, then requested public visibility. Publication changed only repository visibility after current-source checks and an audit of all reachable published heads/tag: 1,108 Git blobs, no scanner findings. The GitHub connector can now read the repository. Private acceptance receipts and packages remain excluded.

First clean-host run [38225154314](https://github.com/hhhheep/agent-relay/actions/runs/38225154314) failed two fixture checks: Windows short/long temporary-path spelling and unspecified decoding of a UTF-8 receipt. Runtime behavior was unchanged; expected override paths now use canonical `resolve()`, and receipt parsing specifies UTF-8. Both affected suites pass locally (21 tests). Fix commit `004fe29031f6b7f9b0e5807f20050e2806f39367` is published. Follow-up [38225690945](https://github.com/hhhheep/agent-relay/actions/runs/38225690945) completed successfully on that exact commit. All required steps passed, including core/update, legacy, Token Guard, Provider consumers, native XATS, Lifecycle/goal and candidate build. This result applies to that commit; subsequent documentation commits retain their own workflow identity.

## Terminal completion publication race follow-up

README-only main commit `9541b88` is published; it does not merge the candidate code. Candidate run [38227032156](https://github.com/hhhheep/agent-relay/actions/runs/38227032156) on `8dec6978295a84466adfd847276d7d5f6c42d130` failed the writer lease receipt assertion: terminal state was visible before lease disposition. The earlier green CI remains scoped to its original commit.

Compatible fix in `solution_agent/harness/terminal_commands.py`: complete the existing writer lease disposition before publishing the terminal manifest, event or status; manifest reconciliation and lost finalization follow the same order. Preserve an already successful matching release receipt during recovery. No contract/state format or transport-owner lock is added. Command IDs, stdout hashes and execution identity semantics remain unchanged. This is a runtime change, so the previous 6f release matrix does not verify the new runtime.

Deterministic publication observers reproduced missing receipts for success/failure/cancelled before repair (three failures). Repaired coverage also checks lost completion and manifest-only recovery preserving the original disposition. The full CI core/install selection passes locally: 97 passed, 2 Windows-native POSIX skips. Private logs retain the before/after evidence. Changed runtime: `harness-0.6-bd6c131ec6a770e5`, source root `bd6c131ec6a770e579937e4da52cfde08ed2fa149dee86b582b9d51ff3b6b591`. Linux, rebuilt paired archives and affected M2–M4 checks remain pending for this runtime; M1 remains tied to the immutable supported old package. No live state is changed.

Follow-up checkpoint: [run 38228079618](https://github.com/hhhheep/agent-relay/actions/runs/38228079618), job `114740438554`, completed successfully on repair commit `d0481c629085a7b09bc4483f82bfa3ba1040788c`. Linux core/legacy/install/namespace/Token Guard selection passed 194 tests with no skips. New client ZIP SHA256 `b1351239bb6a53fda0a086dd443c30be81af55673db6b8841ba594e314360d9c` and Worker ZIP SHA256 `1c1d9b3e66804aa56576e85a90cef066183645421c95829c552ad3450374a68b` passed fresh no-cache Python installation, pip check and runtime/tool verification. The first Linux install command used a mistyped requirements filename; its log is retained, and the corrected command completed in the already created fresh venv. M2–M4 remain pending for bd6c runtime; no full-delivery promotion.

## Current bd matrix and production Manager closure

Current bd M1–M4 all passed; [workplace record](WORKPLACE_ACCEPTANCE.md) contains current identities, exact forward/reverse archive and plan hashes, preserved state and retained diagnostics. M4 preserves 16 hashes and SQLite-consistent backup contents, with original old/new command lookup after rollback and no remaining lease for the bd fixture task. M3 preserves all previous bindings and the original three client history/config hashes.

Production Manager also passed one actual tick with its default adapters: genuine Codex App identity/health read, real isolated loopback XATS `/mcp`, and the strictly paired bd Harness connection. It stopped cleanly with no pending work; XATS closed. This closes simultaneous connection startup only. Native send, paid drafting and live activation retain their explicit NOT RUN scope. `project.json` retains prior archive acceptance separately and now references bd as the current runtime. Final S1–S5 delivery review and rebuilt artifact audit remain pending.

## S1–S5 final engineering review

All five batches and the complete current-runtime matrix are verified within [DELIVERY.md](DELIVERY.md). Historical design checks pass on the immutable 941/904-file baseline snapshot; current engineering provenance preserves that map and records 27 actual changes. New minimal client/Worker configuration examples were validated through the delivered launcher, including paired identity encoding. Publication checks cover current Git candidates only. The builder now distinguishes verified engineering delivery from a dirty, incomplete-matrix or mismatched-runtime build; those cannot be promoted by a single component receipt. Private audit includes the earlier failed dependency/lifecycle attempts and their actual positive replacements. No live or paid business claim is added.
