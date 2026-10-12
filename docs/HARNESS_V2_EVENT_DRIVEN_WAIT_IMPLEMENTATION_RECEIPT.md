# Harness V2 event-driven WAIT implementation receipt

Date: 2026-09-28  
Scope: local P0/P1 implementation and regression verification  
Authority: `HARNESS_V2_EVENT_DRIVEN_WAIT_TASKBOOK.md`

## Implemented

- `stage_execute` persists a canonical `WAITING_EXTERNAL` state and launches a
  deterministic no-model sidecar monitor.
- The model-facing surface remains the existing stage tools; no wait/poll tool
  was added.
- A waiting response carries `suppress_model_continuation: true` and
  `next_model_action: END_TURN`.
- Every job owns durable `wait/` artifacts:
  - `state.json`: canonical versioned lifecycle and orthogonal execution,
    observation, evidence, and scientific status;
  - `events/*.json`: immutable hash-bound event envelopes;
  - `acks/*.json`: post-commit compact acknowledgements;
  - `receipt.json`: compact deterministic handoff receipt and risk route;
  - `HANDOFF.md`: rendered view of canonical fields;
  - `monitor.pid`, `monitor_launch.json`, and `monitor_heartbeat.json`: sidecar
    ownership and liveness evidence.
- Event application is replay-safe by `event_id`, `attempt_id`, and
  `state_version`. Duplicate completion delivery does not submit or collect
  twice.
- Completion candidates transition only to `COLLECTING`; deterministic
  collection verification is required before `CLOSED`.
- Missing or invalid evidence enters `REVIEW_REQUIRED`. A complete result that
  does not support the hypothesis may close as
  `hypothesis_not_supported`; it is not retried or repaired automatically.
- Observation loss updates observation state and its own counter only. It does
  not change the last verified execution state or submission count.
- The stuck breaker requires all of: a declared threshold, fresh observation,
  a running execution, and stale declared progress. It emits at most one
  `STALLED` event. SSH loss alone cannot trip it.
- Deterministic risk routing defaults to `no_model` for normal transitions and
  `human` for undeclared review work. Luna or Sol is selected only when the
  event explicitly declares a compatible decision type/risk.

## Correctness repair discovered during implementation

The former Windows liveness check used `os.kill(pid, 0)`. On Windows that call
is not a safe Unix-style existence probe and could terminate the observed
process, producing a false `lost_process`. The implementation now uses the
read-only Windows `OpenProcess` / `GetExitCodeProcess` API. Linux and macOS keep
the normal signal-zero probe.

## Verification

Focused WAIT acceptance tests:

```text
7 passed in 1.22s
```

Covered scenarios:

1. simulated two-hour healthy run with 120 deterministic progress markers and
   zero model dispatches;
2. one completion event delivered ten times: one application and nine duplicate
   acknowledgements;
3. controller restart reloads the same run and attempt without resubmission;
4. observation loss and restore do not submit another execution;
5. incomplete collection cannot close, while a complete negative result can;
6. durable event, acknowledgement, compact receipt, and handoff artifacts;
7. one-shot stuck breaker and the observation-loss exclusion.

Broader affected-area regression:

```text
149 passed, 1464 deselected in 13.29s
```

The selected suite covers Harness, execution port, continuation bounded wait,
the MCP no-API smoke, and the Windows process-liveness path.

## Deliberate boundary

This release does **not** claim automatic Codex Goal wakeup. No supported outer
Goal callback has been established. The safe supported behavior is therefore:

1. persist `WAITING_EXTERNAL`;
2. end the current model turn;
3. let the deterministic sidecar monitor without model calls;
4. resume through a supported external event adapter when one exists, or by one
   explicit manual check;
5. never use periodic Goal turns, model heartbeats, or repeated model-driven
   `stage_status` calls as the fallback.

Remote P2/P3 deployment, SSH fault injection, monitor recovery after host
restart, and one bounded remote shadow run remain required before calling the
two-ended deployment complete.

## Local controller follow-up

The local Solution Agent scheduler and Codex-worker dispatch path now enforce
no-model normal supervision, explicit-only deduplicated ambiguous review,
pre-provider request/context budgets, duplicate-request blocking, and compact
stream metadata. Verification is recorded in
`HARNESS_V2_LOCAL_TOKEN_CONTROL_IMPLEMENTATION_RECEIPT.md`.

This follow-up still does not provide a Codex App external Goal callback. Its
hard gate applies to Solution Agent initiated model dispatches; repeating Codex
Goals remain an unsupported monitoring mechanism.
