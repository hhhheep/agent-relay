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
The unsafe valid-but-foreign-ID case remains reproducible at the raw CLI layer.
The next increment below adds Harness admission before invoking that CLI; raw CLI
behavior must not be mistaken for a Harness authorization check.

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

## Assignment contract under implementation

An optional declared `worker_pool` policy defines fixed worker handles, exact
task-to-direction membership, total active capacity and per-direction limits.
Absent policy preserves one-shot behavior. Invalid policy fails validation; it
must not silently disable enforcement. Pool policy changes require reconciliation
of existing job history, not automatic rebinding.

There is no new editable pool/session registry. The existing jobs root and its
`.launch.lock` own assignment. Each original `request.json` stores the selected
worker, direction, monotonically increasing per-worker sequence, exact predecessor
command and expected thread, with a digest also bound into the generated adapter
packet. Original submission lookup continues to use the base request fingerprint.
The ordered predecessor chain, not timestamps/names/free matching, establishes
the worker's identity. A busy/unknown predecessor cannot become a free slot.

Before Codex is invoked, the adapter revalidates the declared profile, original
request, packet assignment and predecessor chain under that same lock. It derives
the expected thread again from verified predecessor evidence. Modifying a request
to another valid thread must fail before a model request. Each new assignment
still has its own command, capsule unit, output directory and writer lease.

Reuse requires a terminal predecessor with full command-bound identity, durable
valid worker result files, successful Codex completion and an exact writer-lease
release receipt. Result acceptance remains a separate controller operation.
Interrupted, uncertain or unverifiable work is not automatically replaced.

Capacity is shared by managers using the declared same existing jobs root. The
pool packet pins that root, workplace, runtime root, Codex executable/home and
policy. Another jobs root or changed runtime/policy cannot be adopted implicitly.
This is not a claim to aggregate arbitrary independent deployments on a host.

A typed capacity rejection before a job directory is created is a proven
non-submission. Only that precise case may use the existing pre-executor rejection
and capsule rollback path; unknown runner errors retain original recovery rules.
No caller-facing arbitrary Codex thread parameter is added to `terminal_exec`.

The existing `terminal_exec` gains an optional `worker_pool_fingerprint` SHA256.
Automatic manager dispatch requires this field's schema capability, an expected
release and a pinned fingerprint of `pool_spec(profile, runtime_root)`. The remote
checks the pin again per request before capsule/job creation, avoiding a stale
handshake followed by an unintended one-shot launch. Old caller/new receiver
without a pool retains legacy behavior; new pooled caller/old receiver refuses
before submission. Exact release pairing remains mandatory; optional does not
mean arbitrary mixed-version compatibility.

Only already admitted `ready` publications may be dispatched by the manager's
existing loop. `resume.autoDispatch` is explicit and defaults off. It bounds
attempts per tick, checks current controller scope/epoch immediately before each
submission, and recovers original commands before assigning more work. A proven
`worker_pool_busy` pre-executor refusal can return to this queue; an ACK-lost
`prepared` entry cannot. Existing remote help/verification continuations use the
same pinned HarnessPort submission path.

## Opt-in configuration and current limits

The deployment profile may declare this synthetic example:

```yaml
worker_pool:
  version: 1
  max_running: 3
  workers:
    worker-a: alpha
    worker-b: beta
    worker-c: gamma
  scopes:
    alpha: {task_ids: [TASK-A, TASK-A-NEXT], max_running: 1}
    beta: {task_ids: [TASK-B], max_running: 1}
    gamma: {task_ids: [TASK-C], max_running: 1}
```

Task membership is exact and unambiguous; worker handles are bound to their
declared direction. The existing jobs ceiling must be at least the pool ceiling
and also counts non-worker OS jobs. Do not configure each of three managers with
its own independent three-worker deployment and call that a shared total of three.

Compute the pool fingerprint in the declared remote execution environment with
the existing read-only Harness command mechanism using
`digest(pool_spec(load_remote_deploy_profile(profiles_path, profile_name), root))`
from the owning modules. Pin that result and the actual verified release in the
manager's external `resume.autoDispatch` config, together with `maxPerTick`
(1..64) and `intervalMs` (1000..60000). Do not copy a thread ID from chat memory.
No live profile or manager configuration is changed by these source commits.

Local actual-CLI acceptance uses the explicitly supplied `HARNESS_TEST_CODEX`
binary with `tests/test_worker_session_cli.py`. The test uses an isolated Codex
home and a loopback synthetic Responses endpoint; it does not use account auth or
a paid model. It creates A and B, refuses a real but foreign B ID before any model
request for A, then resumes A with its prior history and excludes B's history.
CI without an explicitly supplied CLI skips only this optional fixture; it must
not claim that such a skip verifies real CLI or remote installed behavior.

Controller transfer extends the existing authority owner with a bounded batch
of exact, individually approved grants. The CLI accepts either the existing
single grant or an array. One SQLite transaction covers every requested scope;
invalid epochs, duplicate scopes/IDs or injected faults roll back the whole batch.
Existing single-grant receipts and tables remain unchanged and retries reuse
the original grant IDs. No wildcard direction transfer or new authority store is
introduced. Before the CLI transfers authority it verifies the successor App
context, requests the existing manager's normal release and acquires the original
queue owner lease. Acquisition is bounded; a live owner is never killed or
overwritten. Holding that same lease during the transfer prevents concurrent
manager dispatch from crossing the authority change. An unresponsive owner
blocks transfer. This does not restart remote commands. The successor bootstrap
path and combined fault scenario still require verification before handoff can
be declared complete.

Result contract/hash validation and ownership release allow a worker to accept
another independent eligible assignment. They do not attest scientific acceptance
or close a direction. Complete controller handoff, integration with existing result-review
receipts remain required work under the full goal. The combined synthetic scenario
now has executable coverage as described below; it does not prove Desktop handoff.

## Current local verification

Follow-up owner audit found that `publication-service.ts`, the existing manual
publish CLI consumer, must enforce the same controller authority before remote
maintenance or a new worker submission. The manager-loop guard alone leaves this
entry point available to an old controller after transfer. Extend the existing
scope/epoch checks to that consumer without changing journal IDs or requiring
the old origin chat to be online. Read-only recovery of an already known command
remains available. Verify stale/foreign callers and revocation during an awaited
operation before another submission; no new authorization store is needed.

This consumer repair is now implemented. Complete Lifecycle regression passed
again: 100 tests, no skips, including old-caller refusal before side effects,
revocation while awaiting registry ACK, successor submission with offline old
origin and read-only original-command recovery. A follow-up Python selection
passed 27 tests (pool, existing work-unit review and actual local CLI). The public
pooled-command recovery test confirms original artifact references, idempotent
recovery and `research_acceptance: not_assessed`; technical unit completion does
not promote working artifacts into authoritative artifacts. No new research
acceptance policy or receipt authority is introduced.

On Windows, the affected Python selection (pool, profiles, MCP, protocol/release,
builder, adapter, original-command reattachment, terminal commands and session
identity) passed: 151 tests, two existing POSIX skips. The explicitly supplied
actual local CLI fixture passed separately: one test, no paid API or account
credentials. The complete Lifecycle suite passed: 99 tests, no skips. Router
packaging and publication privacy passed. These are local development results;
Windows and Linux CI for pool/controller batch increment `d2ceb25` passed in
[run 38907709804](https://github.com/hhhheep/agent-relay/actions/runs/38907709804).
The later `2b2fab0` run 38908481753 passed Linux but failed Windows in the public
pooled recovery test (411 passed, three skips, one failure). The original assertion
indexed a missing `state` and hid the returned error envelope. The assertion now
retains the full envelope; no outcome assertion is relaxed. The same preceding
Windows test order passed locally (215 passed, two platform skips). Its CI-only
failure is not yet explained or waived; a new exact-commit run is required.

## Combined scenario and additional defects

`components/lifecycle/worker-scenario.test.ts` starts three separate Node manager
processes with independent queue owners/journals, sharing one remote jobs root.
Its test-only Python process bridge calls the existing public terminal tool,
worker adapter, capsule/lease, original request/claim, finalizer and Artifact store.
App/MCP transport and Codex outputs are synthetic; the execution launcher pauses
prepared commands for controlled completion. It is not an installed API, actual
worker supervision, authenticated Desktop handoff or remote installed acceptance.

Ten exact approved synthetic assignments write only their distinct output
directories. The test bounds active reservations to three, one per direction;
drops an ACK after durable reservation; restarts actual manager OS processes;
transfers alpha's exact scopes while beta/gamma authority remains unchanged;
rejects the old alpha caller; disconnects one result recovery; processes the late
result; and injects a real but foreign beta thread before alpha's next execution.
It checks original command IDs, three stable worker/thread identities, ten unique
commands/output directories, no duplicate submission after ACK loss, all results
recovered, and all execution leases released. Existing ReviewedStore proposal and
verification records explicitly accept each synthetic result afterward. Technical
completion alone leaves zero review incidents and no authoritative artifacts.
These explicit synthetic decisions do not claim scientific validation.

The scenario exposed queue overtaking: a proven capacity refusal was placed after
new ready rows. Eligible rows now retain original journal order, while rejection
rows requiring manual action cannot fill the bounded scan and hide later work.
Two direct regressions cover both fairness and a 129-row manual-rejection backlog.
The complete Lifecycle suite, including this scenario, passed locally: 103 tests.

A subsequent parallel regression reproduced a Windows launch-lock initialization
race: the first creator wrote a byte before acquiring the lock, after another
opener had already locked that byte. The existing lock owner now opens the same
file without a pre-lock write; the kernel lock also covers an initially empty
file. No lock file replacement, free matching, process kill or parallel lock
authority is introduced; old lock files remain usable. A controlled first-creator
versus contender schedule verifies mutual exclusion. Terminal/pool regression:
53 passed, two POSIX skips. The actual local Codex CLI fixture passed again after
this repair. Current-change CI remains required; this local race is not assumed
to explain the separate CI error envelope.

Remaining acceptance: final current-change CI, source/consumer delivery audit and
engineering integration. Real Desktop successor creation/usage telemetry and
automatic rollover are NOT AVAILABLE through the current NativeAppPort contract.
Remote installed behavior and real Desktop cross-chat acceptance are NOT RUN.
