# Configure a new client and Worker pair

Use your own authorized SSH destination and private directories. These are synthetic configuration examples, not predeclared hosts or permission to start research work. Use the generated client ZIP paired to the Worker ZIP; its `plugins/solution-agent-harness/release_manifest.json` is required. Do not replace it with an authored or unrelated manifest.

## Worker configuration

Install the Worker as described in [INSTALL](INSTALL.md), then create a workplace and run directory for this selected environment. Copy `config/harness_remote_deploy_profiles.example.json` into the installed Worker's private `config/harness_remote_deploy_profiles.yaml` (JSON is valid YAML). Rename `EXAMPLE` to `example`, set `binding_id` to `example`, and replace `solution_agent_root`, `project_workdir`, `remote_run_root`, the terminal slot's `cwd`, and the remaining example paths with your own absolute Linux paths. Select a supported jobs backend and provide its host requirements; the tested Linux combination uses `systemd_transient`. Set `remote_terminals.storage_profile_id` to `example` if using an alias. This private fallback file is excluded from shared packages and runtime source identity.

The release-owned SSH launcher starts the server with the selected root and profile. If you instead choose an external `SOLUTION_AGENT_PROFILES_FILE`, explicitly provide that variable to the actual remote server entrypoint; exporting it only in a local shell does not transmit it through SSH. The private installed-file fallback above works with the delivered launcher without changing its source.

Create `<remote-workplace>/.solution-agent/workplace.id` with one new UUID, exactly these UTF-8 bytes (32 lowercase hex digits, followed by one LF):

```text
solution-agent-workplace-v1:<32-lowercase-hex-digits>
```

Create the corresponding local workplace identity with the same bytes. An explicit Python command, run at each selected workplace using the same UUID, avoids BOM/CRLF conversion:

```python
from pathlib import Path
import uuid
# Generate once; use this same uuid_hex on the other endpoint.
uuid_hex = uuid.uuid4().hex
identity = Path('.solution-agent/workplace.id')
identity.parent.mkdir(parents=True, exist_ok=True)
if identity.exists():
    raise RuntimeError('Existing workplace identity must be preserved')
identity.write_bytes(('solution-agent-workplace-v1:' + uuid_hex + '\n').encode('utf-8'))
print(uuid_hex)
```

Do not generate a second UUID independently or replace an existing workplace identity. For an existing workplace, use its established identity and preserve its configuration/state.

## Client control files

Create a private control directory containing `remote-workplaces/connection-policy.yaml`:

```yaml
policy:
  stage_transport: solution_agent_harness_only
profiles:
  example:
    profile: remote-workplaces/example.yaml
    ssh_alias: example-worker
    lifecycle_ready: true
```

Then create `remote-workplaces/example.yaml`:

```yaml
id: example
ssh_alias: example-worker
local_workdir: C:/example/local-workplace
solution_agent_root: /opt/example/agent-relay-worker
remote_workdir: /work/example
remote_root: /work/example/runs
transport:
  mode: single_process_stdio_ssh
  automatic_reconnect: false
  reconnect_policy: single_attempt_fail_closed
```

Replace the alias and all paths. The declared profile ID must match the private Worker profile, the SSH alias must match your own SSH configuration, and the local/remote workplace identities must agree. `lifecycle_ready` declares the selected profile for this transport; it is not proof that the remote endpoint has passed a check. No transport-owner lock is required.

In your own MCP configuration, declare the installed paired launcher:

```json
{
  "mcpServers": {
    "solution_agent_harness": {
      "command": "C:/example/agent-relay/.venv/Scripts/python.exe",
      "args": [
        "C:/example/agent-relay/plugins/solution-agent-harness/scripts/remote_launcher.py",
        "--control-root", "C:/example/private-control",
        "--profile", "example"
      ],
      "cwd": "C:/example/private-control"
    }
  }
}
```

Absolute paths allow launch from another working directory. Keep private control files, SSH credentials and state outside the shared source. Adding another Worker means adding a separate declared binding; preserve existing profile bytes and state.

## Check before use

First run the local configuration preflight:

```powershell
<client-python> <client-root>/plugins/solution-agent-harness/scripts/remote_launcher.py --control-root <private-control> --profile example --check
```

It validates local declarations, identity format and paired manifest; it does not contact the Worker. For an explicitly selected test workplace, use the delivered read-only cwd probe and original-ID recovery check:

```powershell
<client-python> <client-root>/scripts/release/check_harness_remote.py --python <client-python> --plugin <client-root>/plugins/solution-agent-harness --control-root <private-control> --profile example --task-id example-connection-check --expected-cwd <remote-workplace> --receipt <client-root>/.verification/example-connection.local.json
```

This submits one bounded synthetic terminal command, checks the eleven-tool surface and exact release/protocol/workplace pairing, then queries its original ID. A mismatch stops the connection before side effects; do not weaken identity checks to connect unmatched packages. If observation fails after submission, inspect/query the recorded original ID before trying anything else. Artifact testing is optional and must explicitly select a synthetic source and writable test target; the old-package namespace keeps code read-only and state writable.
