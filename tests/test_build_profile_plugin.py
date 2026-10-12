from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.harness.build_profile_plugin import build, refresh_release_manifest
from scripts.harness.verify_integrated_runtime import _identity
from solution_agent.harness.release_identity import (
    CANONICAL_TOOL_NAMES,
    build_runtime_identity,
    compare_identities,
)


def _write_plugin(root: Path, server: dict) -> None:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin" / "plugin.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"solution_agent_harness": server}}), encoding="utf-8")


def test_materializer_uses_release_launcher_and_only_preserves_profile(tmp_path: Path) -> None:
    source = tmp_path / "source"
    installed = tmp_path / "installed"
    output = tmp_path / "output"
    _write_plugin(
        source,
        {
            "command": "python",
            "cwd": ".",
            "args": ["server.py"],
            "env_vars": ["SOLUTION_AGENT_ROOT"],
            "startup_timeout_sec": 20,
            "tools": {name: {} for name in CANONICAL_TOOL_NAMES},
        },
    )
    _write_plugin(
        installed,
        {
            "command": "example-worker-a.cmd",
            "profile_id": "EXAMPLE_WORKER_A_FAN",
            "cwd": "C:/control",
            "args": ["mcp"],
            "env_vars": ["PROGRAMDATA"],
            "startup_timeout_sec": 30,
            "tools": {"stage_execute": {}},
        },
    )

    result = build(source, installed, output)
    server = json.loads((output / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["solution_agent_harness"]

    assert result["tool_count"] == 11
    assert server["command"] == "python"
    assert server["cwd"] == "."
    assert server["args"] == ["server.py"]
    assert server["env_vars"] == ["SOLUTION_AGENT_ROOT"]
    assert server["startup_timeout_sec"] == 20
    assert server["profile_id"] == "EXAMPLE_WORKER_A_FAN"
    assert result["launcher_source"] == "release-owned"
    assert tuple(server["tools"]) == CANONICAL_TOOL_NAMES
    release = json.loads((output / "release_manifest.json").read_text(encoding="utf-8"))
    assert release["mcp_schema_sha256"] == result["mcp_schema_sha256"]
    assert release["deprecated_aliases"] == []

    old = json.loads((installed / ".mcp.json").read_text())
    del old["mcpServers"]["solution_agent_harness"]["profile_id"]
    (installed / ".mcp.json").write_text(json.dumps(old))
    with pytest.raises(ValueError, match="profile_id"):
        build(source, installed, tmp_path / "unbound-output")


def test_workspace_plugin_package_uses_the_source_runtime_release_id(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / "plugins" / "solution-agent-harness"
    installed = tmp_path / "installed"
    output = tmp_path / "output"
    _write_plugin(
        installed,
        {
            "command": "example-worker-a.cmd",
            "profile_id": "EXAMPLE_WORKER_A_FAN",
            "cwd": "C:/control",
            "args": ["mcp"],
            "tools": {name: {} for name in CANONICAL_TOOL_NAMES},
        },
    )
    result = build(source, installed, output)
    expected = build_runtime_identity(root)
    assert not list(output.rglob("*.pyc"))
    assert result["release_id"] == expected.release_id
    assert result["source_root_sha256"] == expected.source_root_sha256
    assert compare_identities(expected, _identity(output / "release_manifest.json")) == []
    manifest_path = output / ".codex-plugin" / "plugin.json"
    plugin_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plugin_manifest["version"] = "0.6.0+codex.test"
    manifest_path.write_text(json.dumps(plugin_manifest), encoding="utf-8")
    refreshed = refresh_release_manifest(output, root)
    assert refreshed["release_id"] == expected.release_id
    assert refreshed["plugin_payload_sha256"] != result["plugin_payload_sha256"]
    assert compare_identities(expected, _identity(output / "release_manifest.json")) == []
