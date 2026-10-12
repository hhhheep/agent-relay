from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from solution_agent.remote_materializer.controller import _read_context_pack


def _write_required_pack(pack_dir: Path, *, max_tokens: int) -> None:
    pack_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(
        yaml.safe_dump({"budget": {"max_tokens": max_tokens}}),
        encoding="utf-8",
    )
    for name in (
        "task_brief.md",
        "execution_hints.yaml",
        "workspace_context_excerpt.yaml",
        "script_registry_excerpt.yaml",
        "work_order.json",
        "behavior_policy.md",
        "allowed_files.yaml",
        "expected_outputs.md",
        "artifact_summary.md",
    ):
        (pack_dir / name).write_text(f"{name}: ok\n", encoding="utf-8")


def test_model_context_loader_keeps_full_files_but_omits_low_priority_context(tmp_path: Path) -> None:
    pack_dir = tmp_path / "context_pack"
    _write_required_pack(pack_dir, max_tokens=160)
    duplicate_task = "duplicated task details " * 200
    (pack_dir / "task.md").write_text(duplicate_task, encoding="utf-8")
    (pack_dir / "repo_map_excerpt.md").write_text("repo symbol " * 200, encoding="utf-8")

    context = _read_context_pack(pack_dir)

    assert "context_pack/task_brief.md" in context
    assert "duplicated task details" not in context
    assert (pack_dir / "task.md").read_text(encoding="utf-8") == duplicate_task
    audit = json.loads((pack_dir / "model_context_selection.json").read_text(encoding="utf-8"))
    assert audit["estimated_tokens"] <= audit["max_estimated_tokens"]
    assert any(item["name"] == "repo_map_excerpt.md" for item in audit["omitted"])


def test_model_context_loader_fails_closed_when_required_context_cannot_fit(tmp_path: Path) -> None:
    pack_dir = tmp_path / "context_pack"
    _write_required_pack(pack_dir, max_tokens=20)
    (pack_dir / "artifact_summary.md").write_text("evidence " * 100, encoding="utf-8")

    with pytest.raises(ValueError, match="required context exceeds budget"):
        _read_context_pack(pack_dir)
