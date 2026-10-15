# HF-014: distinguish lock faults from writer contention

Base: `44876c4`; branch: `codex/hf014-lock-errors-20261015`.
Classification: ordinary fix in the Harness terminal owner, `solution_agent/harness/remote_terminal.py`.

HF-014-C001–C003 identified that all OS errors were reported as another client's lease. Synthetic EIO, EBADF, ENOLCK and EINVAL reproduced this defect: four tests failed before repair. These failures do not establish the cause of any historical deployed incident.

The existing terminal error envelope now reports `terminal_lock_failed`, numeric errno and unknown owner for non-contention failures. Actual nonblocking lock contention retains `terminal_busy` with errno. POSIX uses EAGAIN/EWOULDBLOCK; Windows also uses EACCES. Windows seeks directly to the byte range before locking, without reading another writer's locked byte; native tests verify that an empty lock file still excludes a second writer and permits attach after detach. Pre-lock file I/O errors cannot claim a competing owner.

No lock file is removed, owner is killed, task is closed or lease stolen. Namespace, tools, exact protocol identity gate, task/command/receipt/CAS and terminal state format are unchanged. The MCP consumer already carries generic TerminalError codes through the existing envelope. No new transport, diagnostic registry or recovery framework is introduced. Rollback changes code only, retaining original state and IDs.

Validation in isolated fixtures on Windows:

```text
python -m pytest tests/test_terminal_lease_errors.py tests/test_remote_terminal.py tests/test_harness_mcp.py tests/test_harness_protocol_06.py -q
55 passed
```

The added fixtures cover both backend error classifiers, handle closure, no tmux side effects after lock failure, successful subsequent attach and a Windows pre-lock I/O failure. Native competing writers remain covered by the existing terminal test. An intermediate repair exposed Windows denying the pre-lock read; that repair was corrected without weakening the contention assertion.

NOT RUN: native Linux flock, local reboot/network interruption, PID reuse diagnostics and deployed recovery. HF-014 as a whole remains open: this commit fixes only its reproduced non-contention error classification. Full owner liveness and recovery acceptance requires separate evidence. No live runtime, installed plugin, research task or deployment was changed.

Publication/provenance and diff checks pass; historical source-map hashes are preserved. The historical design inventory mismatch on `.gitattributes` remains the documented unchanged-base limitation. A future package must regenerate release identity through the existing builder; this source fix is not a newly certified cross-platform release.
