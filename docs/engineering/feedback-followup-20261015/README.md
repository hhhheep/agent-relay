# Unresolved feedback: current-source follow-up

Baseline: engineering `5227660` after integrated maintenance `91c88cd`. Scope: verify outstanding feedback against maintained source, repair demonstrated defects in isolated worktrees, obtain candidate CI before advancing `codex/phase-one-restructure`. No main merge, deployment, live configuration, research state or original historical registry changes. Existing historical evidence remains private.

| Feedback | Current source / acceptance | Disposition |
| --- | --- | --- |
| HF-002 | `publication_registry`, `task_md_sync`, synthetic publication and CAS/tamper tests | Owned live declaration and publication handoff remain environment-specific; no new generic source defect established. |
| HF-003 / HF-006 | Public inventory/boundary fixtures, per-job materialization and output-root validation | Preserve immutable outputs and safety rejection; original remote failure cause and negative/repeat acceptance require the declared environment. |
| HF-008 | `remote_profile` validation rejects placeholder roots | No configured available workplace supplied; do not turn a configuration condition into a code fix. |
| HF-010 | Current MCP approval metadata and policy tests | App permission ownership retained; no global approval or sandbox changes. |
| HF-011 | Event wait/replay/recovery tests and current Lifecycle adapters | Synthetic event processing does not prove original-chat host continuation; no host API or live wakeup claimed. |
| HF-012 | Worker packets, declared artifact references and scoped inputs | Research prerequisite discovery/reuse is caller/project-owned; no established transport loss or generic source defect. |
| HF-013 | Bounded command stdout/stderr, raw transcript reader and recovery tests | Preserve raw evidence instead of stripping echoes. A distinct pre-input validation bug was reproduced and recorded as HF-021. |
| HF-014 | Terminal lock error and owner diagnostics suites | Prior source repairs retained; native Linux reboot/network and historical owner acceptance remain unverified. |
| HF-015 | Actual stdio MCP synthetic execute/collect/replay/closure test | Source atomic stage claim already exists; fresh App installation acceptance remains separate. |
| HF-016 | Existing preflight and distinct worker execution surface | Host visibility is not worker visibility or resource availability; no sandbox bypass or speculative scheduler. |
| HF-017 | Work-unit review/finish/continue tests | Previous source repair retained; no live task closure or research acceptance inferred. |
| HF-018 | Existing terminal slot configured_cwd was omitted from reattach identity checks | Reproduced and repaired in [dedicated record](../hf018-terminal-identity-20261015/README.md). Broader workspace/output ownership acceptance remains open. |
| HF-019 | Closed-client generation/readiness tests and original-ID lookup | Prior source repair retained; original App session reconnect still requires host-owned acceptance. |

## HF-021: validate read windows before interactive effects

Ordinary fix owned by `remote_terminal.ProfileTerminalManager`, called through the existing `terminal_send` MCP tool. On prior source, seven negative regression cases failed: negative cursor, zero/oversized output limits, invalid cursor text, boolean cursor/limit and a fractional limit. Bounds were checked only after load-buffer/paste-buffer/send-keys, or invalid types slipped through. Returning an error after input has executed can invite an unsafe duplicate retry.

The existing reader's validation is factored into one integer/bounds check, called before any interactive paste/control operation and again at raw-reader entry. Valid optional cursor, raw transcript evidence and byte limits remain unchanged. This adds no input/output fields, new tools, state migration or fallback/retry. Reverting this source commit restores the previous behavior; no persisted state is rewritten.

Related suites `test_remote_terminal`, `test_harness_four_fixes`, `test_terminal_commands`: 92 passed, two existing native POSIX skips. Synthetic tmux records prove rejected requests emit no paste or control keys; existing positive REPL/send tests remain active. Source publication and clean-host CI are required before engineering integration. Live terminal/network/Linux acceptance is NOT RUN.

Expanded audit selection initially reported 86 passed and five failures: three acquisition tests referenced absent private specs; two skill-routing tests referenced the former parent workspace and an undistributed legacy skill. These failures are retained for separate repair; they are not treated as a passing audit or silently skipped.

## HF-006: self-contained acquisition regression inputs

The three absent-spec failures also reproduce on unchanged engineering baseline `5227660`. Only test inputs are repaired: a public synthetic allowlisted acquisition spec replaces private workplace specs. The original CPU/source-only/checkout/asset-count assertions remain, with three new rejections for each output argument outside the declared roots. Acquisition, inventory and boundary suites pass 17 tests with no network, model download or research input. The acquisition suite is now part of the existing candidate CI selection. No engine, allowlist, output preservation or research method changes. Historical remote repeat acceptance remains open.

HF-018 candidate `3481329` passed clean-host CI [38745130905](https://github.com/hhhheep/agent-relay/actions/runs/38745130905); the earlier provenance failure remains visible in run 38744839276.
