# Delegate through the existing connection

Local Codex manages; remote Codex implements/executes; Harness transports,
supervises and collects. A new research method does not require a new Harness
tool, core registry entry or release. Keep research code in the workplace.

## Before submitting

This section gates worker submission only. For an authorized install/repair,
missing MCP is a reason to use the declared maintenance/deployment route, not to
stop all work. Build/install the new release first; request App reload only when
the installed artifacts are ready. Do not ask the user to reload an old package.

For an authorized client installation or repair, build the profile client with
`build_profile_plugin.py --profile <exact remote profile key> --control-root
<declared local control directory>`. A working installed client does not need
rebuilding before each worker submission. The build installs
the release-owned `remote_launcher.py`. Each MCP client creates its own SSH
connection to the same declared workplace. There is no workplace transport lock
held for a session. New starts share a bounded per-user endpoint budget and persisted failure cooldown; admitted chats retain independent stdio.
Each launcher attempts SSH once, without an automatic reconnect loop. Throttling, timeout or unavailability must not trigger raw SSH/SCP, legacy run_remote, or rapid client reopening. Keep the original task/command IDs; use the existing MCP connection for status/collection/recovery when available. Explicit reconnect must respect retry-after and recover original IDs before deciding to resubmit. Task and
interactive-terminal writer leases still apply. Its absolute launcher path belongs to the generated
client artifact: retain that artifact while installed. Never copy the old
external SSH/SCP launcher into the formal MCP entry. The maintenance wrapper
remains available separately. The server computes its own identity before
serving tools; client-provided "observed remote identity" is not evidence.

Check the actual tool schema contains `terminal_exec.worker_task`, and that the
current connection passes the installed protocol/release gate. Source files or
a new ZIP do not prove the App or remote process loaded them. If the argument is
absent, report `CLIENT_UPDATE_REQUIRED`; do not improvise a shell Codex launch.
Use the declared profile and the existing task identity. Do not start a parallel
research task to escape an old task's pending state.

A task's `source_release_id` records its creation, not a runtime pin. Supported
capsule schema 1 survives compatible patch releases without rewriting that field
or allocating a replacement task. New jobs record `runtime_release_id`; recovery
retains the job's original value. The client/runtime activation gate still applies.

Read the user's taskbook. Send its relevant Markdown with exact inputs or pinned
artifact references, allowed changes, prohibited actions and acceptance checks.
Distinguish user-required methods and scientific conditions from implementation
choices proposed by Codex. Carry unverified choices as assumptions in the worker
packet, rather than turning them into extra prohibitions. When evidence changes
an implementation choice, the manager can revise it within the authorized scope
and update the taskbook; changing scientific conditions requires the appropriate
research decision. A terminal follow-up still uses the lifecycle below.
Resolve missing paths using bounded reads; ask only about consequential ambiguity
or missing authorization. Missing implementation is work for remote Codex, not a
request for the user to write a script or execution spec.
The adapter uses the profile's `codex_path` and `project_workdir`, inherits the
remote Codex authentication/model settings, and explicitly uses workspace-write
sandbox. It does not select another model, bypass approvals or provision keys.
If sandbox/login/profile restrictions prevent execution, report them; do not
retry with broader access. Task wording does not enforce filesystem permissions
inside the workplace; only delegate changes authorized by the user.

## Match phase to the authorized deliverable

Choose the phase explicitly before submitting; the tool default remains
`prepare_code`. Check the requested deliverable, required checks and resource
authorization against the phase:

- `prepare_code`: implementation and lightweight validation; actual execution
  remains a later step when required by the task.
- `execute_run`: authorized execution of already-prepared work.
- `full`: implementation, execution and checks are all authorized. A bounded
  complete CPU task may use this in one invocation; do not force it into two
  phases. Long or expensive experiments retain their approved staging/budget.

A phase is not permission to use more resources. If task wording and phase
conflict, resolve the mismatch before submission, without expanding scope or
changing every task to `full`.

## Submit once

Example arguments (replace the illustrative IDs and task text):

```json
{
  "task_id": "TASK-EXAMPLE",
  "work_unit_id": "prepare-1",
  "worker_task": "Implement the requested change in the declared workplace. Only edit the approved files. Run local tests and return evidence. Do not launch research, deploy, delete data or change Harness.",
  "worker_phase": "prepare_code",
  "access_mode": "writer",
  "timeout_seconds": 1800,
  "wait_ms": 1000,
  "max_preview_bytes": 2048
}
```

Validate the actual tool arguments before submitting. In the checked runtime,
`wait_ms` must be an integer in 0..30000; verify the loaded schema/reference
when the runtime changes. Waiting time is separate from the task's execution
timeout. Do not use an oversized wait to request a longer-running job.
Until the core validation order is fixed, invalid `wait_ms` can be rejected
after launch: an error or `outcome: unknown` is not evidence that no job ran.

Do not pass `command`, `command_id` or `cancel` with `worker_task`. No
`terminal_open`, additional SSH, YAML spec or per-study registry is needed.
`prepare_code` limits the worker prompt to implementation and lightweight checks.
Use `execute_run` for an authorized prepared execution, or `full` only when both
implementation and execution are in scope. These phases do not grant permission.

## Wait, recover and collect

Save the returned `command_id` immediately. On `WAITING_EXTERNAL` with suppressed
continuation, end the turn. Resume on the supported event or explicit manual
resume; do not poll. Query the SAME `terminal_exec` with just `task_id` and
`command_id` (and bounded preview/wait); cancellation uses those IDs plus
`cancel=true`. Never resend task text as a way to resume a model session.

If transport closes or submission returns no receipt/command ID, preserve the
original task/work-unit IDs, submitted material/version and known request facts.
Mark submission as uncertain, not worker failure or confirmed absence. Once the
declared connection is available, inspect the original request, job/claim and
receipt through existing scoped recovery. Use the positively identified
`command_id` to wait/recover/collect; do not resend the task, replace its work
unit, delete its claim or launch Codex over SSH to bypass uncertainty. An output
directory alone does not establish submission identity. Use `recover_worker`
only when supported by the loaded schema and its documented lookup semantics.

Connection/channel diagnosis belongs to the Local Manager; Worker can provide
existing evidence but is not required to repair the dispatch infrastructure.
If observation remains unavailable, retain the unresolved original submission
and report the blocker. Follow the actual event/manual-resume capability rather
than promising automatic reconnection, polling or wakeups.

`not_found`, `receipt_corrupt` and `job_scope_conflict` are errors, not waiting or
completion, and do not authorize resubmission. Preserve the Command ID and inspect
the exact evidence through scoped maintenance. This patch does not migrate legacy
`main/commands` records or provide task retirement; do not hand-edit a capsule to
simulate those operations. A missing modern record does not prove no legacy job ran.

The terminal supervisor owns process status, timeout/cancellation and log receipts.
The worker is a child process on the remote host, not another SSH connection.
The adapter also claims `(task_id, work_unit_id)` once. Repeating identical text
after that claim retrieves its original command ID; changed text is rejected.
An incomplete claim is an uncertain launch: inspect its evidence, do not delete
the claim or allocate a new work unit to retry blindly. After confirmed terminal
completion, a genuinely new authorized follow-up uses a new work_unit_id under
the same task. That is a new Codex invocation, not guaranteed model-session resume.

At completion, `artifacts` includes terminal stdout/stderr and, when present,
`worker_receipt`, `worker_result`, `worker_notes`. Use `artifact_inspect` to read
those narrowly. This path collects through `terminal_exec`, not `stage_collect`.
The existing worker prompt requires result.yaml and notes/logs; the adapter checks
task identity, output existence and hashes. Exit code zero alone is insufficient.
Read process status, `codex_exit_code`, `contract_valid`, `task_status` and
task acceptance separately. A valid `partial` may be prepared work awaiting
execution, another incomplete deliverable or a resource blocker; check the
actual phase, phase_state and outputs before classifying it. The wrapper may
exit nonzero and the supervisor may show `failed` despite Codex exit 0 and a
valid partial contract. Preserve those raw outcomes and explain the task-level
reason; do not call it an OS execution error or overall success by label alone.
OS/process failure, an invalid contract and a failed acceptance check require
different follow-up. `partial` is not final task completion. A cancelled/failed
worker without a result receipt remains unverified; terminal receipts still
describe the process outcome.
The local manager must inspect changed files, tests and scientific evidence before
accepting a success claim. Worker hashes are integrity evidence, not scientific
proof. Do not rerun completed S0 or start FSFM merely to test connectivity.

## Maintenance is separate

Keep declared SSH/SCP maintenance paths for deployment, diagnosis and repair.
They must not silently dispatch/repeat a worker when the formal path fails. The existing profile lifecycle and orchestrator SSH/SCP entrances share the same startup budget as the managed launcher. Explicit maintenance authorization does not bypass cooldown. Arbitrary shell commands are outside this library guard; do not use them to evade a refusal.
Do not edit the Harness core registry for every research script. Stable reviewed
execution specs may still use stage_execute → wait → stage_collect separately.
