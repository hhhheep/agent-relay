from __future__ import annotations

import json
from pathlib import Path

from solution_agent.tools.search_tools import probe_search_backends, safe_search


def test_python_search_fallback_respects_scope_and_excludes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "solution_agent").mkdir()
    (repo / "solution_agent" / "core.py").write_text("needle = 'action_request'\n", encoding="utf-8")
    (repo / "results").mkdir()
    (repo / "results" / "noise.txt").write_text("action_request should not be scanned\n", encoding="utf-8")
    (repo / "model.ckpt").write_text("action_request binary-like text\n", encoding="utf-8")

    result = safe_search(repo, "action_request", root=".", backend="python_fallback")

    assert result["ok"] is True
    assert result["backend"] == "python_fallback"
    assert result["results"] == [{"path": "solution_agent/core.py", "line": 1, "text": "needle = 'action_request'"}]


def test_solutionignore_is_applied(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".solutionignore").write_text("ignored/**\n", encoding="utf-8")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "note.txt").write_text("needle\n", encoding="utf-8")
    (repo / "kept.txt").write_text("needle\n", encoding="utf-8")

    result = safe_search(repo, "needle", root=".", backend="python_fallback")

    assert [item["path"] for item in result["results"]] == ["kept.txt"]


def test_probe_writes_selected_backend_state(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()

    result = probe_search_backends(repo, write_state=True)

    assert result["selected_search_backend"]
    probe_path = repo / "state" / "tool_probe.json"
    assert probe_path.exists()
    saved = json.loads(probe_path.read_text(encoding="utf-8"))
    assert saved["selected_search_backend"] == result["selected_search_backend"]
