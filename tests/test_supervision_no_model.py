from __future__ import annotations

import shutil
import uuid

from solution_agent.orchestrator import core
from solution_agent.orchestrator.paths import registry_path, task_dir
from solution_agent.orchestrator.registry import load_registry, save_registry
from solution_agent.orchestrator.supervisor import SupervisionDecision
from solution_agent.orchestrator.task_loader import render_new_task


def _running_task() -> tuple[str, dict]:
    task_id = f"no_model_supervision_{uuid.uuid4().hex[:8]}"
    backup = load_registry()
    running = task_dir("running", task_id)
    running.mkdir(parents=True, exist_ok=True)
    (running / "task.md").write_text(render_new_task(task_id, "No-model supervision"), encoding="utf-8")
    save_registry(
        {
            "tasks": {
                task_id: {
                    "task_id": task_id,
                    "title": "No-model supervision",
                    "status": "running",
                    "remote": {"host": "host", "remote_root": "/remote/root"},
                }
            }
        },
        path=registry_path(),
    )
    return task_id, backup


def _cleanup(task_id: str, backup: dict) -> None:
    save_registry(backup, path=registry_path())
    for status in ("pending", "running", "completed", "partial", "failed"):
        path = task_dir(status, task_id)
        if path.exists():
            shutil.rmtree(path)


def _status(stdout: str) -> dict:
    return {
        "status": {"returncode": 0, "stdout": stdout, "stderr": ""},
        "cache": {"hit": False},
    }


def test_normal_wait_never_calls_supervision_model(monkeypatch) -> None:
    task_id, backup = _running_task()
    calls = []
    monkeypatch.setattr(core, "supervise_with_llm", lambda *args, **kwargs: calls.append(True))
    try:
        payload = core.supervise_remote(
            task_id,
            host="host",
            remote_root="/remote/root",
            status_result=_status("running:\nyes\nheartbeat_age_seconds:\n10\n"),
        )
        assert calls == []
        assert payload["decision"]["action"] == "continue"
        assert payload["model_dispatch_count"] == 0
        assert payload["llm_advisory"]["source"] == "skipped:model_review_disabled"
    finally:
        _cleanup(task_id, backup)


def test_simulated_two_hour_wait_has_zero_model_dispatches(monkeypatch) -> None:
    task_id, backup = _running_task()
    calls = []
    monkeypatch.setattr(core, "supervise_with_llm", lambda *args, **kwargs: calls.append(True))
    status = _status("running:\nyes\nheartbeat_age_seconds:\n10\n")
    try:
        dispatches = 0
        for _ in range(120):
            payload = core.supervise_remote(
                task_id,
                host="host",
                remote_root="/remote/root",
                status_result=status,
            )
            dispatches += payload["model_dispatch_count"]
        assert dispatches == 0
        assert calls == []
    finally:
        _cleanup(task_id, backup)


def test_explicit_ambiguous_review_dispatches_once_per_fingerprint(monkeypatch) -> None:
    task_id, backup = _running_task()
    calls = []

    def fake_review(*args, **kwargs):
        calls.append(True)
        return SupervisionDecision(
            action="warn",
            confidence=0.8,
            reason_code="manual_review_required",
            summary="Review once.",
            evidence=[],
            concerns=["ambiguous state"],
            next_check_minutes=0,
            source="fake-model",
        )

    monkeypatch.setattr(core, "supervise_with_llm", fake_review)
    try:
        first = core.supervise_remote(
            task_id,
            host="host",
            remote_root="/remote/root",
            status_result=_status("running:\nno\n"),
            allow_model_review=True,
        )
        second = core.supervise_remote(
            task_id,
            host="host",
            remote_root="/remote/root",
            status_result=_status("running:\nno\n"),
            allow_model_review=True,
        )
        assert len(calls) == 1
        assert first["model_dispatch_count"] == 1
        assert second["model_dispatch_count"] == 0
        assert second["llm_advisory"]["source"] == "skipped:duplicate_supervision_fingerprint"
    finally:
        _cleanup(task_id, backup)
