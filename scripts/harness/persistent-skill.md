---
name: solution-agent-harness
description: Use the manually started persistent Harness for declared remote workplaces, scoped tasks, supervised commands, artifacts and authorized maintenance.
---

# Solution Agent Harness — persistent connection

This installed variant is connection-only (`manual-persistent/v1`). The operator
manually starts one workplace owner, which holds one declared SSH transport.
The plugin connects to local IPC; creating a chat, reloading the plugin or rebuilding
a client must not start SSH or the owner. Do not run the old direct launcher, wrapper
`mcp`, a second host, SSH or SCP to recover an ordinary failed tool call. A disconnected
owner supports explicit operator `reconnect` without restarting its process or
tasks. The operator may enable bounded automatic reconnect; failed recovery latches
until manual action, with at least 60 seconds between attempts and an automatic
budget of three attempts per hour. Cooldown survives client rebuilds. Never loop stop/start,
toggle recovery to bypass a latch, or schedule age-based restarts. SSH/SCP remain available for explicitly authorized
installation, diagnosis and repair through the selected declared profile.

Read the selected profile from the existing control plane. Profile labels are routing
context, never extra permission. Preserve canonical workplace bytes and exact runtime,
source, protocol and schema pairing. Do not guess host paths, forge a caller or copy
an identity from a different workplace.

Multiple chats/tasks can share the transport. Every call authenticates the actual App
context and its existing task/controller scope. The owner only reads registered
authority; it cannot create grants, transfer research control or register a task from
tool arguments. New scope registration belongs to the existing trusted local publisher.
For an explicitly authorized new task/work unit, the operator can use the retained
host CLI `register <private-host.json> <task> <work-unit> <declared-source> --apply`.
It verifies the genuine App caller, reuses the existing publisher registry and refuses
to replace another caller's binding. It starts no backend and dispatches no work.
An unregistered or stale scope is a control-plane problem, not a reason to reconnect.
Explicit administrative rebind requires the committed original transfer chain and a
generation/epoch check. Never change another scope to make a call pass.

The host CLI provides `reconnect <private-host.json>`, `auto-reconnect
<private-host.json> on|off`, and `status <private-host.json>`. The panel also shows
connection state, cooldown and recovery controls while offline. Connectivity
maintenance requires operator authorization; it grants no new task authority and
never resends a command. A healthy transport refuses replacement. If the old
process tree cannot be confirmed empty, recovery refuses a new connection.

Prefer the operator button panel for human registration/handoff. Launch the retained
host CLI `panel <private-host.json>` from the genuine RECEIVING chat, then open its
returned local browser URL for the user. The panel lists registered work and fixes
the receiver to that actual chat; task/chat IDs, epoch and grants are never typed by
the agent. Only the human may press registration/takeover confirmation buttons.
Do not call the panel HTTP mutation endpoint, click its confirmation, manufacture
grants, or infer takeover permission from copied context. For a new chat that does
not yet exist, first prepare original artifact/task references and create that chat
only when the user requests it; old ownership remains until the receiving chat is
ready and the user authorizes takeover. Splitting work uses a separate new scope;
it does not transfer the parent's authority. Existing manual CLI operations are
advanced maintenance entry points, not a license for models to choose identity IDs.

For moving existing work, preserve the original task/work-unit and command/job/
artifact references. A copied chat, fork or message does not grant authority. The
trusted operator must commit the exact approved controller transfer from the genuine
receiving App chat and declared workspace, then scope-select `rebind` using the
retained host CLI. Drain pending calls first; idle frontends need not be killed.
Old-caller authority is rechecked on subsequent calls. If transfer committed but
rebind failed, inspect its original receipt and retry the same transfer/rebind;
never reset the epoch or publisher origin. Returning control requires a new approved
reverse transfer at the current epoch, not restoration of an old authority database.
Owner restart recovery of jobs requires the original hash-pinned canonical receipt
on the correct scope. Unknown job IDs must not be self-authorized from chat text.
Reconstructing a client or registering a new task does not restart the owner/SSH.

Use the existing eleven Harness tools and their approval rules:

- For a supervised short command, use `terminal_exec` with the existing `task_id`.
  Writes require its authorized `work_unit_id`, writer access and a bounded timeout.
  Read-only commands use `access_mode=read_only`. No interactive terminal is needed.
- For remote Codex work only when the user authorized it, read
  [remote-codex.md](references/remote-codex.md) and use `worker_task` Markdown on
  `terminal_exec`. A connection check never dispatches a worker.
- Use `stage_execute`, `stage_status`, `stage_collect` for reviewed bounded stages.
  Preserve the original capsule/work-unit/runtime binding and stage IDs. A status or
  collection is not scientific approval. Unknown recovery references require the
  original canonical receipt; do not relabel its execution release.
- Use `terminal_open` only for interactive work in the authorized task. Send/close
  operations must remain attached to that scope; never take another chat's terminal.
  Child terminals reuse the same SSH. `terminal_read` is a bounded diagnostic view.
- Use `artifact_store` once for reusable content, `artifact_inspect` for selective
  reads/diffs and `artifact_apply` plan/commit with the original expected target hash.
  Keep Harness Artifact Clipboard/CAS provenance; OS clipboard is not its substitute.

If a command result is uncertain, query its original command ID. Never repeat a submit
or create another task/work unit to bypass a lost response. Existing receipt, lease,
ACK, artifact and supervisor semantics remain authoritative.

For a paired owner configured with a private submission journal, a plain command's
intent and original command ID are saved by the program before dispatch. If the
reply was lost before any command ID was observed, use terminal_exec with the
original task and recover_submission=true, without command, command_id, cancel,
finish_maintenance or worker_task. The owner derives scope from current authority
and only looks up saved originals; it never replays an unknown submission. Follow
its returned submission_cursor for bounded journal pages. Never invent or supply
submission_id: it belongs to program state. Queued, missing or unknown is not
completion or permission to resubmit. An unjournaled legacy request cannot gain a
new recovery identity retrospectively. Journal recovery does not grant a scope or
replace an approved controller transfer.

The optional journal contains exact original plain-command inputs in a private
operator-protected directory. Recovery listings expose metadata and backend
receipts, never those saved inputs. Keep the directory out of artifacts and logs.
Legacy hash-only entries remain readable without inventing missing inputs. The
journal rejects new submissions at its record/byte limit; do not delete uncertain
entries or automatically replay them to clear capacity.

When a stage returns `WAITING_EXTERNAL` and `suppress_model_continuation=true`, end
the turn. The deterministic monitor owns waiting; do not create polling Goals or
model heartbeat loops. Respect manual-resume limitations.

The local operator retains `status`, guarded `stop` and scope-selected `rebind` through
the retained host CLI. The plugin carries only its client credential and cannot start,
stop or administratively rebind the owner. Keep private credentials/configuration out
of messages, artifacts and logs. Never kill an unknown process or clear an ownership
lock to force a connection.

For maintenance use the selected declared profile and read
[remote-and-reporting.md](references/remote-and-reporting.md). For an existing managed
job review read [proactive-manager.md](references/proactive-manager.md); that does not
authorize new dispatch. Read other references only when their workflow is requested.
