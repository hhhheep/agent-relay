from __future__ import annotations

from click.testing import CliRunner

from solution_agent import cli
from solution_agent.contracts import CONTRACT_SCHEMA_VERSION, REMOTE_RUNTIME_VERSION, V1_BASELINE_VERSION
from solution_agent.orchestrator import core


def test_v1_stability_report_ok_with_stopped_daemons(monkeypatch):
    registry = {
        "tasks": {
            "done_1": {"status": "completed", "worker": "materialized_execution"},
            "remote_done": {
                "status": "completed",
                "worker": "materialized_execution",
                "remote": {"remote_task_dir": "/runs/remote_done"},
            },
        }
    }
    monkeypatch.setattr(core, "ensure_orchestrator_dirs", lambda: None)
    monkeypatch.setattr(core, "load_registry", lambda: registry)
    monkeypatch.setattr(core, "queue", lambda: [{"task_id": "ready_1", "ready": True}, {"task_id": "blocked_1", "ready": False}])
    monkeypatch.setattr(core, "scheduler_status", lambda: {"running": False, "lock_path": "scheduler.lock", "lock": None})
    monkeypatch.setattr(core, "remote_watch_status", lambda: {"running": False, "healthy": False, "state": {}})
    monkeypatch.setattr(core, "registry_hygiene", lambda apply_safe=False: {"issue_count": 0, "fixable_count": 0})

    report = core.v1_stability_report()

    assert report["ok"] is True
    assert report["status"] == "ok"
    assert report["baseline_version"] == V1_BASELINE_VERSION
    assert report["contract_schema_version"] == CONTRACT_SCHEMA_VERSION
    assert report["remote_runtime_version"] == REMOTE_RUNTIME_VERSION
    assert report["queue_summary"] == {"pending": 2, "ready": 1, "blocked": 1}
    assert report["remote_summary"]["total_records"] == 1
    assert {item["code"] for item in report["warnings"]} == {"scheduler_not_running", "remote_watch_not_running"}


def test_v1_stability_report_attention_for_hygiene_and_active_remote(monkeypatch):
    registry = {
        "tasks": {
            "active_1": {
                "status": "running",
                "worker": "materialized_execution",
                "remote": {"remote_task_dir": "/runs/active_1"},
                "remote_health": "unknown",
            }
        }
    }
    monkeypatch.setattr(core, "ensure_orchestrator_dirs", lambda: None)
    monkeypatch.setattr(core, "load_registry", lambda: registry)
    monkeypatch.setattr(core, "queue", lambda: [])
    monkeypatch.setattr(core, "scheduler_status", lambda: {"running": True, "healthy": False, "stale": True, "heartbeat_age_seconds": 999})
    monkeypatch.setattr(core, "remote_watch_status", lambda: {"running": True, "healthy": True, "stale": False})
    monkeypatch.setattr(core, "registry_hygiene", lambda apply_safe=False: {"issue_count": 2, "fixable_count": 1})

    report = core.v1_stability_report()

    assert report["ok"] is False
    assert report["status"] == "attention"
    codes = {item["code"] for item in report["issues"]}
    assert {"registry_hygiene_issues", "scheduler_unhealthy", "active_remote_health_unknown"} <= codes
    assert report["remote_summary"]["active_records"] == 1


def test_v1_stability_report_cli(monkeypatch):
    monkeypatch.setattr(core, "v1_stability_report", lambda: {"schema_version": 1, "ok": True})

    result = CliRunner().invoke(cli.cli, ["v1-stability-report"])

    assert result.exit_code == 0
    assert '"ok": true' in result.output


def test_scheduler_start_blocks_when_v1_stability_has_issues(monkeypatch):
    monkeypatch.setattr(core, "scheduler_status", lambda: {"running": False})
    monkeypatch.setattr(core, "scheduler_startup_guard", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(
        core,
        "v1_stability_report",
        lambda: {"ok": False, "issues": [{"code": "active_remote_health_unknown"}]},
    )

    result = core.scheduler_start(
        host="host",
        remote_root="/tmp/runs",
        remote_workdir="/tmp/work",
        preflight=False,
    )

    assert result["started"] is False
    assert result["reason"] == "v1 stability check failed"
    assert result["v1_stability_report"]["issues"][0]["code"] == "active_remote_health_unknown"


def test_scheduler_start_cli_exposes_stability_override(monkeypatch):
    captured = {}

    def fake_scheduler_start(**kwargs):
        captured.update(kwargs)
        return {"started": False, "reason": "dry test"}

    monkeypatch.setattr(core, "scheduler_start", fake_scheduler_start)

    result = CliRunner().invoke(
        cli.cli,
        [
            "scheduler-start",
            "--host",
            "host",
            "--remote-root",
            "/tmp/runs",
            "--remote-workdir",
            "/tmp/work",
            "--skip-preflight",
            "--skip-v1-stability-check",
        ],
    )

    assert result.exit_code == 0
    assert captured["preflight"] is False
    assert captured["v1_stability_check"] is False
