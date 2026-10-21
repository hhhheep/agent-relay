from __future__ import annotations

import json
import builtins
import hashlib
import shutil
from pathlib import Path

import pytest

from scripts.harness.build_profile_plugin import build, refresh_release_manifest
from scripts.harness.verify_integrated_runtime import _identity
from solution_agent.harness.release_identity import (
    CANONICAL_TOOL_NAMES,
    build_runtime_identity,
    compare_identities,
    source_tree_sha256,
)


def test_same_checkout_materializer_rejects_missing_runtime_dependency(tmp_path: Path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    source = root / 'plugins/solution-agent-harness'
    output = tmp_path / 'must-not-materialize'
    real_import = builtins.__import__

    def missing_runtime(name, *args, **kwargs):
        if name == 'solution_agent.harness.mcp_server':
            raise ModuleNotFoundError('synthetic missing MCP dependency', name='mcp')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', missing_runtime)
    with pytest.raises(ValueError, match='runtime dependencies required'):
        build(source, source, output, profile_id='fixture')
    assert not output.exists()


def _write_plugin(root: Path, server: dict) -> None:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin" / "plugin.json").write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"solution_agent_harness": server}}), encoding="utf-8")


def test_persistent_materializer_preserves_approval_policy_and_never_bundles_tokens(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = root / 'plugins' / 'solution-agent-harness'
    installed = tmp_path / 'installed'
    _write_plugin(installed, {'profile_id': 'fixture'})
    frontend, node, client = (tmp_path / name for name in ('frontend.mjs', 'node.exe', 'client.local.json'))
    frontend.write_text('// connection-only fixture', encoding='utf-8')
    node.write_text('fixture', encoding='utf-8')
    token = 'c' * 64
    config = {'host': '127.0.0.1', 'port': 12345, 'clientToken': token,
              'configurationSha256': 'a' * 64, 'runtimeSha256': 'b' * 64,
              'frontendSha256': hashlib.sha256(frontend.read_bytes()).hexdigest(),
              'nodeSha256': hashlib.sha256(node.read_bytes()).hexdigest()}
    client.write_text(json.dumps(config), encoding='utf-8')
    output = tmp_path / 'persistent-plugin'
    result = build(source, installed, output, persistent_frontend=frontend,
                   persistent_client_config=client, node_executable=node,
                   install_version='0.6.0+persistent.test')
    generated = json.loads((output / '.mcp.json').read_text())['mcpServers']['solution_agent_harness']
    original = json.loads((source / '.mcp.json').read_text())['mcpServers']['solution_agent_harness']
    assert generated['tools'] == original['tools']
    packaged = output.resolve() / 'scripts' / 'persistent_frontend.mjs'
    assert generated['args'] == [str(packaged), str(client.resolve())]
    assert packaged.read_bytes() == frontend.read_bytes()
    guard = 'plugins/solution-agent-harness/scripts/workplace_owner.py'
    assert (output / 'scripts/workplace_owner.py').read_bytes() == (root / guard).read_bytes()
    assert guard in source_tree_sha256(root)[1]
    assert result['transport_mode'] == 'manual-persistent/v1'
    assert json.loads((output / '.codex-plugin/plugin.json').read_text())['version'] == '0.6.0+persistent.test'
    assert (output / 'skills/solution-agent-harness/SKILL.md').read_bytes() == (root / 'scripts/harness/persistent-skill.md').read_bytes()
    assert result['release_id'] == build_runtime_identity(root).release_id
    assert not any(token in p.read_text(encoding='utf-8') for p in output.rglob('*') if p.is_file())
    client.write_text(json.dumps({'host': '127.0.0.1', 'port': 12345, 'clientToken': token, 'adminToken': 'd'*64}))
    with pytest.raises(ValueError, match='admin'):
        build(source, installed, tmp_path / 'must-not-exist', persistent_frontend=frontend,
              persistent_client_config=client, node_executable=node)
    assert not (tmp_path / 'must-not-exist').exists()
    client.write_text(json.dumps({**config, 'frontendSha256': '0' * 64}))
    with pytest.raises(ValueError, match='frontendSha256'):
        build(source, installed, tmp_path / 'wrong-bundle', persistent_frontend=frontend,
              persistent_client_config=client, node_executable=node)
    assert not (tmp_path / 'wrong-bundle').exists()


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

def test_selected_other_checkout_uses_its_actual_runtime_schema(tmp_path: Path) -> None:
    from solution_agent.harness.release_identity import release_files, mcp_tool_contract, canonical_json_sha256
    root = Path(__file__).resolve().parents[1]
    selected = tmp_path / 'selected-source'
    for path in release_files(root):
        target = selected / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # Bootstrap packages include runtime modules beyond the identity allowlist.
    # A selected checkout must likewise contain its actual import dependencies.
    shutil.copytree(root / 'solution_agent', selected / 'solution_agent', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    expected = build_runtime_identity(root)
    assert canonical_json_sha256(mcp_tool_contract(selected)) != expected.mcp_schema_sha256
    source = selected / 'plugins/solution-agent-harness'
    result = build(source, source, tmp_path / 'materialized', profile_id='fixture')
    assert result['release_id'] == expected.release_id
    assert result['source_root_sha256'] == expected.source_root_sha256
    assert result['mcp_schema_sha256'] == expected.mcp_schema_sha256
