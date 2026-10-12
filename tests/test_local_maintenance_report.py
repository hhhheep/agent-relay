from __future__ import annotations

import json

from click.testing import CliRunner

from solution_agent import cli
from solution_agent.orchestrator import core


def test_local_maintenance_report_classifies_inbox_and_cache(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    inbox_batch = workspace / "tasks" / "inbox" / "batch"
    inbox_batch.mkdir(parents=True)
    (inbox_batch / "TASK_BATCH_INDEX.md").write_text("# Batch\n", encoding="utf-8")
    (inbox_batch / "task_a.md").write_text(
        """---
task_id: task_a
title: Task A
priority: normal
worker: materialized_execution
status: pending
created_at: 2026-05-07T12:00:00+08:00
---

# Task
""",
        encoding="utf-8",
    )
    (workspace / "tasks" / "inbox" / "loose.md").write_text("# Loose\n", encoding="utf-8")
    (workspace / "tasks" / "inbox" / "README.md").write_text("# Inbox docs\n", encoding="utf-8")
    (workspace / ".pytest_cache").mkdir()
    (workspace / "state" / "pytest-cache-files-old").mkdir(parents=True)
    remote_context = workspace / "state" / "remote_context_abc"
    remote_context.mkdir(parents=True)
    (remote_context / "manifest.json").write_text("{}", encoding="utf-8")
    remote_auth = workspace / "state" / "remote_auth"
    remote_auth.mkdir(parents=True)
    (remote_auth / "host__abc.token").write_text("secret-token\n", encoding="utf-8")
    (remote_auth / "host__abc.remote_materializer.env").write_text("API_KEY=secret\n", encoding="utf-8")

    monkeypatch.setattr(core, "root", lambda: workspace)
    monkeypatch.setattr(core, "state_dir", lambda: workspace / "state")
    monkeypatch.setattr(core, "remote_execution_profiles_path", lambda: workspace / "config" / "remote_execution_profiles.yaml")
    monkeypatch.setattr(core, "registry_hygiene", lambda apply_safe=False: {"issue_count": 0, "fixable_count": 0})

    report = core.local_maintenance_report()

    assert report["ok"] is True
    assert report["inbox"]["line_count"] == 1
    assert report["inbox"]["anomaly_count"] == 1
    assert any(item["code"] == "inbox_anomalies_present" for item in report["warnings"])
    assert report["local_residue"]["cache_candidate_count"] == 2
    assert report["local_residue"]["remote_context_cache_count"] == 1
    assert report["local_residue"]["remote_auth_token_count"] == 1
    assert report["local_residue"]["remote_auth_staged_env_count"] == 1
    assert any(item["code"] == "remote_auth_staged_env_residue" for item in report["warnings"])
    assert any(item["code"] == "remote_auth_tokens" for item in report["observations"])
    assert any(item["code"] == "remote_execution_profiles_missing" for item in report["observations"])


def test_local_maintenance_report_flags_bad_remote_profiles(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "tasks" / "inbox").mkdir(parents=True)
    profile_path = workspace / "config" / "remote_execution_profiles.yaml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("profiles:\n  bad: []\n", encoding="utf-8")

    monkeypatch.setattr(core, "root", lambda: workspace)
    monkeypatch.setattr(core, "state_dir", lambda: workspace / "state")
    monkeypatch.setattr(core, "remote_execution_profiles_path", lambda: profile_path)
    monkeypatch.setattr(core, "registry_hygiene", lambda apply_safe=False: {"issue_count": 0, "fixable_count": 0})

    report = core.local_maintenance_report()

    assert report["ok"] is False
    assert any(item["code"] == "invalid_remote_execution_profiles" for item in report["issues"])


def test_local_maintenance_report_cli(monkeypatch):
    monkeypatch.setattr(core, "local_maintenance_report", lambda: {"schema_version": 1, "ok": True})

    result = CliRunner().invoke(cli.cli, ["local-maintenance-report"])

    assert result.exit_code == 0
    assert json.loads(result.output)["ok"] is True
