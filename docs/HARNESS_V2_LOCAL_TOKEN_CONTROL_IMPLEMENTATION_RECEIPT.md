# Harness V2 local token-control implementation receipt

Date: 2026-09-28  
Scope: local Solution Agent controller and Codex-worker dispatch path  
Authority: `HARNESS_V2_EVENT_DRIVEN_WAIT_TASKBOOK.md` and
`HARNESS_V2_TOKEN_EFFICIENCY_AND_CORRECTNESS_TASKBOOK.md`

## Implemented

### Deterministic supervision during execution and wait

- `supervise_remote()` no longer invokes `supervise_with_llm()` during normal
  scheduler supervision.
- Running, waiting, GPU-pressure, completed, and transport-unavailable states
  are reduced by deterministic code with `model_dispatch_count: 0`.
- Model review is explicit-only and is admitted only for the ambiguous
  `codex_confirm` route.
- An admitted review is deduplicated by a stable supervision fingerprint. The
  same unresolved state cannot consume a second model request.
- The scheduler daemon keeps the default no-model policy.

### Pre-provider Codex dispatch gate

- The local `worker=codex` path now atomically reserves a model request before
  invoking `codex exec`.
- The default task budget is one Codex model request and a maximum estimated
  prompt context of 24,000 tokens. A task may explicitly revise these values
  through its `model_budget` front matter; the runtime does not silently raise
  them.
- The same prompt/provider/model/phase fingerprint is rejected before provider
  invocation even when a larger request budget was declared.
- Repeated failure fingerprints are rejected before provider invocation.
- A crash after admission consumes the reservation rather than risking a
  duplicate billed request.
- Each admitted request writes one usage event with provider usage marked
  `unavailable` when the Codex CLI does not expose exact usage. It is never
  recorded as zero.

### Bounded model-facing output

- Full Codex stdout and stderr remain exact on disk.
- The `run_worker()` return value contains only stream path, byte count,
  SHA-256, return code, prompt hash, request ID, and next action.
- Raw stream bodies are no longer returned by the normal Codex-worker call.

No provider call, remote deployment, report generation, or research execution
was performed for this implementation.

## Acceptance evidence

Focused and affected-area suite:

```text
140 passed in 13.33s
```

This includes:

- a simulated 120-cycle/two-hour healthy wait with zero supervision-model
  dispatches;
- explicit ambiguous review delivered twice with one model dispatch;
- identical Codex prompt submitted twice with one actual `run_codex` call;
- request, context, duplicate-request, and repeated-failure preflight blocks;
- full raw stream persistence with compact returned metadata;
- existing WAIT, usage-ledger, MCP, retry, scheduler, supervisor, and
  remote-materializer regressions.

Complete repository regression through Research Token Guard:

```text
1599 passed, 25 failed, 1 skipped, 1 warning, 5 subtests passed
```

The 25 failure identities exactly match the verified pre-change baseline;
added regressions are zero.

```text
receipt: .token-guard/runs/20260928T154002Z-fc9d5a65/receipt.json
stdout sha256: 39e11f4e7b6b755f4334a8a14bd804602c97f6929b07156a85e4c5a6f901a691
receipt verification: valid
```

The local plugin no-API check remains valid with the fixed seven-tool manifest:

```text
tool_count: 7
canonical_schema_sha256: 3308fbd22615595e2d13a803d352397d6bbcc0c83adbf052f3b28682aee42d7a
```

## Deliberate boundary

This change controls model dispatches initiated by the local Solution Agent
runtime. It cannot intercept or suspend a Codex App Goal turn that the Codex
host starts outside this repository. No supported external Goal callback is
available.

For long execution, the supported token-safe path is therefore:

1. launch through the Harness/Solution Agent lifecycle;
2. let the deterministic scheduler or WAIT sidecar observe the job;
3. end the active Codex turn while the job is running;
4. resume on one explicit user check or a future supported host event;
5. do not use a repeating Codex Goal or model heartbeat as the monitor.

Automatic Codex App wake remains unclaimed. Remote deployment of these local
changes also remains outside this receipt.
