"""Release-owned stdio transport; one independent SSH process per MCP client."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

import yaml


def route(control: Path, profile_id: str, plugin: Path) -> tuple[str, str]:
    selector = profile_id.casefold()
    if not re.fullmatch(r"[a-z0-9_-]+", selector):
        raise ValueError("invalid profile selector")
    policy = yaml.safe_load((control / "remote-workplaces/connection-policy.yaml").read_text(encoding="utf-8"))
    declared = policy["profiles"][selector]
    profile_path = (control / declared["profile"]).resolve()
    profile_path.relative_to((control / "remote-workplaces").resolve())
    profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    if profile["id"] != selector or profile["ssh_alias"] != declared["ssh_alias"] or declared["lifecycle_ready"] is not True:
        raise RuntimeError("profile identity does not match connection policy")
    if policy["policy"]["stage_transport"] != "solution_agent_harness_only":
        raise RuntimeError("unsupported stage transport policy")
    transport = profile["transport"]
    if transport["mode"] != "single_process_stdio_ssh" or transport["automatic_reconnect"] is not False:
        raise RuntimeError("unsupported transport mode or reconnect policy")
    identity = (Path(profile["local_workdir"]) / ".solution-agent/workplace.id").read_bytes()
    if not re.fullmatch(rb"solution-agent-workplace-v1:[0-9a-f]{32}\n", identity):
        raise RuntimeError("invalid local workplace.id")
    manifest = json.loads((plugin / "release_manifest.json").read_text(encoding="utf-8"))
    expected = {key: manifest[key] for key in ("protocol_version", "release_id", "source_root_sha256", "mcp_schema_sha256")}
    if any(not value for value in expected.values()):
        raise RuntimeError("plugin runtime identity is incomplete")
    root = profile["solution_agent_root"]
    command = ["env", f"SOLUTION_AGENT_ROOT={root}", f"PYTHONPATH={root}",
               f"PATH={root}/.venv/bin:/usr/local/bin:/usr/bin:/bin",
               f"SOLUTION_AGENT_PROFILE_ID={profile_id}", "SOLUTION_AGENT_PROTOCOL_REQUIRED=1",
               "SOLUTION_AGENT_EXPECTED_IDENTITY_B64=" + base64.b64encode(json.dumps(expected).encode()).decode(),
               "SOLUTION_AGENT_WORKPLACE_IDENTITY_B64=" + base64.b64encode(identity).decode(),
               f"{root}/.venv/bin/python", "-m", "solution_agent.harness.mcp_server"]
    return profile["ssh_alias"], shlex.join(command)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-root", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    alias, command = route(args.control_root.resolve(), args.profile, Path(__file__).resolve().parents[1])
    if args.check:
        print(json.dumps({"ok": True, "profile": args.profile.casefold(), "protocol": "0.6"}))
        return 0
    # Each App client owns only its SSH child and stdio streams. Legacy workplace
    # lock/cooldown files are deliberately neither read nor changed, so an older
    # live client cannot block a new chat. Task and terminal writer leases remain
    # enforced by the remote runtime.
    return subprocess.call(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, command],
        stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, ValueError, OSError, RuntimeError, yaml.YAMLError) as exc:
        print(f"Harness launcher: {exc}", file=sys.stderr)
        raise SystemExit(2)
