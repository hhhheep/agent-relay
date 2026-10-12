from __future__ import annotations

from pathlib import Path

from solution_agent.orchestrator import core
from solution_agent.orchestrator import memory


def test_workflow_profile_created_with_operating_order(tmp_path: Path, monkeypatch):
    path_map = {
        "project_context_path": tmp_path / "project_context.md",
        "last_summary_path": tmp_path / "last_summary.md",
        "memory_path": tmp_path / "memory.md",
        "task_history_path": tmp_path / "task_history.md",
        "file_index_path": tmp_path / "file_index.md",
        "codex_worker_profile_path": tmp_path / "codex_worker_profile.md",
        "workflow_profile_path": tmp_path / "workflow_profile.md",
    }
    for name, path in path_map.items():
        monkeypatch.setattr(memory, name, lambda path=path: path)

    memory.ensure_local_memory_files()

    text = path_map["workflow_profile_path"].read_text(encoding="utf-8")
    assert "Score modules first" in text or "score modules first" in text
    assert "Run narrow tests" in text
    assert "Missing cache, script, or capability is a partial result" in text
    assert "worker, line, batch, dag root, and prefix" in text.lower()


def test_remote_context_paths_include_workflow_profile():
    paths = memory.remote_context_paths("/remote/workdir")

    assert "/remote/workdir/.codex-orchestrator/workflow_profile.md" in paths


def test_remote_context_paths_include_policy_guides():
    paths = memory.remote_context_paths("/remote/workdir")

    for filename in (
        "scheduler_optimization_notes.md",
        "opencoder_task_execution_guide.md",
        "inbox_task_format.md",
    ):
        assert f"/remote/workdir/.codex-orchestrator/{filename}" in paths


def test_workspace_workflow_profile_retains_core_rules():
    memory.ensure_local_memory_files()
    text = memory.workflow_profile_path().read_text(encoding="utf-8")

    for phrase in (
        "Start from the local clone",
        "Preserve the task book's intent",
        "score modules first",
        "Run narrow tests",
        "Missing cache, script, or capability is a partial result",
        "Do not invent cache paths",
        "Treat worker, line, batch, dag root, and prefix as admission filters",
        "Do not launch unrelated experiment lines",
        "Stop for user judgment",
    ):
        assert phrase.lower() in text.lower()


def test_workflow_profile_repairs_stale_file_with_core_rules(tmp_path: Path, monkeypatch):
    path_map = {
        "project_context_path": tmp_path / "project_context.md",
        "last_summary_path": tmp_path / "last_summary.md",
        "memory_path": tmp_path / "memory.md",
        "task_history_path": tmp_path / "task_history.md",
        "file_index_path": tmp_path / "file_index.md",
        "codex_worker_profile_path": tmp_path / "codex_worker_profile.md",
        "workflow_profile_path": tmp_path / "workflow_profile.md",
    }
    for name, path in path_map.items():
        monkeypatch.setattr(memory, name, lambda path=path: path)
    path_map["workflow_profile_path"].write_text("# Workflow Profile\n\nOld notes only.\n", encoding="utf-8")

    memory.ensure_local_memory_files()

    text = path_map["workflow_profile_path"].read_text(encoding="utf-8")
    assert "Managed Core Rules" in text
    assert "score modules first" in text
    assert "Do not invent cache paths" in text
    assert "worker, line, batch, dag root, and prefix" in text.lower()


def test_remote_bootstrap_blocks_when_policy_guides_missing(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    for filename in (
        "last_summary.md",
        "memory.md",
        "task_history.md",
        "file_index.md",
        "codex_worker_profile.md",
        "workflow_profile.md",
    ):
        (state / filename).write_text("# context\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "MAINTENANCE_POLICY_V1.md").write_text("# maintenance\n", encoding="utf-8")

    monkeypatch.setattr(core, "root", lambda: tmp_path)
    monkeypatch.setattr(core, "state_dir", lambda: state)
    monkeypatch.setattr(core, "codex_worker_profile_path", lambda: state / "codex_worker_profile.md")
    monkeypatch.setattr(core, "workflow_profile_path", lambda: state / "workflow_profile.md")
    monkeypatch.setattr(core, "ensure_local_memory_files", lambda: None)

    def fail_copy(*args, **kwargs):
        raise AssertionError("remote copy should not run when context files are missing")

    monkeypatch.setattr(core, "scp_to_remote", fail_copy)
    monkeypatch.setattr(core, "run_ssh", fail_copy)

    result = core.remote_bootstrap_context("host", "/remote/workdir")

    assert result["blocked"] is True
    assert result["reason"] == "remote_context_required_files_missing"
    missing = {item["name"] for item in result["missing_context_files"]}
    assert {"scheduler_optimization_notes", "opencoder_task_execution_guide", "inbox_task_format"} <= missing
