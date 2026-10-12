# Current P1 acceptance — 2026-10-12

Stage: **P1 engineering candidate**. Current Harness M1–M4 isolated acceptance passed; the authorized isolated acceptance is complete. This does not promote untested live activation or paid execution to P1 Verified. Live plugins, hooks, deployment, research state and schedules were not changed. This page supersedes the chronological [earlier rounds](ACCEPTANCE_HISTORY.md).

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
| Native Codex App bridge | PASSED: production NativeAppPort with official installed stdio server, trusted caller/pipe, exact identity and thread read | native-app-real.local.json; no send or pipe discovery |
| Writer remote business adapter | PASSED: standalone MCP and own real SSH route, synthetic evidence SHA256 and outside-root rejection | writer-remote-real.local.json; no credentials/model call |
| S5 documentation, privacy, provenance, extension | Installation/update steps, unique owners and real Writer/Background Provider consumers documented; publication/provenance gates required before packaging | [Install](INSTALL.md), [workplace record](WORKPLACE_ACCEPTANCE.md), source-changes.json, build_candidate.py |

Private receipts remain ignored under `.verification/full-acceptance/`; the published workplace record contains bounded methods and hashes, without private profiles or research data. The current tested runtime is `harness-0.6-6f9532ef91a5cdae`, source root `6f9532ef91a5cdaec9cfc345f888039b64f29f39472d4c1288f3ecc0f44efe91`. Original supported baseline is `harness-0.6-cdf4c9cbb9e48fb2`, ZIP SHA256 `067da359bf8ee832fc03a0c4a81e8da8cf90a9fd58c6eb3da22d5b0d505f8d1d`. Strict protocol/workplace pairing remains unchanged; arbitrary old/new release interoperability is not claimed.

## Unverified coverage and concrete next checks

| Item | Status and reason | Completion evidence needed |
| --- | --- | --- |
| Native App send / paid Writer draft | NOT RUN; no messaging destination or paid execution is included in this engineering test. Existing integration/Provider fixtures passed. | Explicit authorized use-case and actual receipt; no claim of live message delivery or paid drafting. |
| Another physical Windows device | NOT AVAILABLE. Evidence is same-host clean installation, explicitly allowed as the clean-install scenario; it does not prove another OS image or machine. | An additional declared device if broader hardware coverage is requested. |
| GitHub Actions | PASSED on published commit `004fe29031f6b7f9b0e5807f20050e2806f39367`; Windows clean-host run completed all required steps. | [Run 38225690945](https://github.com/hhhheep/agent-relay/actions/runs/38225690945); later commits require their own CI result. |
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
