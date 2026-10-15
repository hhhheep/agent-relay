# Harness maintenance integration

Target: `codex/phase-one-restructure`, starting at `4c4438b`. Candidate: `codex/harness-integration-20261015`. The merge preserves all individual fix and disposition commits; it does not rebase or rewrite the pre-existing four-fix commit. The reviewed heads are `7c6d85e` (ordinary work-unit lifecycle), `aceb0a2` (Manager health), and `3e993a6` (sanitized disposition index). Their shared history includes the other independently validated repairs. No textual conflict occurred; current provenance was regenerated for the combined sources while preserving the immutable extraction map.

Scope: original-command recovery, strict wait validation, worker phase/outcome presentation, self-contained publication/inventory fixtures, terminal-owner diagnostics, truthful work-unit decision state, closed-client readiness and stale Manager health. Each component repair has its own engineering record. This merge adds no runtime feature or live-state cleanup.

Local integration validation on Windows x64 / Node 24 / Python 3.13:

- CI core/synthetic update selection: 228 passed, two existing native POSIX skips.
- CI legacy workspace/namespace and Token Guard selection: 95 passed.
- Complete Lifecycle suite: 83 passed.
- Goal integration suites: eight passed.
- Provider/Writer/Background consumer suites using mock HTTP: 23 passed; no paid requests.
- Dependency `pip check`, target-native SQLite/in-memory source smoke build and 43-file router source build passed.
- Feedback lint, publication scan, current provenance and exact-byte diff checks passed.

Local dependencies are existing unchanged locked development environments referenced only inside the ignored worktree. GitHub clean-host CI independently reinstalls the declared dependencies and builds the source-only candidate. Local success alone is not a remote CI claim.

Publication: the candidate branch was pushed first. The GitHub connector could not create the proposed draft PR. The user then explicitly authorized pushing the tested engineering branch to trigger its existing push workflow; CI success remains required before declaring completion. The public default `main` branch is outside this operation. No installed plugin, hook, service, research task, live capsule/mailbox or runtime binding is modified. No force push is used.

The generated source-only package is a new runtime identity and cannot inherit the older P1 release's full M1-M4 installation certification. Native Linux, actual desktop reconnect/wakeup, machine reboot and live activation remain outside this integration check. Historical design-inventory baseline mismatch is unchanged and is not masked by editing its gate. Raw field evidence remains private. Newly received worker-stall observations require separate classification and reproduction; no unverified stall fix is included in this integration.

## Clean-host CI path-alias failure and repair

Initial run: https://github.com/hhhheep/agent-relay/actions/runs/38742834276 at `d3c5b4c`: privacy/provenance passed, core tests had 226 passed, two native POSIX skips and two failures. Windows short and long spellings of the same temporary workplace differed. The historical failed run is retained.

Ordinary fix, owners `task_md_sync` and `worker_help`: the receiver still resolves and confines the actual destination, materializes the immutable bundle and verifies every hash, but returns the declared workplace spelling expected by the existing exact-destination receipt check. The worker snapshot consumer returns a canonical directory consistently with `materialize`. No receipt field, state schema, protocol identity or path acceptance check is relaxed; no controller-side resolution of a remote path is introduced. Reverting these two source changes restores the old behavior without a state migration.

A real receiver subprocess regression uses an existing parent-directory alias to reproduce the spelling mismatch on either platform, verifies the actual file and rejects a substituted destination. The publication regression checks both canonical and alias workplace inputs. Affected local suites: `test_md_sync`, `test_publication_registry`, `test_harness_md_sync`, `test_harness_codex_worker`: 28 passed. The complete core selection then passed: 230 passed, two existing native POSIX skips. Clean-host CI must also pass on the repaired commit.
