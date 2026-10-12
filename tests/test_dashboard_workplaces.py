from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from solution_agent import web


def _record(task_id: str, root: str, *, status: str = "running") -> dict:
    return {
        "task_id": task_id,
        "title": f"{task_id} title",
        "status": status,
        "started_at": "2026-05-05T12:00:00+08:00",
        "remote": {
            "host": "EXAMPLE_WORKER_B",
            "remote_workdir": "/srv/example-project",
            "remote_root": root,
            "remote_task_dir": f"{root}/{task_id}__run",
            "run_id": f"{task_id}__run",
            "remote_execution_env": {
                "mode": "conda",
                "conda_env": "py_torch",
                "worker": "materialized_execution",
                "isolation": {
                    "per_run_directory": True,
                    "per_run_tmp": True,
                    "per_run_cache": True,
                    "forbid_runtime_install": True,
                },
            },
        },
        "last_remote_reconcile": {
            "status": {
                "status": {
                    "stdout": (
                        "pid:\n123\n"
                        "exit_code:\n\n"
                        "run_state:\nrunning\n"
                        "heartbeat_age_seconds:\n5\n"
                        "running:\nyes\n"
                        "stderr_tail:\n"
                        "stdout_tail:\nok\n"
                    )
                }
            }
        },
    }


def _config() -> dict:
    return {
        "host": "EXAMPLE_WORKER_B",
        "remote_workdir": "/srv/example-project",
        "remote_root": "/remote/root/a",
        "allowed_gpu_ids": "0,1",
        "min_free_gpu_mem_gb": 10,
        "max_starts_per_cycle": 2,
        "max_running_total": 4,
        "max_running_gpu": 4,
        "max_running_cpu": 1,
        "max_tasks_per_gpu": 2,
        "interval_minutes": 5,
        "model": "local",
        "gpu_snapshot_cache_seconds": 60,
        "remote_watch_interval_seconds": 30,
        "remote_worker": "materialized_execution",
    }


def test_remote_run_rows_show_all_workplaces_by_default():
    registry = {
        "tasks": {
            "task_a": _record("task_a", "/remote/root/a"),
            "task_b": _record("task_b", "/remote/root/b"),
        }
    }
    with patch("solution_agent.web.load_registry", return_value=registry):
        rows = web._remote_run_rows(_config())

    assert {row["task_id"] for row in rows} == {"task_a", "task_b"}
    assert {row["remote_root"] for row in rows} == {"/remote/root/a", "/remote/root/b"}


def test_remote_run_rows_can_filter_to_active_workplace():
    registry = {
        "tasks": {
            "task_a": _record("task_a", "/remote/root/a"),
            "task_b": _record("task_b", "/remote/root/b"),
        }
    }
    with patch("solution_agent.web.load_registry", return_value=registry):
        rows = web._remote_run_rows(_config(), all_workplaces=False)

    assert [row["task_id"] for row in rows] == ["task_a"]


def test_remote_runs_respect_hidden_completed_and_attention_lists():
    registry = {
        "tasks": {
            "task_done": _record("task_done", "/remote/root/a", status="completed"),
            "task_failed": _record("task_failed", "/remote/root/a", status="failed"),
            "task_visible": _record("task_visible", "/remote/root/a", status="completed"),
        }
    }
    with patch("solution_agent.web.load_registry", return_value=registry), patch(
        "solution_agent.web._hidden_completed", return_value={"task_done"}
    ), patch("solution_agent.web._hidden_attention", return_value={"task_failed"}):
        rows = web._remote_run_rows(_config(), status_filter="all")
        html = web._render_remote_runs(_config(), {"remote_status": ["all"]})

    assert [row["task_id"] for row in rows] == ["task_visible"]
    assert "task_visible" in html
    assert "task_done" not in html
    assert "task_failed" not in html


def test_hidden_dashboard_lists_accept_utf8_bom(tmp_path):
    completed_path = tmp_path / "completed.json"
    attention_path = tmp_path / "attention.json"
    completed_path.write_text('\ufeff["task_done"]', encoding="utf-8")
    attention_path.write_text('\ufeff["task_failed"]', encoding="utf-8")

    with patch("solution_agent.web._hidden_completed_path", return_value=completed_path), patch(
        "solution_agent.web._hidden_attention_path", return_value=attention_path
    ):
        assert web._hidden_completed() == {"task_done"}
        assert web._hidden_attention() == {"task_failed"}


def test_workplaces_include_recorded_remote_roots_without_switching_workers_off():
    registry = {
        "tasks": {
            "task_a": _record("task_a", "/remote/root/a"),
            "task_b": _record("task_b", "/remote/root/b"),
        }
    }
    with patch("solution_agent.web.load_registry", return_value=registry):
        workplaces = web._remote_workplaces(_config())
        html = web._render_workplaces(_config())

    roots = {item["remote_root"] for item in workplaces}
    assert {"/remote/root/a", "/remote/root/b"}.issubset(roots)
    assert "Existing remote workers keep running" in html
    assert "/remote/root/a" in html
    assert "/remote/root/b" in html


def test_history_workplace_can_be_forgotten_from_ui_without_hiding_active_root():
    registry = {
        "tasks": {
            "task_a": _record("task_a", "/remote/root/a"),
            "task_b": _record("task_b", "/remote/root/b"),
        }
    }
    cfg = _config()
    cfg["hidden_history_workplaces"] = [web._workplace_config_key("EXAMPLE_WORKER_B", "/remote/root/b")]
    with patch("solution_agent.web.load_registry", return_value=registry):
        workplaces = web._remote_workplaces(cfg)
        html = web._render_workplaces(cfg)

    roots = {item["remote_root"] for item in workplaces}
    assert "/remote/root/a" in roots
    assert "/remote/root/b" not in roots
    assert "Show Hidden History (1)" in html
    assert "hide_history_workplace" not in html


def test_history_workplace_shows_forget_button_when_visible():
    registry = {
        "tasks": {
            "task_a": _record("task_a", "/remote/root/a"),
            "task_b": _record("task_b", "/remote/root/b"),
        }
    }
    with patch("solution_agent.web.load_registry", return_value=registry):
        html = web._render_workplaces(_config())

    assert "hide_history_workplace" in html
    assert "Forget" in html
    assert "Remote files, local task history, and results are not deleted" in html


def test_manage_page_groups_saved_history_hidden_and_danger_zone():
    registry = {
        "tasks": {
            "task_a": _record("task_a", "/remote/root/a"),
            "task_b": _record("task_b", "/remote/root/b"),
        }
    }
    cfg = _config()
    cfg["hidden_history_workplaces"] = [web._workplace_config_key("EXAMPLE_WORKER_B", "/remote/root/b")]
    with patch("solution_agent.web.load_registry", return_value=registry):
        html = web._render_manage(cfg).decode("utf-8")

    assert "<h2>Manage</h2>" in html
    assert "Saved Workplaces" in html
    assert "History Workplaces" in html
    assert "Danger Zone: Local Files" in html
    assert "show_history_workplace" in html
    assert 'id="history-bulk-form"' in html
    assert "Forget Selected" in html
    assert "Restore Selected" in html
    assert "Dry Run Local Cleanup" in html
    assert "local_cleanup_delete" in html
    assert "Delete Selected Local Files" in html
    assert "Second confirmation" in html


def test_bulk_history_workplace_visibility_updates_selected_keys():
    cfg = _config()
    key_a = web._workplace_config_key("EXAMPLE_WORKER_B", "/remote/root/a")
    key_b = web._workplace_config_key("EXAMPLE_WORKER_B", "/remote/root/b")

    result = web._bulk_history_workplaces(cfg, [key_b], "forget")
    assert result["selected_count"] == 1
    assert key_b in cfg["hidden_history_workplaces"]
    assert key_a not in cfg["hidden_history_workplaces"]

    result = web._bulk_history_workplaces(cfg, [key_b], "restore")
    assert result["changed_count"] == 1
    assert key_b not in cfg["hidden_history_workplaces"]


def test_action_flash_renders_as_modal_payload():
    flash_path = Path(".tmp_dashboard_flash_test.json")
    try:
        with patch("solution_agent.web._dashboard_flash_path", return_value=flash_path):
            web._write_dashboard_flash({"headline": "Saved in modal", "details": ["ok"], "reload": False})
            html = web._render_page("Test", "<p>body</p>", refresh=False).decode("utf-8")
    finally:
        if flash_path.exists():
            flash_path.unlink()

    assert "Saved in modal" in html
    assert "__ACTION_FLASH_JSON__" not in html
    assert "var initialActionFlash =" in html
    assert not flash_path.exists()


def test_dashboard_preserves_scroll_position_across_action_reload():
    html = web._render_page("Test", "<p>body</p>", refresh=False).decode("utf-8")

    assert "solution-agent-dashboard-scroll:" in html
    assert "saveScrollPosition();" in html
    assert "restoreScrollPosition" in html
    assert "history.scrollRestoration = 'manual'" in html


def test_attention_and_completed_panels_expose_bulk_hide_controls():
    registry = {
        "tasks": {
            "task_failed": _record("task_failed", "/remote/root/a", status="failed"),
            "task_done": _record("task_done", "/remote/root/a", status="completed"),
        }
    }
    with patch("solution_agent.web.load_registry", return_value=registry):
        attention_html = web._render_attention_panel(_config(), plan={"skipped": []})
        records_html = web._render_records()

    assert 'id="attention-bulk-form"' in attention_html
    assert "bulk_hide_attention" in attention_html
    assert "Hide Selected" in attention_html
    assert 'form="attention-bulk-form"' in attention_html
    assert 'id="completed-bulk-form"' in records_html
    assert "bulk_hide_completed" in records_html
    assert "Hide Selected Completed" in records_html


def test_remote_target_card_can_switch_between_own_agent_and_codex():
    cfg = _config()
    own_html = web._render_remote_target(cfg)
    assert "new launches use: <strong>Own agent</strong>" in own_html
    assert 'name="remote_worker"' in own_html
    assert 'value="materialized_execution" selected' in own_html
    assert 'value="codex"' in own_html
    assert "Use Codex" not in own_html
    assert "Use Own Agent" not in own_html

    cfg["remote_worker"] = "codex"
    codex_html = web._render_remote_target(cfg)
    assert "new launches use: <strong>Codex</strong>" in codex_html
    assert 'value="materialized_execution"' in codex_html
    assert 'value="codex" selected' in codex_html
    assert "Use Own Agent" not in codex_html
    assert "Use Codex" not in codex_html


def test_skipped_waiting_explains_target_workplace_mismatch():
    item = {
        "task_id": "task_targeted",
        "reason": "target_workplace_mismatch",
        "mismatches": [
            {
                "field": "remote_root",
                "expected": "/remote/root/b",
                "active": "/remote/root/a",
            }
        ],
    }

    assert web._human_skip_reason(item) == (
        "target workplace mismatch; remote_root: expected /remote/root/b, active /remote/root/a"
    )
    assert web._skip_attention_type(item)[0] == "blocked_workplace"

    html = web._render_skipped_waiting(_config(), plan={"skipped": [item]})
    assert "blocked_workplace" in html
    assert "/remote/root/b" in html
    assert "/remote/root/a" in html


def test_remote_run_cards_offer_hide_for_terminal_runs():
    registry = {
        "tasks": {
            "task_done": _record("task_done", "/remote/root/a", status="completed"),
            "task_failed": _record("task_failed", "/remote/root/a", status="failed"),
        }
    }
    with patch("solution_agent.web.load_registry", return_value=registry), patch(
        "solution_agent.web._hidden_completed", return_value=set()
    ), patch("solution_agent.web._hidden_attention", return_value=set()):
        html = web._render_remote_runs(_config(), {"remote_status": ["all"]})

    assert "bulk_hide_completed" in html
    assert "hide_attention_task" in html
    assert "Results are not deleted" in html


def test_settings_page_exposes_worker_select():
    cfg = _config()
    cfg["remote_worker"] = "codex"
    html = web._render_settings(cfg).decode("utf-8")

    assert 'name="remote_worker"' in html
    assert 'value="materialized_execution"' in html
    assert 'value="codex" selected' in html


def test_history_workplace_ignores_per_run_remote_workdir_values():
    registry = {
        "tasks": {
            "task_a": {
                **_record("task_a", "/remote/root/b"),
                "remote": {
                    **_record("task_a", "/remote/root/b")["remote"],
                    "remote_workdir": "/remote/root/b/task_a__run",
                    "remote_task_dir": "/remote/root/b/task_a__run",
                    "remote_execution_env": {
                        "remote_workdir": "/remote/root/b/task_a__run",
                        "worker": "materialized_execution",
                    },
                },
            }
        }
    }
    with patch("solution_agent.web.load_registry", return_value=registry):
        workplaces = web._remote_workplaces(_config())

    history = [item for item in workplaces if item.get("from_registry")]
    assert history
    assert history[0]["remote_workdir"] == "/srv/example-project"


def test_local_cleanup_plan_and_delete_are_workspace_scoped(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    temp_dir = workspace / ".tmp"
    logs_dir = workspace / "logs"
    temp_dir.mkdir()
    logs_dir.mkdir()
    (temp_dir / "scratch.txt").write_text("scratch", encoding="utf-8")
    (logs_dir / "dashboard.log").write_text("log", encoding="utf-8")
    registry = {"tasks": {}}

    with patch("solution_agent.web.root", return_value=workspace):
        with patch("solution_agent.web.load_registry", return_value=registry):
            plan = web._local_cleanup_plan(["temp"])
            deleted = web._delete_local_cleanup_categories(["temp"])

    assert plan["candidate_count"] == 1
    assert plan["candidates"][0]["path"].endswith(".tmp")
    assert deleted["deleted_count"] == 1
    assert not temp_dir.exists()
    assert logs_dir.exists()


def test_local_cleanup_can_remove_pytest_cache_residue(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache_dir = workspace / ".pytest_cache"
    state_cache = workspace / "state" / "pytest-cache-files-old"
    cache_dir.mkdir()
    state_cache.mkdir(parents=True)
    (cache_dir / "nodeids").write_text("cache", encoding="utf-8")
    (state_cache / "tmp.txt").write_text("cache", encoding="utf-8")

    with patch("solution_agent.web.root", return_value=workspace):
        with patch("solution_agent.web.load_registry", return_value={"tasks": {}}):
            with patch("solution_agent.web.state_dir", return_value=workspace / "state"):
                plan = web._local_cleanup_plan(["test_cache"])
                deleted = web._delete_local_cleanup_categories(["test_cache"])

    paths = {Path(str(item["path"])).name for item in plan["candidates"]}
    assert ".pytest_cache" in paths
    assert "pytest-cache-files-old" in paths
    assert deleted["deleted_count"] == 2
    assert not cache_dir.exists()
    assert not state_cache.exists()


def test_local_cleanup_requires_at_least_one_category(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with patch("solution_agent.web.root", return_value=workspace):
        plan = web._local_cleanup_plan([])
        deleted = web._delete_local_cleanup_categories([])

    assert plan["ok"] is False
    assert plan["error"] == "no_cleanup_categories_selected"
    assert deleted["ok"] is False
    assert deleted["deleted_count"] == 0


def test_local_cleanup_skips_active_worktrees_and_dispatch(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "worktrees" / "active_task").mkdir(parents=True)
    (workspace / "worktrees" / "done_task").mkdir(parents=True)
    (workspace / "dispatch" / "active_task").mkdir(parents=True)
    (workspace / "dispatch" / "done_task").mkdir(parents=True)
    registry = {
        "tasks": {
            "active_task": {"task_id": "active_task", "status": "running"},
            "done_task": {"task_id": "done_task", "status": "completed"},
        }
    }

    with patch("solution_agent.web.root", return_value=workspace):
        with patch("solution_agent.web.load_registry", return_value=registry):
            plan = web._local_cleanup_plan(["worktrees", "dispatch"])

    paths = {Path(str(item["path"])).name for item in plan["candidates"]}
    assert "done_task" in paths
    assert "active_task" not in paths


def test_local_cleanup_plan_truncates_large_candidate_list(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dispatch = workspace / "dispatch"
    dispatch.mkdir()
    for index in range(120):
        item = dispatch / f"done_task_{index:03d}"
        item.mkdir()
        (item / "payload.txt").write_text("x", encoding="utf-8")

    with patch("solution_agent.web.root", return_value=workspace):
        with patch("solution_agent.web.load_registry", return_value={"tasks": {}}):
            plan = web._local_cleanup_plan(["dispatch"])

    assert plan["candidate_count"] == 120
    assert len(plan["candidates"]) == 80
    assert plan["truncated"] is True


def test_local_cleanup_remote_context_cache_keeps_newest(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = workspace / "state"
    state.mkdir()
    for index in range(55):
        path = state / f"remote_context_{index:03d}.tar.gz"
        path.write_text(str(index), encoding="utf-8")
        os.utime(path, (1000 + index, 1000 + index))

    with patch("solution_agent.web.root", return_value=workspace):
        with patch("solution_agent.web.state_dir", return_value=state):
            with patch("solution_agent.web.load_registry", return_value={"tasks": {}}):
                plan = web._local_cleanup_plan(["remote_context_cache"])
                deleted = web._delete_local_cleanup_categories(["remote_context_cache"])

    planned_names = {Path(str(item["path"])).name for item in plan["candidates"]}
    assert plan["candidate_count"] == 5
    assert "remote_context_000.tar.gz" in planned_names
    assert "remote_context_004.tar.gz" in planned_names
    assert deleted["deleted_count"] == 5
    assert not (state / "remote_context_000.tar.gz").exists()
    assert (state / "remote_context_054.tar.gz").exists()
