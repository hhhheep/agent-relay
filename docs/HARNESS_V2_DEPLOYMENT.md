# Harness V2 Deployment Handoff

## Current package

The integration checkout is the source of truth for this deployment candidate.
The original `C:\Users\example-user\Desktop\ai-tools\solution-agent` checkout has not
been modified because it contains unrelated uncommitted work.

The bundle builder records a baseline SHA-256 and overlay SHA-256 per file. The
installer refuses a changed target, writes each verified candidate atomically,
records one hash-only forward-deployment receipt, and never deletes a target
file. It does not create a source backup or rollback tree; recovery replays a
reviewed, hash-verified release forward.

## Build and inspect locally

From the integration checkout:

```powershell
python scripts/harness/build_deployment_bundle.py `
  --overlay-root . `
  --baseline-root C:\Users\example-user\Desktop\ai-tools\solution-agent `
  --file-list deploy/overlay-files.txt `
  --output dist/solution-agent-harness-v2.zip

python scripts/harness/install_overlay.py `
  --bundle dist/solution-agent-harness-v2.zip `
  --target-root C:\Users\example-user\Desktop\ai-tools\solution-agent
```

The second command is a dry run. Any conflict must be reviewed and merged; do
not use a force flag. Apply only after the dry run is conflict-free:

```powershell
python scripts/harness/install_overlay.py `
  --bundle dist/solution-agent-harness-v2.zip `
  --target-root C:\Users\example-user\Desktop\ai-tools\solution-agent `
  --apply
```

## Runtime setup

Create a checkout-local virtual environment and install the narrow MCP runtime:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-harness.txt
$env:SOLUTION_AGENT_ROOT = (Get-Location).Path
python plugins/solution-agent-harness/scripts/server.py --check
```

Use the `plugin-creator` workflow in a new Codex task to register the plugin in
the personal marketplace. Do not edit marketplace JSON or Codex config by hand.
After installation, start another new task and confirm that the live tool list
contains exactly:

- `stage_execute`
- `stage_status`
- `stage_collect`

First run the no-API smoke stage. Only then run a report-writer stage with a
small validated evidence packet.

## Remote Example 91 rollout

The approved user-scoped control checkout is:

```text
/home/agent-user/.local/opt/solution-agent
```

The research workspace remains `/srv/example-workspaces/fan-workspace`, and the
recommended lifecycle storage is `/srv/example-workspaces/harness-runs`. Do not
use either one as the overlay target. The first installation requires a complete
audited release archive and SHA-256; the overlay zip alone cannot initialize an
empty checkout.

Use the Example remote solution-agent lifecycle, not an ad-hoc long SSH session:

1. Run workplace readiness and confirm the exact remote workdir/run root.
2. Transfer the bundle as a declared artifact and verify its SHA-256 remotely.
3. Run `install_overlay.py` without `--apply` against the remote checkout.
4. Stop on any conflict. Review the exact file instead of overwriting it.
5. Apply, create the remote checkout-local venv, and run `server.py --check`.
6. Register the plugin on the Codex host that actually runs the remote task.
7. Open a new remote Codex task and run the no-API seven-tool smoke test.
8. Run one real report-writer stage. Pass `DASHSCOPE_API_KEY` only through the
   environment/environment file and use the Singapore endpoint when configured.
9. Put the draft in a new `_worker_out`/artifact directory, not in
   `research/records` or `.research`.
10. Confirm report, claim sidecar, publication packet, provider usage, receipt,
    and artifact hashes. Then submit the draft through the existing ResearchFlow
    GET/SHA/PUT/409 path.

Do not copy an API key into the bundle, task Markdown, execution spec, command
arguments, logs, or handoff. The bundle intentionally contains no environment
file.

## Acceptance gates

- Three-tool MCP smoke succeeds end to end.
- A required Token Guard receipt is verified or collection fails closed.
- Required context cannot be omitted to satisfy a budget.
- Repeated identical validation failure stops further model retries.
- The report writer produces one canonical report and a claims sidecar.
- The writer does not alter the existing local/ResearchFlow destination; its
  publication packet names the draft SHA and expected destination SHA.
- A second `stage_collect` rejects an artifact changed after the first collect.
- Savings are claimed only after at least three paired, evidence-verified,
  outcome-equivalent, cache-balanced observations.
- Existing baseline failures are reported separately from harness regressions.
