#!/usr/bin/env python3
"""Materialize a profile-bound plugin while preserving its declared launcher."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution_agent.harness.release_identity import (
    CANONICAL_TOOL_NAMES,
    build_runtime_identity,
    canonical_json_sha256,
)


def _object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _payload_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name != "release_manifest.json"):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def build(source: Path, installed: Path, output: Path, *, profile_id: str | None = None, control_root: Path | None = None) -> dict:
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    source_mcp = _object(source / ".mcp.json")
    installed_mcp = _object(installed / ".mcp.json")
    source_servers = source_mcp.get("mcpServers")
    installed_servers = installed_mcp.get("mcpServers")
    if not isinstance(source_servers, dict) or len(source_servers) != 1:
        raise ValueError("source plugin must declare exactly one MCP server")
    if not isinstance(installed_servers, dict) or len(installed_servers) != 1:
        raise ValueError("installed profile plugin must declare exactly one MCP server")
    source_name, source_server = next(iter(source_servers.items()))
    installed_name, installed_server = next(iter(installed_servers.items()))
    if source_name != installed_name or not isinstance(source_server, dict) or not isinstance(installed_server, dict):
        raise ValueError("source and installed MCP server identities differ")
    if not profile_id and "profile_id" not in installed_server and "profile" not in installed_server:
        raise ValueError("installed profile plugin must declare a profile_id")
    tools = source_server.get("tools")
    if not isinstance(tools, dict) or tuple(tools) != CANONICAL_TOOL_NAMES:
        raise ValueError("source plugin tools do not match the canonical ordered tool set")

    shutil.copytree(source, output, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    if (source / 'scripts/remote_launcher.py').is_file():
        shutil.copyfile(source.parent.parent / 'solution_agent/harness/ssh_admission.py',
                        output / 'scripts/connection_admission.py')
    materialized_server = dict(source_server)
    # Launcher identity is release-owned. Only the declared profile selector crosses
    # the build boundary; legacy command/args/env are deliberately ignored.
    materialized_server["profile_id"] = installed_server.get("profile_id", installed_server.get("profile"))
    for launcher_key in ("command", "cwd", "args", "env", "env_vars", "startup_timeout_sec"):
        if launcher_key not in source_server:
            materialized_server.pop(launcher_key, None)
    if control_root is not None:
        selector = profile_id or materialized_server["profile_id"]
        launcher = output.resolve() / "scripts/remote_launcher.py"
        if not launcher.is_file():
            raise ValueError("source plugin has no release-owned remote launcher")
        materialized_server.pop("profile_id", None)
        materialized_server.pop("env_vars", None)
        materialized_server.update(command=sys.executable, cwd=str(control_root.resolve()),
                                   args=[str(launcher), "--control-root", str(control_root.resolve()), "--profile", selector],
                                   startup_timeout_sec=90)
    (output / ".mcp.json").write_text(
        json.dumps({"mcpServers": {source_name: materialized_server}}, indent=2) + "\n",
        encoding="utf-8",
    )
    tool_contract = {
        "server": source_name,
        "tools": materialized_server["tools"],
        "tool_timeout_sec": materialized_server.get("tool_timeout_sec"),
    }
    schema_hash = canonical_json_sha256(tool_contract)
    payload_hash = _payload_hash(output)
    source_root = source.parent.parent
    source_identity = None
    try:
        source_identity = build_runtime_identity(source_root)
        release_id = source_identity.release_id
        source_root_sha256 = source_identity.source_root_sha256
    except (OSError, ValueError, json.JSONDecodeError):
        if control_root is not None:
            raise
        release_id = f"harness-plugin-{payload_hash[:16]}"
        source_root_sha256 = None
    identity_fields = asdict(source_identity) if source_identity is not None else {}
    if source_identity is not None:
        identity_fields["capabilities"] = list(source_identity.capabilities)
        schema_hash = source_identity.mcp_schema_sha256
    release_manifest = {
        **identity_fields,
        "protocol_version": "0.6",
        "schema_version": 1,
        "release_id": release_id,
        "source_root_sha256": source_root_sha256,
        "plugin_payload_sha256": payload_hash,
        "mcp_schema_sha256": schema_hash,
        "tool_count": len(CANONICAL_TOOL_NAMES),
        "ordered_tool_names": list(CANONICAL_TOOL_NAMES),
        "deprecated_aliases": [],
        "launcher_preserved": True,
        "launcher_source": "release-owned",
    }
    (output / "release_manifest.json").write_text(
        json.dumps(release_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "source": str(source),
        "installed": str(installed),
        "output": str(output),
        "server": source_name,
        "launcher_preserved": True,
        **release_manifest,
    }


def refresh_release_manifest(plugin: Path, source_root: Path) -> dict:
    """Refresh identity after the required Codex cachebuster changes plugin bytes."""

    plugin = plugin.resolve()
    identity = build_runtime_identity(source_root.resolve())
    tool_contract = _object(plugin / ".mcp.json")["mcpServers"]
    name, server = next(iter(tool_contract.items()))
    schema_hash = canonical_json_sha256(
        {
            "server": name,
            "tools": server["tools"],
            "tool_timeout_sec": server.get("tool_timeout_sec"),
        }
    )
    manifest_path = plugin / "release_manifest.json"
    payload = _object(manifest_path)
    schema_hash = identity.mcp_schema_sha256
    identity_fields = asdict(identity)
    identity_fields["capabilities"] = list(identity.capabilities)
    payload.update(identity_fields)
    payload.update(plugin_payload_sha256=_payload_hash(plugin), mcp_schema_sha256=schema_hash)
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, manifest_path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--installed", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--refresh", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--control-root", type=Path)
    args = parser.parse_args()
    if args.refresh:
        if args.source_root is None:
            parser.error("--refresh requires --source-root")
        result = refresh_release_manifest(args.refresh, args.source_root)
    else:
        if args.source is None or args.installed is None or args.output is None:
            parser.error("build mode requires --source, --installed, and --output")
        result = build(args.source.resolve(), args.installed.resolve(), args.output.resolve(),
                       profile_id=args.profile, control_root=args.control_root)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
