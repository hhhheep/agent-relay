# Harness v2 two-ended token contract

Token reduction is a property of the complete local/remote path.  Installing a
compact local MCP surface while leaving the remote side as an unbounded SSH
shell only moves the waste to another boundary.

The implementation and quantitative acceptance contract is defined in
`HARNESS_V2_TOKEN_EFFICIENCY_AND_CORRECTNESS_TASKBOOK.md`.  Event-driven waiting
is defined separately in `HARNESS_V2_EVENT_DRIVEN_WAIT_TASKBOOK.md`; both are
required before claiming steady-state weekly savings.

## Shared contract, different roles

| Phase | Local control side | Remote execution side |
| --- | --- | --- |
| Plan | Select one bounded stage and acceptance gate | Validate the same allowlisted execution spec |
| Input | Send paths, hashes, budgets, and compact evidence | Read declared `input_roots`; do not echo large inputs |
| Execute | Do not poll in a model loop | Run under lifecycle control and capture raw stdout/stderr locally |
| Observe | Receive compact state only | Token Guard retains full streams and emits a receipt |
| Collect | Decide from structured metrics and hashes | Hash artifacts, re-verify receipts, and return compact result |
| Diagnose | Request an exact excerpt only when required | Serve the requested exact excerpt from verified raw evidence |
| Report | Audit claims and commit through the file authority | Writer creates a draft, sidecar, usage, and publication packet |

The schemas for execution specs, receipts, artifact manifests, provider usage,
failure fingerprints, and publication packets must be identical on both sides.
Raw logs, large tables, report bodies, and provider responses stay at the side
that produced them unless a decision-critical excerpt is requested.

Each side also emits immutable, non-secret usage events under the same `run_id`,
`attempt_id`, and `spec_version`.  Collection merges events idempotently and
reports model turns, tool calls, context estimates, provider usage, retained raw
evidence bytes, and model-facing result bytes by phase and side.  Missing usage
is unavailable, never zero.

Authority, acceptance gates, and required scientific evidence are pinned exact
or reconstructed deterministically.  They are not replaced by an LLM summary.
A context budget may drop optional material; it must fail closed when required
material does not fit.

## Remote thin runtime

The remote thin runtime is installed below `solution_agent_root` and contains:

- the allowlisted execution wrapper and seven-tool MCP server (three bounded
  stage tools plus four profile-bound terminal tools);
- Token Guard receipt capture and verification;
- artifact hashing and repeat-collect verification;
- compact status/result serialization;
- the fixed report writer with restricted provider-env loading;
- a publication packet handoff to the existing ResearchFlow manager.

It is not a second scheduler, a second ResearchFlow database, or a replacement
for the project workplace. `project_workdir` remains the research workspace and
`remote_run_root` remains lifecycle storage.

## Deployment boundary

Remote deployment is a separate non-MCP control plane. It may transfer and
verify a release/bundle, run dry-run/apply, set up the checkout-local runtime,
register the plugin, open a fresh Codex task, and collect deployment receipts.
It must not expand the live MCP tool surface beyond:

- `stage_execute`
- `stage_status`
- `stage_collect`

The deployment control plane and remote thin runtime meet at a versioned,
hash-verified installation receipt. No API key or provider response body belongs
in that receipt.

The Harness server writes a deterministic manifest for its seven tools,
including ordered names, canonical schema bytes, and schema hash.  Host-wide
tools supplied by other plugins are a Codex host concern: they may be measured,
but Harness must not claim to reduce them without a supported and verified host
profile mechanism.
