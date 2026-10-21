# Persistent Harness reconnect

Status: Windows client update activated as `0.6.0+persistent.20261021.1`.
The supported remote backend and MCP schema were retained unchanged. Private
loaded-runtime, original-ID and rollback receipts are kept outside publication.

The observed failure is a network-environment change invalidating an existing
SSH connection while the local owner remains alive. Formal transport does not
require the test VPN. The current owner becomes offline and permits startup only
from idle, so recovery currently replaces that owner manually. A reconnect should
instead preserve the owner and replace only its declared backend transport.

## Operator entry points

Use the retained `persistent-harness-host-cli.mjs` with the private host JSON:

```text
status <private-host.json>
reconnect <private-host.json>
auto-reconnect <private-host.json> on
auto-reconnect <private-host.json> off
panel <private-host.json>
```

Automatic recovery defaults off. It waits for ten seconds of stable local
interface/address observations and an already failed transport, then attempts
once. It never disconnects a healthy transport just because an interface changes.
Attempts are at least sixty seconds apart; automatic recovery also refuses after
three connection attempts in the preceding hour (manual attempts count toward
that budget). A failed or interrupted automatic
attempt latches until a successful explicit manual reconnect. Switching the
option or restarting clients cannot clear that latch. This first implementation
observes interface/address changes, not every possible route-table mutation;
SSH keepalive/transport failure still determines when recovery is needed.

The sibling `<private-host.json>.reconnect.json` persists policy and attempt
reservation, with an atomic flushed replacement before connecting. Keep it in
the same private operator directory. Missing means default-off; invalid state
fails closed. It contains no task data or admin token. Status reports connectivity,
cooldown, latch and fixed reason codes. The local admin token never reaches MCP.

## Change boundary

Add an explicit operator `reconnect` action to the retained host CLI and existing
local control panel. The control derives workplace, endpoint, runtime pins and
identity from the sealed host configuration; no model-entered IDs or new task
registration are involved. The plugin remains connection-only and receives no
admin credential or connection-start capability. No remote MCP schema extension
is needed for this reconnect action.

The existing control window should show connection state, the last disconnect or
reconnect reason, an in-progress indicator and any cooldown remaining. Provide a
manual **Reconnect** button and a separate **Automatic reconnect on network
change** switch, initially off until explicitly activated. Disable duplicate
button presses while an operation is running. The interface must explain a
failure without asking a model to copy chat/task IDs or type SSH commands.
These controls affect workplace connectivity, not task ownership or research
continuation. Do not add them to the registration/takeover confirmation flow.

The existing authenticated local admin IPC may accept a reconnect request with
the expected owner generation, configuration hash and runtime hash. Require the
owner to be offline, zero pending requests and no concurrent start/attach/reconnect
transition. A healthy owner refuses the operation. Close and await the old owned
backend transport before opening a replacement; if its exit cannot be established,
refuse the new connection rather than overlap transports. Keep the workplace
singleton ownership lock throughout the transition.

The existing guardian proves that only the host and the exact verifier ancestor
chain remain in its OS process tree. A leftover SSH/launcher, unknown parent or
unreadable process membership blocks recovery. It does not kill unrelated
processes. Backend launch files and environment pins are checked again before
each reconnect. Verified cached schemas remain discoverable offline, while
actual calls and new attachments still require an online verified backend.

## Client-only update and rollback

Build the local client from reviewed source and retain the exact remote peer
tuple from the supported base plugin. Do not materialize the unrelated backend
submission-journal candidate for this update. Back up the active host/client
JSON, launcher bytes, plugin source/cache and current loaded hashes before
activation. Preserve all authority databases, workplace bytes and original IDs;
do not restore an old database during rollback.

Drain local pending requests, stop only the recorded owner, confirm its owned
SSH tree has exited, then start the newly pinned owner once. Update only matching
client pins, reload the selected plugin, and query existing original IDs with
explicit read-only access. The new owner is expected for this code update;
subsequent reconnects preserve that new owner's generation and frontend leases.
If activation fails, leave recovery latched and preserve logs. A rollback restores
the backed-up client/launcher/plugin bytes using current-hash checks and starts
the supported prior owner only after confirming no transport overlap and its
cooldown; it never dispatches or replays research work.

Create exactly one new transport from the existing declaration and verify its
tool catalog, peer protocol/release/source/schema and canonical workplace before
marking it online. A mismatch or connection failure returns to offline with a
compact reason, without retrying. Preserve owner generation, current controller
bindings/epochs and frontend attachment identities, with fresh authorization on
subsequent calls. Owned Native App helper processes are closed during the empty
tree check and bootstrapped from their original in-memory trusted context on the
next call. The cumulative `backendStarts` counter identifies transport attempts;
concurrent SSH count must remain at most one.

No prior command, worker packet, stage or artifact apply is replayed. An in-flight
request lost during the network transition remains uncertain and is recovered
only through its existing canonical identity or journal. A reconnect does not
finish a lease, cancel a command, accept research results or grant task authority.

## Network-change trigger

The optional trigger observes local interface/address changes without
opening probe SSH/TCP connections. A route change alone must not replace a healthy
transport; use existing transport failure/keepalive detection to establish offline.
Debounce interface churn and reuse the same serialized reconnect action.

An enabled trigger needs an explicit attempt budget: one attempt for a settled
network transition, a persisted cooldown, and a failure latch requiring operator
intervention. Further flapping must not reset the budget into unlimited attempts.
No age-based restart, model polling, worker restart or per-chat connection loop.
The operator's reconnect button remains available for explicit recovery.

## Required evidence before activation

- Network changes while internet remains available, followed by transport loss:
  one replacement, same owner and original work identities.
- Concurrent button/trigger/client calls: one reconnect operation and at most one
  live transport; healthy, stale-generation and non-admin calls cannot reconnect.
- Old transport that cannot exit, failed authentication, wrong peer/schema or
  wrong workplace: no overlapping or repeated connection and no execution replay.
- Lost command ACK and current controller changes: original result recovery and
  fresh authorization, without changing task IDs, epochs or scientific state.
- Desktop catalog discovery stays usable offline and existing frontend leases
  recover after reconnect. Authenticated SDK listings may reject while offline;
  execution remains gated. Reconnect does not require repeated plugin toggles.
- A bounded real direct-connection check with VPN off: both original chats query
  their canonical IDs through the same replacement SSH, with before/after receipts.

This update is separate from the larger submission-journal backend candidate.
The optional owner journal source requires a matching extended backend and is
disabled for the supported base peer. Publishing source does not activate it.
Installation and real direct recovery require their own loaded-runtime evidence;
isolated results alone do not establish production connectivity.

## Verification for this Windows update

- All 146 Lifecycle tests passed, including recovery, authority, native App and
  direct consumers. Guardian/materializer/attestation suites: 19 passed.
- Packaged CLI with a fake SSH boundary performed manual and automatic recovery
  under the same owner. One existing frontend recovered and four reconstructed
  frontends reused it. Three sequential transports, no overlapping SSH and no
  command submission; all test-owned processes stopped afterward.
- Formal activation used one replacement direct connection with the test VPN
  off. Two existing Desktop chats each queried one canonical original command
  through their native tools with explicit read-only access. Both retained the
  original execution release, successful state and shared the same owner/SSH.
- Original workplace/controller records and protected configuration hashes
  matched. Four matching local client declarations were migrated; no remote
  backend deployment, worker dispatch or research acceptance occurred.
- The actual browser panel displayed online, automatic recovery enabled and
  the manual reconnect button disabled while healthy. The operator CLI remains
  available independently of the temporary thirty-minute panel URL.

Automatic network-loss recovery was exercised using the isolated transport;
production networking was not deliberately disrupted again. This is a Windows
client update, not a new Linux backend or full submission-journal acceptance.
