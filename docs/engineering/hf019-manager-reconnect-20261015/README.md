# HF-019: invalidate a closed Manager MCP client

Base `3f86535`; branch `codex/hf019-manager-reconnect-20261015`. Ordinary lifecycle fix in `HarnessPort` and its direct Manager consumer. A synthetic EOF reproduced the defect: the adapter retained the old client and admitted another recovery call instead of requiring client reconstruction. This is a concrete source defect distinct from the unverified original desktop-chat reconnect scenario.

The existing MCP close callback now invalidates only its own client generation. A close during initialization cannot publish a connected client, and a delayed old close cannot clear a newer session. The Manager skips disconnected Harness scheduling and reports its existing `harness_reconnect_required` state instead of falsely returning `ready`. Durable publication and command identities are unchanged.

An explicit owner-controlled call to the existing connect entry creates a fresh MCP client from the declared installed configuration; subsequent recovery uses the original command ID. Tests verify no command/worker payload replay and preservation of the existing expected-runtime rejection. The configured launcher and protocol 0.6 exact workplace/runtime pairing remain unchanged. No transparent transport bridge, automatic SSH retry, new scheduling framework, live reconnect policy or App reload is introduced. Original App-session auto reconnect and HF-011 host wakeup remain unverified integration capabilities, not completed by this commit.

Validation in `components/lifecycle`:

```text
node --import tsx --test harness-disconnect.test.ts harness-port.test.ts task-publisher.test.ts publication-service.test.ts failure-isolation.test.ts resume-controller.test.ts resume-scheduler.test.ts
23 passed, 0 failed
```

The worktree uses its original lockfile for isolated Node dependencies and an ignored reference to the formal repository's existing test Python environment. Initial direct-consumer attempts failed because the worktree lacked that declared interpreter; the tests passed after supplying it without changing assertions. A restricted-sandbox temporary-file rename failure is separately retained as an environment limitation. Publication, provenance and diff checks pass. Existing CI discovers the new test via its lifecycle test glob. No Python runtime, tool schema, persisted journal schema or generated binding changed. Rollback preserves journals and original command recovery; it reintroduces stale-client readiness reporting. Native Linux and actual network/App-session interruption are NOT RUN. No deployment or live state was changed.
