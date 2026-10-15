# HF-014: explain writer contention and preserve normal recovery

Base `cc05d65`; branch `codex/hf014-owner-diagnostics-20261015`. Ordinary terminal-owner diagnostic repair in `solution_agent/harness/remote_terminal.py`. Before repair, native two-client contention exposed no owner identity. The new regression failed before implementation.

After a successful attach, existing schema-1 session metadata additionally records an advisory lease-owner PID, process creation marker, host boot identity where available, connection ID, task ID and lock type. Windows queries process creation/exit information through read-only process handles; Linux reuses the existing boot/start-marker process observer. Incomplete legacy records report unknown. PID reuse and unreadable identities do not establish a live matching process.

Contention retains the existing `terminal_busy` error envelope/code. Its bounded message includes the recorded identity and liveness observation. This metadata is explicitly not proof of the kernel lock holder: a stale record or an exited PID never authorizes overriding an actual OS lock. No new public tool, argument, error-envelope key, lock registry or transport is added. Existing readers accept legacy session records and ignore the advisory field; rollback retains the original session/lock and does not migrate state. Read-only terminal retrieval remains available during writer contention.

Recovery is the existing kernel-lock path: the old MCP owner exits normally, then a new manager attaches the same terminal. A real child-process fixture verifies exclusion while the owner runs, reacquisition after normal exit, unchanged lock inode and no terminal recreation. No lock is deleted, process killed, valid writer displaced or research command resubmitted. The fixture reads the actual child's reported PID because Windows venv launchers can have a distinct parent PID.

Validation:

```text
python -m pytest tests/test_terminal_owner_diagnostics.py tests/test_terminal_lease_errors.py tests/test_remote_terminal.py tests/test_harness_mcp.py tests/test_terminal_commands.py tests/test_harness_release_identity.py tests/test_harness_protocol_06.py -q -x
105 passed, 2 existing native-POSIX skips
```

Coverage includes legacy/malformed metadata, PID-start mismatch, simulated reboot/absent/unreadable evidence, preserved native exclusion and generic error-envelope serialization. Current publication/provenance and diff checks pass; historical source-map hashes remain intact. Native Linux, actual machine reboot/network failure and deployed acceptance are NOT RUN. This source repair closes the reproduced missing diagnostic and normal-exit recovery subcases, not every historical HF-014 acceptance scenario. No deployment or live task/state was changed; future packages must use a newly generated exact-paired release identity.
