# Worker session reuse and controller handoff

Status: implementation in progress; no live activation, remote acceptance or
automatic Desktop chat rollover. Source baseline: `c88daf2`.

## Requirement and authority

The authorized goal is fixed-capacity workers accepting successive assignments
in the same verified Codex thread, with independent command receipts, explicit
result acceptance, and scoped local controller handoff. Model memory must never
select or reconstruct the target thread. A valid but foreign thread ID must be
rejected before a model request. Existing workplace, exact release-pair,
task/unit, command recovery, CAS and writer-lease gates remain mandatory.

This is a state/contract extension, not an ordinary retry fix. Its owners are:

| Owner | Responsibility |
| --- | --- |
| `solution_agent/harness/codex_worker.py` | CLI execution, machine-observed thread identity and worker result receipt |
| `solution_agent/harness/terminal_commands.py` | Command identity, durable raw streams, execution lifetime and atomic job admission |
| `solution_agent/harness/mcp_server.py` | Existing public tool, exact peer identity, capsule transitions and artifact collection |
| `solution_agent/harness/remote_profile.py` | Declared workplace/runtime and execution policy |
| `components/lifecycle/task-publisher.ts` | Durable authorized publication and original-command recovery |
| `components/lifecycle/manager-driver.ts` | Existing manager loop; bounded assignment and recovery |
| `components/lifecycle/controller-authority.ts` | Per-task/unit controller and epoch transfer |
| `components/lifecycle/session-lifecycle.ts` | Genuine App context and manager ownership transfer |

The remote execution owner is authoritative for session ownership and busy state.
The local publication journal retains assignment intent and references. Do not add
a second editable session registry, queue database, scheduler service, transport,
or raw SSH fallback. Reuse must remain within the declared authorization scope.

## Required behavior

1. The adapter captures `thread.started.thread_id` from the original supervised
   command's full JSONL stream. A bounded preview and assistant prose are not an
   identity source. Missing, malformed, ambiguous or foreign identity is not
   reusable. Preserve the original execution even when identity cannot be proven.
2. Persist command/request/stream evidence before any later reuse. A worker handle
   resolves its thread through the execution owner, never through caller-supplied
   arbitrary thread IDs, names, `--last`, account history search or model memory.
   Check authorized scope, binding revision and exclusive use before spawning
   Codex; compare observed identity afterward as a second check.
3. Preserve a distinct task/unit, command and immutable receipt for each
   assignment. Unknown submission is reconciled, never blindly retried. The same
   worker cannot accept another assignment until results are durable and execution
   ownership is released. Busy/unknown workers still consume their reservation.
4. Distinguish a workplace-wide limit from direction limits. Existing
   `max_concurrent` covers OS jobs sharing one jobs root, not arbitrary profiles or
   agent sessions. Do not silently claim a global pool across separate jobs roots.
   Keep one active work unit per capsule and existing write isolation.
5. Execution finished, result awaiting review, accepted work and direction closure
   remain separate. Independent eligible work may proceed while results await
   review; dependent or conflicting work waits.
6. Verify a successor chat before transferring only its approved task/unit scopes.
   Preserve worker/thread/command IDs, journal and result cursor. Retire old
   authority, not remote execution. Resolve bootstrap admission and multi-scope
   transfer explicitly; a new SessionStart must not evict an unrelated manager.
7. Enable automatic rollover only with complete host usage, resolved compaction
   threshold and measured handoff reserve. Missing host capability is a blocker;
   local log scraping, guessed percentages and synthetic chat events do not prove
   real Desktop automation.

## Incremental delivery and compatibility

First capture strict, command-bound session evidence in the existing command
completion manifest and recovered receipt, with malformed/ambiguous/wrong-command cases. This alone
does not enable reuse or provide an assignment-to-session authorization guard.
Then wire authoritative worker binding, atomic assignment, explicit CLI resume,
capacity, acceptance, controller transfer and recovery through the owners above.
Every new module must have a real consumer in that path.

Legacy one-shot commands and their original claims remain recoverable. Missing
session evidence on old commands means non-reusable, not permission to infer an
ID. Do not mutate old claims, repurpose work-unit IDs, scan another user's history
or treat an old worker process as a newly available slot.

Public tool/profile/receipt changes require sender/receiver and builder coverage;
optional fields do not imply compatibility. Retain protocol 0.6 exact release and
schema fingerprints. No mixed-version reuse promise is made. Session bindings
must be opt-in until end-to-end acceptance and deployment authorization.

Rollback disables new assignment/reuse and preserves old execution state and
receipts. It does not reset bindings, overwrite unknown state, kill workers or
replay commands. A committed controller transfer is reconciled forward; do not
silently resurrect the old controller.

## Verification and completion

- Negative cases: valid-but-wrong thread rejected before model request; missing,
  multiple, corrupted and truncated identity events; altered binding; wrong
  workplace/runtime/command; lost ACK; duplicate assignment; busy session;
  competing managers; old controller epoch rejected.
- Actual local Codex CLI against synthetic loopback Responses: explicit resume,
  correct prior history, no foreign history, original command/full-stream
  recovery, and no real credentials or paid provider.
- Three fixed workers, ten authorized synthetic assignments across three
  directions: scoped controller handoff plus disconnect, late completion,
  restart and wrong-ID injection. Prove capacity, single execution and complete
  result attribution/review. Real Desktop cross-chat acceptance remains separate.
- Affected regression suites, publication privacy, provenance, package/contract
  checks and CI; update the root README, feedback index and delivery evidence.
  Use the repository's required pre-push gate; no bypass or weakened checks.

NOT RUN: deployed remote CLI/session compatibility, real Desktop automatic
rollover, school network, paid models and live activation. The overall goal
remains incomplete until all required behavior has authoritative evidence.

## Current implementation evidence

The existing terminal finalizer records `worker_session` only for worker-linked
commands. Observation checks the original request, worker claim, task/unit,
command ID, runtime release and complete raw stdout hash. Exactly one canonical
UUID event is required; nested prose, duplicate fields, duplicate events and
incomplete/oversized streams are refused. A fresh command reader recomputes the
observation and rejects changed evidence. No CLI/MCP input or dispatch choice is
added in this increment. `reuse_authorized` is always false, even when observed.
Old manifests without this field keep their prior read behavior; no historical
claim or completion manifest is rewritten by reading.

Local real-CLI/synthetic-loopback integration confirms the new terminal path
recovers the same thread ID despite a truncated preview. This is a Windows
fixture using Python as its command shell, not deployed Linux acceptance.
The unsafe valid-but-foreign-ID case remains reproducible at the CLI layer;
pre-request authoritative binding and reuse are still pending, not claimed fixed.

Validation for this increment: 117 Python tests passed, two existing native-POSIX
bash tests skipped on Windows. Suites: worker session identity, Codex worker,
worker job reattach, terminal commands, MCP, task capsule and help resume.
The actual local CLI fixture also passed nine identity/history/recovery checks.
Its raw local paths, session IDs and receipts are not publication inputs.

The controller prerequisite is independently repaired: `assertController` now
checks caller identity at epoch zero as well as after transfer. The new negative
test failed on the prior code with a missing expected exception. Direct routing
tests also require absent/foreign callers to be blocked before native dispatch.
The normal routing fixture now supplies the same verified caller identity that
the production manager supplies; the guard itself is not relaxed.
Full Lifecycle validation after this change: 90 passed, no failures or skips.
This does not implement bootstrap admission, session reuse or automatic rollover.
