# Solution Agent Harness V2

## Active engineering taskbooks

`HARNESS_V2_INTEGRATED_FOCUS_RUNTIME_TASKBOOK.md` is the sole authority for
end-to-end completion.  It owns source/runtime identity, Task Capsule
integration, event-driven WAIT, selective artifact reading, deterministic
materialize/compose/patch, terminal command-result boundaries, deployment, and
the final acceptance definition.

`HARNESS_V2_INTEGRATED_FOCUS_RUNTIME_IMPLEMENTATION_DRAFT.md` is its subordinate
file-level implementation blueprint. It records the concrete module tree,
proposed APIs, patch order, and vertical slices; it cannot lower or replace the
completion gates in the authoritative taskbook.

The current architecture also has component taskbooks:

- `HARNESS_V2_EVENT_DRIVEN_WAIT_TASKBOOK.md` owns durable wait, event delivery,
  resume, observation loss, and verified completion;
- `HARNESS_V2_TOKEN_EFFICIENCY_AND_CORRECTNESS_TASKBOOK.md` owns full-path usage
  accounting, bounded context/output, model and tool budgets, two-ended
  aggregation, and the weekly operating gate.
- `HARNESS_V2_REMOTE_TERMINAL_TASKBOOK.md` owns the profile-bound interactive
  terminal component;
- `CONTEXT_GATEWAY_PLAN.md` owns the existing context-pack design baseline.

`HARNESS_V2_TWO_ENDED_TOKEN_CONTRACT.md` is their shared local/remote boundary.
All component taskbooks and implementation receipts are subordinate evidence.
Passing their tests alone is not an end-to-end completion, active-runtime, or
subscription-allowance savings claim.

## Outcome

Keep `solution-agent` as the control plane and make evidence-preserving bounded
stages its only high-output execution path. The harness must reduce repeated
model context without hiding raw evidence or weakening scientific gates.

## Authority boundaries

- `solution-agent` owns task identity, lifecycle, remote dispatch, approval,
  registry events, collection, and the final task state.
- Research Token Guard owns raw stdout/stderr capture, stream hashes, bounded
  receipts, declared-input fingerprints, and exact evidence retrieval.
- The context gateway owns deterministic selection under a hard preflight
  budget. Estimates are labelled as estimates and are never reported as API
  billing usage.
- Provider responses own measured input, cached input, output, total token, and
  cost observations. Missing required usage fails closed.
- The append-only per-run usage ledger owns phase/side aggregation. It stores
  measurements and hashes, not prompt bodies, response bodies, or credentials.
- The Bailian report writer writes one canonical report plus a structured claim
  sidecar. Codex performs one evidence audit and does not create a competing
  full report.

## Single state model

The solution-agent registry remains the lifecycle authority. A Token Guard
ledger is an evidence attachment referenced by a registry event, not a second
task scheduler. Every terminal stage records `go`, `stop`, or `defer`, a verified
receipt, artifact hashes, and a short handoff.

## Integrated tool surface

The deployed v0.5 baseline exposes seven fixed MCP tools. The integrated target
keeps those lifecycle boundaries and adds explicit command-result and artifact
read/write outcomes. The canonical target surface and migration rules live in
`HARNESS_V2_INTEGRATED_FOCUS_RUNTIME_TASKBOOK.md`; do not infer completion from
the older seven-tool count.

The bounded stage surface is:

1. `stage_execute` validates and launches one bounded stage.
2. `stage_status` returns only material state changes and compact usage.
3. `stage_collect` verifies receipts and artifacts, then emits the handoff.

The profile-bound interactive baseline is:

4. `terminal_open` creates or attaches to one declared fixed terminal.
5. `terminal_send` sends bounded input and returns bounded incremental output.
6. `terminal_read` returns a bounded sanitized preview.
7. `terminal_close` detaches or explicitly terminates the managed terminal.

Report drafting is a stage kind, not six additional top-level tools. Raw log
retrieval is an explicit evidence query through `stage_collect`.

The integrated target additionally separates normal command execution from raw
interactive terminal streaming and provides one read-only artifact inspection
surface plus store/apply write surfaces. Reads and writes remain separate, and
every follow-up uses stable artifact IDs and hashes.

## Borrowed designs

- Inspect AI: per-sample token, turn, time, working-time, and cost gates; use it
  as an independent regression evaluator rather than the production scheduler.
- Aider: symbol-level repository map ranked by dependency relevance and bounded
  by an explicit context budget.
- SWE-agent: bounded file views, succinct search results, edit-time validation,
  and explicit empty-output success.
- MCP Python SDK: typed narrow tools and structured outputs.
- OpenInference: compatible usage field names without storing prompt bodies by
  default.

## Non-negotiable acceptance gates

- Existing execution-port and remote lifecycle tests continue to pass.
- Required evidence is never silently dropped to satisfy a context budget.
- Provider token claims use provider usage, not byte or character estimates.
- A/B savings claims require answer-equivalence and cache-balance checks.
- Repeating the same failure fingerprint cannot consume another model retry
  unless a declared input, command, or validator version changed.
- Remote deployment is packaged but not activated until a new Codex task loads
  the plugin and completes a no-API smoke test.
