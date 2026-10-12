# Harness V2 event-driven wait and resume taskbook

Implementation tracking: local P0/P1 work and its verification evidence are
recorded in `HARNESS_V2_EVENT_DRIVEN_WAIT_IMPLEMENTATION_RECEIPT.md`. Remote
P2/P3 deployment and shadow validation remain open.

Date: 2026-09-28

Status: required next-version engineering work

Related field issue: `HF-011`

Companion scope: `HARNESS_V2_TOKEN_EFFICIENCY_AND_CORRECTNESS_TASKBOOK.md`
defines full-path accounting, context/output budgets, model routing, and weekly
allowance acceptance.  This document remains the authoritative WAIT/event
contract; its A1 result is consumed by the companion ledger and must not be
weakened into a non-zero waiting budget.

## 1. Outcome

Replace model-driven waiting with a durable event-driven boundary across the
entire local/remote path.  When a run enters `WAITING_EXTERNAL`, no Codex,
Goal, Luna, Sol, report-writer, or other model turn may be started merely to
observe that the run is still waiting.  A deterministic monitor owns waiting
and may hand control back only on a material event:

- verified completion candidate;
- verified execution failure;
- stalled progress that requires a decision;
- explicit decision request;
- declared budget exhaustion.

The change must preserve the existing authority model:

- the solution-agent registry remains the task lifecycle authority;
- the remote workplace scheduler remains the launch and resource authority;
- ResearchFlow and local file owners remain publication authorities;
- the Harness remains an execution and evidence boundary;
- the live MCP surface is not expanded merely to implement waiting.

## 2. Observed defect

The current three-stage-tool surface advises the model to wait rather than poll, but
that text does not stop an outer Goal or continuation scheduler from starting a
new model turn.  Once a new turn starts, the controller tends to call
`stage_status` again.  The result is an agent-level polling loop even when the
inner monitor is quiet.

This is a control-plane defect, not a prompting defect.  Adding stronger prose
such as "do not poll" is not an acceptable fix.

## 3. P0 invariant: WAIT reaches the outermost scheduler

Entering `WAITING_EXTERNAL` must durably disarm automatic model continuation
for that run.  The following distinction is mandatory:

```text
program monitor tick       allowed; no model invocation
remote heartbeat           allowed; no model invocation
progress observation       allowed; no model invocation
material event reduction   allowed; deterministic code first
model turn                 forbidden until a wake condition is committed
```

The implementation must integrate with the outermost continuation mechanism,
not only `stage_status`, the remote runner, or `bounded_wait.py`.

If the active Codex surface does not expose a reliable external-event resume
API, the supported fallback is:

1. end the current controller turn after persisting the wait state;
2. let the Harness monitor continue outside the model process;
3. persist and optionally notify the terminal material event;
4. resume only on one explicit user turn or a future supported event callback.

Periodic Goal turns, heartbeat automations that invoke a model, or repeated
`stage_status` calls are forbidden fallbacks.  Automatic resume is optional in
the first implementation; zero model calls during WAIT is not.

## 4. State model

Use one canonical lifecycle with orthogonal observation and scientific state.
Do not overload one `status` field with transport, execution, evidence, and
scientific meaning.

### 4.1 Lifecycle state

```text
CREATED
  -> VALIDATED
  -> DISPATCHED
  -> RUNNING
  -> WAITING_EXTERNAL
  -> COLLECTING
  -> REVIEW_REQUIRED | CLOSED

Any non-terminal state may enter DEFERRED or CANCELLED by an authorized action.
EXECUTION_FAILED requires reliable terminal evidence; loss of observation is
not such evidence.
```

### 4.2 Orthogonal status fields

At minimum persist:

```yaml
execution_status: not_started | running | exited | failed | cancelled
observation_status: available | stale | unavailable
evidence_status: not_collected | collecting | complete | incomplete | invalid
scientific_status: not_assessed | hypothesis_supported | hypothesis_not_supported | inconclusive | invalid_evidence | not_applicable
```

`hypothesis_not_supported` is a valid research outcome.  It must never trigger
automatic repair, retry, or result-seeking execution.

### 4.3 Heartbeat separation

Persist separate timestamps and sources:

```yaml
monitor_heartbeat_at: deterministic monitor is alive
transport_observed_at: SSH or remote transport last responded
progress_observed_at: declared progress marker last advanced
process_observed_at: process identity was last verified
```

A fresh monitor heartbeat with stale experiment progress may become `STALLED`.
A stale transport observation becomes `OBSERVATION_UNAVAILABLE`; it does not
change the last verified execution state.

## 5. Versioned run specification

Every run starts from an immutable, hash-bound `run_spec` revision containing
the objective, allowed paths, command identity, completion contract, budgets,
and evidence requirements.

Minimum identity fields:

```yaml
run_id: stable logical run identity
spec_id: stable specification identity
spec_version: monotonically increasing integer
spec_sha256: hash of canonical serialized specification
attempt_id: one concrete execution submission
```

Revision records are append-only and contain:

```yaml
from_version: 1
to_version: 2
reason: exact reason for the change
operation: resume | execution_retry | code_repair | scope_change
diff_sha256: hash of the machine-readable diff
created_at: timestamp
```

The operations are distinct:

- `resume` reconnects to the same `attempt_id` and never submits work again;
- `observation_retry` retries only observation and does not consume an
  execution retry;
- `execution_retry` creates a new `attempt_id` only after verified failure or
  explicit authorization;
- `code_repair` changes code or command inputs, creates a new spec revision,
  and then creates a new attempt;
- `scope_change` changes the research question or acceptance contract and must
  never be hidden inside a retry.

Do not use one shared retry counter for these operations.

## 6. Durable event contract

Every wake-capable event must include:

```json
{
  "schema_version": "1",
  "run_id": "...",
  "attempt_id": "...",
  "event_id": "globally unique immutable id",
  "state_version": 12,
  "kind": "COMPLETION_CANDIDATE",
  "occurred_at": "RFC3339 timestamp",
  "producer_id": "remote-monitor",
  "payload_sha256": "...",
  "payload": {}
}
```

Required event kinds:

- `COMPLETION_CANDIDATE`
- `EXECUTION_FAILURE_VERIFIED`
- `OBSERVATION_UNAVAILABLE`
- `OBSERVATION_RESTORED`
- `PROGRESS_ADVANCED`
- `STALLED`
- `DECISION_REQUIRED`
- `BUDGET_EXCEEDED`
- `COLLECTION_VERIFIED`
- `COLLECTION_REJECTED`

`PROGRESS_ADVANCED`, heartbeat, and observation-restored events update state but
do not wake a model unless the run's declared policy explicitly requires a
decision.

## 7. Delivery, deduplication, and acknowledgement

Implement an outbox/inbox protocol or an equivalent crash-safe mechanism.
The event journal is a delivery record attached to the canonical registry; it
must not become a second scheduler.

Required behavior:

1. State transition and outbound event are persisted atomically or made
   replay-safe through a deterministic idempotency key.
2. Consumers enforce uniqueness for `(consumer_id, event_id)`.
3. Acknowledgement is written only after the state update and any collect
   request are durably committed.
4. Redelivery after missing acknowledgement is allowed.
5. Redelivery of an acknowledged event returns `duplicate: true` without
   launching a model, resubmitting execution, or collecting twice.
6. A stale `state_version` is acknowledged as stale with no state regression.
7. A version gap requests deterministic state reconciliation; it does not wake
   a model merely to inspect the gap.
8. Restart reconstructs pending delivery from the journal and resumes from the
   last committed version.

An acknowledgement record contains at least:

```yaml
consumer_id: outer-controller
event_id: immutable event id
received_at: timestamp
committed_state_version: 13
result: applied | duplicate | stale | quarantined
```

## 8. Observation loss is not execution failure

SSH timeout, MCP disconnect, stale status cache, monitor restart, and temporary
DNS or network failure produce `OBSERVATION_UNAVAILABLE` unless reliable remote
evidence proves process failure.

While observation is unavailable:

- retain the last verified `execution_status`;
- do not resubmit the experiment;
- do not increment `execution_retry_count`;
- retry observation with deterministic backoff under a separate budget;
- record the outage interval and evidence source;
- escalate only when a declared decision threshold is reached.

On reconnect, verify the existing run identity, process/job identity, spec hash,
and artifact namespace before resuming observation.  A reconnect must not create
a second job.

## 9. Completion is a verified transition

`DONE`, process exit, a result file, or a completion event only moves the run to
`COLLECTING`.  `CLOSED` requires deterministic verification of all declared
completion evidence:

- reliable exit status or an explicitly supported detached-job terminal proof;
- complete expected data range or sample coverage;
- all required artifacts present under declared roots;
- artifact sizes and SHA-256 values recorded and reverified;
- required Token Guard receipt and stream hashes valid;
- metrics schema and scientific evidence contract valid;
- no unexpected missing or extra critical outputs.

Collection writes `execution_status`, `evidence_status`, and
`scientific_status` independently.  An execution may be successful while the
scientific result is negative or inconclusive.  Only invalid or incomplete
evidence blocks close; disagreement with the hypothesis does not.

## 10. Deterministic handoff and model routing

Model calls are not part of the normal state transition path.

Deterministic code must first perform:

- schema and field comparisons;
- test pass/fail reduction;
- artifact completeness and hash checks;
- retry and budget accounting;
- event reduction and deduplication;
- generation of `run_state.json`, `receipt.json`, and the short handoff.

The human-readable `HANDOFF.md` is rendered from canonical fields and should
normally contain only objective, current state, last verified checkpoint,
artifacts, evidence status, open decision, and next action.  It is a view, not a
second source of truth.

Use model routing only after deterministic triage:

| Route | Allowed work |
| --- | --- |
| no model | waiting, heartbeat, field comparison, test reduction, handoff rendering, normal close |
| Luna | bounded prose normalization or ambiguous but low-risk triage from a compact receipt |
| Sol | statistical correctness, metric semantics, research interpretation, high-cost repair decision, conflicting evidence |

The model router must record the reason, selected evidence paths, and actual
provider usage.  It must not send raw logs by default.

## 11. Outer controller integration

The current `stage_execute`, `stage_status`, and `stage_collect` tools remain
available and backward compatible.  Event-driven waiting is an internal
controller/runtime capability, not a requirement for more model-facing tools.

Expected integration points include:

- `solution_agent/harness/mcp_server.py`: return a durable wait disposition and
  compact state without encouraging a status loop;
- the registry/event layer: canonical version, event journal, acknowledgements,
  and idempotency keys;
- the scheduler/daemon: deterministic monitor registration and recovery;
- the remote runner/collector: separate observation loss, terminal execution,
  and verified completion;
- the outer Goal/continuation adapter: suppress model continuation while a run
  is waiting and accept at most one material wake event;
- `solution_agent/continuation/bounded_wait.py`: it may remain an internal
  bounded monitor helper, but it must never be driven once per model turn.

If no supported outer Goal adapter exists, document that boundary explicitly
and ship the manual-resume fallback.  Do not claim automatic event resume.

## 12. Suggested implementation phases

### P0: contracts and failing acceptance tests

- Add versioned run, state, event, acknowledgement, completion, and revision
  schemas.
- Add an instrumented model-dispatch spy/counter.
- Write all five required acceptance scenarios before changing runtime logic.

### P1: local durable monitor

- Persist wait registration outside the model process.
- Add crash-safe event journal, deduplication, acknowledgement, and restart.
- Generate deterministic state and handoff artifacts.

### P2: remote observation and collection

- Separate transport, monitor, process, and progress observations.
- Reattach by stable run/attempt/job identity.
- Verify completion and hashes before close.

### P3: outer continuation adapter

- Stop automatic Goal turns on `WAITING_EXTERNAL`.
- Resume once on a committed wake event when the surface supports it.
- Otherwise deliver the manual-resume fallback without periodic model calls.

### P4: bounded model routing

- Add no-model/Luna/Sol routing after deterministic triage.
- Keep statistical and scientific review separate from execution repair.

Each phase must preserve existing stage, terminal, deployment, ResearchFlow,
and report-writer tests.

## 13. Required acceptance tests

The first release is not complete until all five scenarios pass with durable
evidence.

### A1. Zero model calls during wait

Simulate a two-hour healthy computation with monitor heartbeats and progress
updates.  From the committed transition into `WAITING_EXTERNAL` until the first
material wake event:

```text
controller model calls = 0
worker model calls = 0 unless the declared execution itself is a model stage
stage_status calls issued by a model = 0
```

Use an instrumented dispatcher counter, not an estimate from log length.

### A2. Event redelivery is idempotent

Deliver the same completion event ten times.  Assert:

- one inbox application;
- one collect transition;
- zero duplicate experiment submissions;
- at most one permitted review-model invocation;
- nine duplicate acknowledgements.

### A3. Controller restart reattaches

Restart the controller after dispatch and before completion.  Assert that it
restores the same `run_id`, `attempt_id`, remote job identity, state version,
and pending event position without resubmitting work.

### A4. SSH loss does not rerun

Make observation unavailable and later restore it while the remote process
continues.  Assert `OBSERVATION_UNAVAILABLE`, unchanged execution-attempt count,
no new scheduler submission, and successful reattachment to the same job.

### A5. Completion and negative-result semantics

Test both cases:

1. zero exit with missing/incomplete artifacts enters `COLLECTION_REJECTED` and
   cannot reach `CLOSED`;
2. complete, hash-verified evidence that does not support the hypothesis enters
   review and may close as `hypothesis_not_supported` without repair or rerun.

## 14. Additional regression cases

- out-of-order events cannot regress state;
- two consumers may acknowledge independently without duplicate effects;
- a completion event for the wrong attempt is quarantined;
- a changed spec hash cannot silently resume an older job;
- heartbeat freshness cannot substitute for progress;
- progress stalling cannot be inferred from SSH loss alone;
- model routing receives only declared compact evidence;
- raw logs remain retrievable by exact bounded evidence query;
- existing collect hash revalidation remains mandatory;
- negative and inconclusive outcomes never enter automatic repair.

## 15. Non-goals

- Do not adopt Temporal, OpenHands, Microsoft Agent Framework, DSH, or another
  orchestration runtime in the first release.
- Do not replace the existing scheduler, registry, ResearchFlow, or file owner.
- Do not add a second task database.
- Do not make an LLM the final launch, retry, completion, or close authority.
- Do not redesign remote deployment, terminal commands, report writing, or the
  scientific task itself.
- Do not use polling automations that wake a model as an event system.

The implementation may borrow patterns from these upstream sources without
taking runtime dependencies:

- 12-Factor Agents pause/resume and owned control flow:
  <https://github.com/humanlayer/12-factor-agents/blob/main/content/factor-06-launch-pause-resume.md>
  and
  <https://github.com/humanlayer/12-factor-agents/blob/main/content/factor-08-own-your-control-flow.md>
- OpenHands stuck detection:
  <https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/stuck_detector.py>
- Microsoft Magentic progress ledger:
  <https://github.com/microsoft/agent-framework/blob/main/python/packages/orchestrations/agent_framework_orchestrations/_magentic.py>
- Temporal durable agent pattern samples:
  <https://github.com/temporalio/samples-python/blob/main/openai_agents/agent_patterns/README.md>

## 16. Deliverables

The implementation task must deliver:

1. versioned schemas and migrations or compatibility adapters;
2. durable wait/event/acknowledgement implementation;
3. outer continuation suppression or the documented manual-resume fallback;
4. deterministic completion verifier and handoff renderer;
5. observation-loss/reconnect implementation;
6. separated operation counters and revision ledger;
7. focused unit, restart, fault-injection, and integration tests;
8. a no-provider-call smoke and one bounded remote shadow run;
9. receipts proving A1 through A5;
10. documentation updates to the architecture, two-ended token contract, and
    operator handoff after implementation is verified.

## 17. Stop conditions

Stop and report a concrete blocker rather than simulating success when:

- the outer surface cannot suppress automatic model continuation and no manual
  zero-call fallback can be used;
- event state cannot be committed or replayed without duplicate submission;
- remote identity cannot be matched to the original attempt;
- completion evidence cannot distinguish incomplete output from a valid
  negative result;
- a proposed change would create a second lifecycle authority;
- existing stage collection or artifact-hash verification regresses.

## 18. Short handoff for the implementation task

Implement only the event-driven WAIT/resume upgrade defined here.  Start with
failing A1-A5 tests and preserve the existing scheduler, registry, three-stage
tool contract, terminal capability, ResearchFlow, and report-writer boundaries.
WAIT must suppress the outer Goal/model continuation, not merely print advice.
Use crash-safe event delivery with deduplication and acknowledgement; keep
observation loss separate from execution failure; treat completion as a
verified COLLECT transition; and allow complete negative scientific results to
close without automatic repair.  If automatic external wake is unavailable,
ship and prove the zero-model-call manual-resume fallback instead of polling.
