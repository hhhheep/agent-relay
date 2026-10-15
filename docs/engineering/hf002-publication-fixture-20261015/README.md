# Self-contained publication integration fixture

Base: `99f6ccd`. Branch: `codex/hf002-publication-fixture-20261015`.

The publication integration test failed in both the clean worktree and unchanged `4c4438b`: it read a deployment-specific `worker-md-allowlist.json` absent from the repository. This prevented reproducible validation of the document handoff associated with HF-002. This is a test portability repair, not a repair of live document synchronization or closure of HF-002.

The fixture now creates an allowlist and three exact-byte synthetic Markdown files under its temporary source root. It still executes the real transfer receiver in a subprocess, accepts the version/hash receipt, installs the publication registry binding, reads the snapshot through the worker packet and rejects a tampered file. An additional assertion independently checks all source hashes. No runtime code, private configuration, policy or deployment is changed. The existing CI core selection includes this integration and its direct consumers.

Validation:

```text
python -m pytest tests/test_routing_submission_recovery.py tests/test_publication_registry.py tests/test_md_sync.py tests/test_harness_md_sync.py tests/test_harness_event_wait.py tests/test_harness_mcp.py -q -x
73 passed
```

An initial fixture hash assertion caught Windows newline translation; the fixture now writes explicit UTF-8 bytes. No assertion was removed or weakened. Publication and current provenance checks pass; historical source-map hashes remain unchanged. Native Linux and live App/worker publication are NOT RUN. Existing historical design-inventory baseline limitations remain unchanged. Rollback affects test inputs and CI coverage only; no state migration is involved.
