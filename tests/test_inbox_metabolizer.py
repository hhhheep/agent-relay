from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

from solution_agent.orchestrator import core
from tests.helpers import WorkspaceTempDir


def _patch_root(monkeypatch, tmp: Path) -> Path:
    workspace = tmp / "workspace"
    inbox = workspace / "tasks" / "inbox"
    inbox.mkdir(parents=True)
    monkeypatch.setattr(core, "root", lambda: workspace)
    return inbox


def _write_index(folder: Path) -> None:
    (folder / "TASK_BATCH_INDEX.md").write_text("# Batch\n", encoding="utf-8")


def _write_simple_task(folder: Path, name: str, *, extra: str = "") -> Path:
    path = folder / f"{name}.md"
    path.write_text(
        f"""---
task_id: {name}
title: {name}
worker: materialized_execution
intent: metrics_production
needs_gpu: true
outputs:
  - metrics.csv
{extra}---

# Task

Run metrics.
""",
        encoding="utf-8",
    )
    return path


def test_inbox_metabolize_dry_run_infers_safe_fields(monkeypatch):
    with WorkspaceTempDir() as tmp:
        inbox = _patch_root(monkeypatch, tmp)
        batch = inbox / "batch_a"
        batch.mkdir()
        _write_index(batch)
        _write_simple_task(batch, "a_001_prepare")

        result = core.inbox_metabolize(batch, write=False)

        assert result["ok"] is True
        updates = result["planned"][0]["updates"]
        assert updates["priority"] == "normal"
        assert updates["status"] == "pending"
        assert updates["line"] == "batch_a"
        assert updates["dag_root"] == "a_001_prepare"
        assert updates["resource_hint"] == "gpu"
        assert updates["resource_lane"] == "gpu_run"
        assert updates["allowed_workers"] == ["materialized_execution"]
        assert updates["allow_codex_fallback"] is False


def test_inbox_metabolize_infers_a_residual_decision_contract(monkeypatch):
    with WorkspaceTempDir() as tmp:
        inbox = _patch_root(monkeypatch, tmp)
        batch = inbox / "A_residual_correction_inbox_batch"
        batch.mkdir()
        _write_index(batch)
        _write_simple_task(
            batch,
            "a_rescorr_014_line_a_decision_synthesis",
            extra="""line: A_residual_correction
intent: decision_synthesis
outputs:
  - decision_summary.json
  - A_residual_correction_report.md
""",
        )

        result = core.inbox_metabolize(batch, write=False)

        contract = result["planned"][0]["updates"]["decision_contract"]
        assert contract["primary_json"] == "decision_summary.json"
        assert "decision_summary.json" in contract["required_files"]
        assert "A_residual_correction_report.md" in contract["required_files"]
        assert "best_iou_gain" in contract["required_fields"]
        assert "recommended_use" in contract["required_fields"]


def test_inbox_metabolize_write_updates_folder_front_matter(monkeypatch):
    with WorkspaceTempDir() as tmp:
        inbox = _patch_root(monkeypatch, tmp)
        batch = inbox / "batch_b"
        batch.mkdir()
        _write_index(batch)
        task_path = _write_simple_task(batch, "b_001_gpu")

        result = core.inbox_metabolize(batch, write=True)

        assert result["written"][0]["task_id"] == "b_001_gpu"
        raw = task_path.read_text(encoding="utf-8")
        meta = yaml.safe_load(raw.split("---", 2)[1])
        assert meta["resource_lane"] == "gpu_run"
        assert meta["dag_root"] == "b_001_gpu"
        assert meta["allow_codex_fallback"] is False


def test_inbox_metabolize_does_not_write_zip_containers(monkeypatch):
    with WorkspaceTempDir() as tmp:
        inbox = _patch_root(monkeypatch, tmp)
        zip_path = inbox / "batch_c.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("TASK_BATCH_INDEX.md", "# Batch\n")
            zf.writestr(
                "c_001.md",
                """---
task_id: c_001
title: c_001
worker: materialized_execution
intent: metrics_production
needs_gpu: false
---

# Task
""",
            )

        result = core.inbox_metabolize(zip_path, write=True)

        assert result["planned"][0]["write_supported"] is False
        assert result["written"] == []
        assert result["skipped"][0]["reason"] == "write_not_supported_for_container"


def test_inbox_lint_reports_hard_forbidden_fields(monkeypatch):
    with WorkspaceTempDir() as tmp:
        inbox = _patch_root(monkeypatch, tmp)
        batch = inbox / "batch_d"
        batch.mkdir()
        _write_index(batch)
        _write_simple_task(batch, "d_001", extra="materialized_execution_bypass_materializer: true\n")

        result = core.inbox_lint(batch)

        assert result["ok"] is False
        assert any("materialized_execution_bypass_materializer" in item["message"] for item in result["issues"])
