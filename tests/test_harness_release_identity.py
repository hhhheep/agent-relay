from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from solution_agent.harness.release_identity import (
    CANONICAL_TOOL_NAMES,
    build_runtime_identity,
    canonical_json_sha256,
    compare_identities,
    build_source_inventory,
    mcp_tool_contract,
    source_tree_sha256,
    write_release_manifest,
)


def _minimal_root(root: Path) -> Path:
    files = {
        "solution_agent/harness/mcp_server.py": "VALUE = 1\n",
        "solution_agent/harness/remote_terminal.py": "TERMINAL = True\n",
        "solution_agent/context_gateway.py": "CONTEXT = True\n",
        "scripts/harness/build_profile_plugin.py": "BUILD = True\n",
        "plugins/solution-agent-harness/scripts/server.py": "SERVER = True\n",
        "plugins/solution-agent-harness/skills/solution-agent-harness/SKILL.md": "# Skill\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    plugin = root / "plugins/solution-agent-harness/.codex-plugin/plugin.json"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text(json.dumps({"name": "solution-agent-harness", "version": "1.0.0"}), encoding="utf-8")
    (root / "plugins/solution-agent-harness/.mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "solution_agent_harness": {
                        "tools": {name: {"approval_mode": "approve"} for name in CANONICAL_TOOL_NAMES}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    release_paths = [
        *files,
        "plugins/solution-agent-harness/.codex-plugin/plugin.json",
        "plugins/solution-agent-harness/.mcp.json",
        "deploy/overlay-files.txt",
    ]
    allowlist = root / "deploy/overlay-files.txt"
    allowlist.parent.mkdir(parents=True, exist_ok=True)
    allowlist.write_text("\n".join(release_paths) + "\n", encoding="utf-8")
    return root


def test_source_hash_is_deterministic_and_ignores_state(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    first, first_files = source_tree_sha256(root)
    state = root / "state/runtime.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"changed": true}\n', encoding="utf-8")
    second, second_files = source_tree_sha256(root)
    assert first == second
    assert first_files == second_files


def test_source_hash_changes_when_release_file_changes(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    before, _ = source_tree_sha256(root)
    (root / "solution_agent/harness/mcp_server.py").write_text("VALUE = 2\n", encoding="utf-8")
    after, _ = source_tree_sha256(root)
    assert before != after


def test_source_hash_ignores_unlisted_remote_extension(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    before, before_files = source_tree_sha256(root)
    extra = root / "solution_agent/harness/remote_only_extension.py"
    extra.write_text("REMOTE = True\n", encoding="utf-8")
    after, after_files = source_tree_sha256(root)
    assert after == before
    assert after_files == before_files


def test_runtime_identity_binds_plugin_version_and_tool_contract(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    identity = build_runtime_identity(root)
    assert identity.plugin_version == "1.0.0"
    assert identity.host_resume_mode == "manual"
    assert identity.mcp_schema_sha256 == canonical_json_sha256(
        {
            "server": "solution_agent_harness",
            "tools": {name: {"approval_mode": "approve"} for name in CANONICAL_TOOL_NAMES},
            "tool_timeout_sec": None,
        }
    )


def test_same_version_with_changed_schema_is_not_equal(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    expected = build_runtime_identity(root)
    observed = replace(expected, mcp_schema_sha256="0" * 64)
    assert compare_identities(expected, observed) == ["mcp_schema_sha256"]


def test_workspace_schema_hash_includes_live_tool_arguments() -> None:
    root = Path(__file__).resolve().parents[1]
    contract = mcp_tool_contract(root)
    terminal_open = contract["runtime_tools"]["terminal_open"]["input_schema"]["properties"]
    terminal_exec = contract["runtime_tools"]["terminal_exec"]["input_schema"]["properties"]
    assert tuple(contract["runtime_tools"]) == CANONICAL_TOOL_NAMES
    assert "rebind_from_profile_id" not in terminal_open
    assert "expected_release_id" not in terminal_open
    assert "task_id" in terminal_open
    assert "terminal_id" not in terminal_exec
    assert "work_unit_id" in terminal_exec
    assert set(terminal_exec["access_mode"]["enum"]) == {"read_only", "writer"}
    assert "timeout_seconds" in terminal_exec
    assert "cancel" in terminal_exec
    assert build_runtime_identity(root).mcp_schema_sha256 == canonical_json_sha256(contract)


def test_python_module_hash_mismatch_is_reported(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    expected = build_runtime_identity(root)
    observed = replace(
        expected,
        python_module_hashes={**expected.python_module_hashes, "extra.py": "1" * 64},
    )
    assert compare_identities(expected, observed) == ["python_module_hashes"]


def test_release_manifest_contains_auditable_source_and_skill_identity(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    manifest = write_release_manifest(root, tmp_path / "release_manifest.json")
    assert manifest["source_commit"] is None
    assert manifest["source_dirty"] is True
    assert len(manifest["skill_sha256"]) == 64
    assert manifest["created_at"].endswith("+00:00")
    assert set(manifest["python_module_hashes"]) == {
        "plugins/solution-agent-harness/scripts/server.py",
        "scripts/harness/build_profile_plugin.py",
        "solution_agent/context_gateway.py",
        "solution_agent/harness/mcp_server.py",
        "solution_agent/harness/remote_terminal.py",
    }


def test_source_inventory_records_selection_commit_tree_and_dirty(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    inventory = build_source_inventory(
        [root],
        selected_authoritative_root=root,
        selection_reason="reviewed test root",
    )
    selected = inventory["candidate_roots"][0]
    assert selected["commit"] is None
    assert selected["dirty"] is True
    assert len(selected["tree_sha256"]) == 64
    assert inventory["selected_authoritative_root"] == str(root.resolve())
    assert inventory["selection_reason"] == "reviewed test root"


def test_release_source_rejects_symlink(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    target = root / "outside.py"
    target.write_text("X = 1\n", encoding="utf-8")
    link = root / "solution_agent/harness/link.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    allowlist = root / "deploy/overlay-files.txt"
    allowlist.write_text(allowlist.read_text(encoding="utf-8") + "solution_agent/harness/link.py\n", encoding="utf-8")
    with pytest.raises(ValueError, match="symlinked"):
        source_tree_sha256(root)
