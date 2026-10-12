# Harness v2 Example 91 bootstrap installation

This procedure initializes the approved user-scoped core at:

```text
/home/agent-user/.local/opt/solution-agent
```

It does not modify `/srv/example-workspaces/fan-workspace`, does not read the API
key, and does not install into a historical run snapshot.

## Artifacts supplied by the maintainer

- `solution-agent-harness-bootstrap-<version>.zip`
- `solution-agent-harness-bootstrap-<version>.zip.manifest.json`
- `install_bootstrap_release.py`

Before transfer, use the SHA-256 printed by the builder and recorded in the
sidecar manifest. Do not copy a hash from an older release.

## Remote staging

Transfer the three files as declared deployment artifacts into a new directory
below `/srv/example-workspaces/harness-runs/deployments/`. Record local and remote
byte counts and SHA-256. Do not put them inside fan-workspace.

Run the installer without `--apply` first:

```bash
/usr/bin/python3 install_bootstrap_release.py \
  --archive solution-agent-harness-bootstrap-<version>.zip \
  --expected-sha256 '<SHA256_FROM_THE_CURRENT_MANIFEST>' \
  --target-root /home/agent-user/.local/opt/solution-agent
```

Acceptance requires `safe: true`, `conflicts: []`, target state `absent` or
`empty`, and the expected file count. Stop if the target is non-empty or the
hash differs. There is no force option.

Only after the dry-run receipt is reviewed, repeat with `--apply`. The installer
extracts into a temporary sibling directory, verifies every file, atomically
publishes the new root, and writes `bootstrap-install-receipt.json`.

## Runtime setup

```bash
cd /home/agent-user/.local/opt/solution-agent
/usr/bin/python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements-harness.txt
SOLUTION_AGENT_ROOT="$PWD" .venv/bin/python plugins/solution-agent-harness/scripts/server.py --check
.venv/bin/python scripts/harness/validate_remote_deploy_profile.py \
  --profiles config/harness_remote_deploy_profiles.yaml \
  --profile EXAMPLE_WORKER_A_FAN
```

Do not source the provider env file in the interactive shell. Plugin runtime
configuration may name
`/home/agent-user/.remote_materializer/.env` as `REPORT_WRITER_ENV_FILE`; only
the allowlisted report-writer child loads the permitted variables.

## Plugin and smoke gates

Use the plugin-creator update/install workflow on the same remote user/home that
runs Codex. Do not edit marketplace JSON or Codex configuration manually. Start
a new Codex task and require the live MCP surface to be exactly:

- `stage_execute`
- `stage_status`
- `stage_collect`

Run the no-API smoke before the provider smoke. The provider smoke uses one
small de-identified evidence packet and must return report, claims sidecar,
usage, publication packet, receipt, and artifact hashes. Do not rerun the costly
three-case A/B unless the deployment result disagrees with the existing
acceptance evidence.

## Stop conditions

Stop without cleanup or overwrite when any of these occurs:

- archive or file hash mismatch;
- non-empty target root;
- plugin surface differs from the seven fixed tools;
- checkout-local server check fails;
- provider response remains unusable after one corrective retry;
- a credential appears in a command, log, receipt, Markdown, or newly created
  deployment artifact.
