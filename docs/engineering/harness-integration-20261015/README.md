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

Local dependencies are existing unchanged locked development environments referenced only inside the ignored worktree. GitHub clean-host CI must independently reinstall the declared dependencies and build the source-only candidate before the engineering branch is advanced. CI evidence belongs to the attached integration pull request/checks; local success alone is not a remote CI claim.

Publication procedure: push the candidate branch, validate its pull-request CI against the existing engineering base, then fast-forward the engineering checkout and push the same reviewed candidate. The public default `main` branch is outside this operation. No installed plugin, hook, service, research task, live capsule/mailbox or runtime binding is modified. No force push is used.

The generated source-only package is a new runtime identity and cannot inherit the older P1 release's full M1-M4 installation certification. Native Linux, actual desktop reconnect/wakeup, machine reboot and live activation remain outside this integration check. Historical design-inventory baseline mismatch is unchanged and is not masked by editing its gate. Raw field evidence remains private. Newly received worker-stall observations require separate classification and reproduction; no unverified stall fix is included in this integration.
