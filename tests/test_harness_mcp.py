from __future__ import annotations

import ast
import asyncio
import json
import time
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from mcp import Client
import yaml
import pytest

import solution_agent.harness.mcp_server as server
from solution_agent.harness.mcp_server import mcp
from solution_agent.harness.usage_ledger import UsageLedger
from solution_agent.harness.task_capsule import ScopeContract, TaskCapsule


def test_containment_canonicalizes_declared_root_alias_without_accepting_foreign_path(tmp_path):
    target, alias = tmp_path / "allowed", tmp_path / "declared-alias"
    target.mkdir()
    allowed, foreign = target / "result.json", tmp_path / "foreign.json"
    allowed.write_text("{}", encoding="utf-8")
    foreign.write_text("{}", encoding="utf-8")
    if os.name == "nt":
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        assert powershell, "Windows junction acceptance requires native PowerShell"
        subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command",
            "New-Item -ItemType Junction -Path $env:HARNESS_FIXTURE_LINK -Target $env:HARNESS_FIXTURE_TARGET -ErrorAction Stop | Out-Null"],
            env={**os.environ, "HARNESS_FIXTURE_LINK": str(alias), "HARNESS_FIXTURE_TARGET": str(target)},
            check=True, capture_output=True)
    else:
        alias.symlink_to(target, target_is_directory=True)
    try:
        assert server._inside(alias / allowed.name, alias) == allowed.resolve()
        assert server._inside(allowed, alias) == allowed.resolve()
        with pytest.raises(ValueError, match="outside"):
            server._inside(foreign, alias)
    finally:
        if os.name == "nt":
            alias.rmdir()  # Remove only the junction entry, never its target.
        else:
            alias.unlink()


def test_mcp_server_source_parses_as_remote_python_310() -> None:
    source = Path(server.__file__).read_text(encoding="utf-8")
    ast.parse(source, filename=str(server.__file__), feature_version=(3, 10))


def test_mcp_uses_app_policy_for_consequential_tools() -> None:
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "plugins" / "solution-agent-harness" / ".mcp.json").read_text(
            encoding="utf-8"
        )
    )
    tools = config["mcpServers"]["solution_agent_harness"]["tools"]
    assert set(tools) == {
        "stage_execute",
        "stage_status",
        "stage_collect",
        "terminal_open",
        "terminal_exec",
        "terminal_send",
        "terminal_read",
        "terminal_close",
        "artifact_inspect",
        "artifact_store",
        "artifact_apply",
    }
    assert {name for name, tool in tools.items() if tool["approval_mode"] == "approve"} == {
        "stage_status",
        "terminal_read",
        "artifact_inspect",
    }
    assert {name for name, tool in tools.items() if tool["approval_mode"] == "prompt"} == {
        "stage_execute",
        "stage_collect",
        "terminal_open",
        "terminal_exec",
        "terminal_send",
        "terminal_close",
        "artifact_store",
        "artifact_apply",
    }


def test_mcp_exposes_canonical_integrated_tool_surface() -> None:
    async def run():
        async with Client(mcp) as client:
            result = await client.list_tools()
            return result.tools

    tools = asyncio.run(run())
    assert [tool.name for tool in tools] == [
        "stage_execute",
        "stage_status",
        "stage_collect",
        "terminal_open",
        "terminal_exec",
        "terminal_send",
        "terminal_read",
        "terminal_close",
        "artifact_inspect",
        "artifact_store",
        "artifact_apply",
    ]
    annotations = {tool.name: tool.annotations for tool in tools}
    assert annotations["stage_status"].read_only_hint is True
    assert annotations["stage_execute"].read_only_hint is False
    assert annotations["stage_collect"].idempotent_hint is True
    assert annotations["terminal_open"].idempotent_hint is True
    assert annotations["terminal_exec"].destructive_hint is True
    assert annotations["terminal_send"].read_only_hint is False
    assert annotations["terminal_read"].read_only_hint is True
    assert annotations["terminal_close"].destructive_hint is True
    assert annotations["artifact_inspect"].read_only_hint is True
    assert annotations["artifact_store"].destructive_hint is False
    assert annotations["artifact_apply"].idempotent_hint is False
    legacy_seven = {
        "stage_execute",
        "stage_status",
        "stage_collect",
        "terminal_open",
        "terminal_send",
        "terminal_read",
        "terminal_close",
    }
    assert legacy_seven.issubset({tool.name for tool in tools})
    assert "terminal_read_raw" not in {tool.name for tool in tools}


def test_server_identity_is_attached_without_copying_response_logic(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    expected = {
        "release_id": "harness-v2-focus-test",
        "source_root_sha256": "a" * 64,
        "plugin_version": "0.6.0",
        "mcp_schema_sha256": "b" * 64,
        "profile_id": "profile-test",
        "capabilities": ["wait_sidecar"],
        "host_resume_mode": "manual",
    }
    monkeypatch.setattr(server, "_cached_server_identity", lambda root, profile_id: expected)
    monkeypatch.setattr(server, "_solution_root", lambda: root)
    monkeypatch.setenv("SOLUTION_AGENT_PROFILE_ID", "profile-test")
    assert server._with_server({"ok": True}) == {"ok": True, "server": expected}


def test_legacy_stage_execute_contract_returns_upgrade_error_without_execution(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server, "_solution_root", lambda: tmp_path)
    monkeypatch.setattr(
        server,
        "_server_identity",
        lambda: {
            "release_id": "release-test",
            "source_root_sha256": "a" * 64,
            "plugin_version": "0.6.0",
            "mcp_schema_sha256": "b" * 64,
            "profile_id": "test",
            "capabilities": [],
            "host_resume_mode": "manual",
        },
    )

    async def run() -> dict:
        async with Client(mcp) as client:
            result = await client.call_tool("stage_execute", {"spec_path": "legacy.execution_spec.yaml"})
            return result.structured_content

    response = asyncio.run(run())
    assert response["ok"] is False
    assert response["error"]["phase"] == "client_contract"
    assert response["error"]["code"] == "CLIENT_SCHEMA_UPGRADE_REQUIRED"
    assert response["missing_arguments"] == ["task_capsule_id", "work_unit_id", "release_id"]
    assert response["full_output_fallback"] is False
    assert "job_id" not in response
    assert not (tmp_path / "executions").exists()


def test_terminal_open_has_no_legacy_rebind_gate(monkeypatch) -> None:
    class Manager:
        called = False

        def open(self, *args, **kwargs):
            self.called = True
            return {"ok": True}

    manager = Manager()
    monkeypatch.setattr(server, "_terminal_manager", lambda: manager)
    monkeypatch.setattr(server, "_with_server", lambda value: value)
    response = server.terminal_open(restart=False)
    assert response["ok"] is True
    assert manager.called is True


@pytest.mark.parametrize('finish_maintenance', [False, True])
def test_terminal_exec_binds_job_and_queues_completion_once(tmp_path: Path, monkeypatch, finish_maintenance) -> None:
    command_dir = tmp_path / "jobs" / ("a" * 32)
    command_dir.mkdir(parents=True)
    stdout = command_dir / "stdout.bin"
    stderr = command_dir / "stderr.bin"
    stdout.write_bytes(b"ok\n")
    stderr.write_bytes(b"")

    class Manager:
        calls: list[dict] = []
        profile = SimpleNamespace(
            default_terminal="main",
            slots={"main": SimpleNamespace(cwd=tmp_path)},
        )

        def exec(self, terminal_id, **kwargs):  # noqa: ANN001
            self.calls.append({"terminal_id": terminal_id, **kwargs})
            return {
                "ok": True,
                "command_id": "a" * 32,
                "command_dir": str(command_dir),
                "task_id": kwargs["task_id"],
                "work_unit_id": kwargs["work_unit_id"],
                "access_mode": kwargs["access_mode"],
                "writer_lease_id": kwargs["writer_lease_id"],
                "state": "succeeded",
                "event_id": "terminal-command:" + "a" * 32 + ":succeeded",
                "stdout": {"path": str(stdout)},
                "stderr": {"path": str(stderr)},
            }

    manager = Manager()
    monkeypatch.setattr(server, "_solution_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_terminal_manager", lambda: manager)
    monkeypatch.setattr(server, "_with_server", lambda value: value)
    monkeypatch.setattr(server, "_server_identity", lambda: {"release_id": "release-test"})

    first = server.terminal_exec(
        "TASK-TERMINAL",
        command="printf ok",
        work_unit_id="WU-1",
        timeout_seconds=10,
        finish_maintenance=finish_maintenance,
    )
    second = server.terminal_exec(
        "TASK-TERMINAL",
        command_id="a" * 32,
        work_unit_id="WU-1",
        timeout_seconds=10,
        finish_maintenance=finish_maintenance,
    )
    capsule = server._capsule_store().load("TASK-TERMINAL")

    assert first["state"] == second["state"] == "succeeded"
    assert manager.calls[0]["terminal_id"] is None
    assert manager.calls[0]["task_id"] == "TASK-TERMINAL"
    assert manager.calls[0]["work_unit_id"] == "WU-1"
    assert manager.calls[0]["access_mode"] == "writer"
    assert manager.calls[0]["writer_lease_id"].startswith("terminal-job-")
    assert first["writer_lease_released"] is True
    assert capsule.active_work_unit == (None if finish_maintenance else "WU-1")
    assert capsule.writer_leases == []
    assert capsule.queued_events == ["terminal-command:" + "a" * 32 + ":succeeded"]


def test_terminal_exec_writer_requires_work_unit_without_launching(tmp_path: Path, monkeypatch) -> None:
    class Manager:
        called = False

        def exec(self, *args, **kwargs):  # noqa: ANN001
            self.called = True
            raise AssertionError("writer validation must precede launch")

    manager = Manager()
    monkeypatch.setattr(server, "_solution_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_terminal_manager", lambda: manager)
    monkeypatch.setattr(server, "_with_server", lambda value: value)
    monkeypatch.setattr(server, "_server_identity", lambda: {"release_id": "release-test"})

    result = server.terminal_exec("TASK-NO-WORK-UNIT", command="printf no")
    capsule = server._capsule_store().load("TASK-NO-WORK-UNIT")

    assert result["error"]["code"] == "task_capsule_conflict"
    assert manager.called is False
    assert capsule.active_work_unit is None
    assert capsule.writer_leases == []


def test_terminal_exec_runner_exception_preserves_unknown_lease_and_claim(tmp_path: Path, monkeypatch) -> None:
    class Manager:
        profile = SimpleNamespace(
            default_terminal="main",
            slots={"main": SimpleNamespace(cwd=tmp_path)},
        )

        def exec(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("launch failed")

    monkeypatch.setattr(server, "_solution_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_terminal_manager", lambda: Manager())
    monkeypatch.setattr(server, "_with_server", lambda value: value)
    monkeypatch.setattr(server, "_server_identity", lambda: {"release_id": "release-test"})

    result = server.terminal_exec(
        "TASK-ROLLBACK",
        command="printf no",
        work_unit_id="WU-ROLLBACK",
    )
    capsule = server._capsule_store().load("TASK-ROLLBACK")

    assert result["ok"] is False
    assert capsule.active_work_unit == "WU-ROLLBACK"
    assert len(capsule.writer_leases) == 1


def test_plugin_skill_routes_to_read_only_markdown_reader_manager() -> None:
    root = Path(__file__).resolve().parents[1]
    skill_root = (
        root
        / "plugins"
        / "solution-agent-harness"
        / "skills"
        / "solution-agent-harness"
    )
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    managed_markdown = (skill_root / "references" / "managed-markdown.md").read_text(encoding="utf-8")
    assert "managed-markdown.md" in skill
    assert "research_md_snapshot" in managed_markdown
    assert "research_md_sync_plan" in managed_markdown
    assert "Phase 0 is read-only" in managed_markdown
    assert "base_candidate.json" in managed_markdown


def test_artifact_store_and_selective_read_update_the_same_task_capsule(tmp_path: Path, monkeypatch) -> None:
    tmp_path = tmp_path.resolve()  # Match the production _solution_root contract.
    monkeypatch.setattr(server, "_solution_root", lambda: tmp_path)
    monkeypatch.setattr(
        server,
        "_server_identity",
        lambda: {
            "release_id": "release-test",
            "source_root_sha256": "a" * 64,
            "plugin_version": "0.6.0",
            "mcp_schema_sha256": "b" * 64,
            "profile_id": "test",
            "capabilities": [],
            "host_resume_mode": "manual",
        },
    )
    source = tmp_path / "evidence.md"
    source.write_text("# Summary\nselected\n# Raw\nignored\n", encoding="utf-8")
    stored = server.artifact_store("focus-artifacts", str(source))
    assert stored["ok"] is True, stored
    artifact_id = stored["manifest"]["artifact_id"]
    selected = server.artifact_inspect(
        "focus-artifacts",
        artifact_id,
        mode="read",
        selector={"type": "markdown_heading", "value": "Summary"},
    )
    capsule = server._capsule_store().load("focus-artifacts")
    assert selected["content"] == "# Summary\nselected"
    assert [item.artifact_id for item in capsule.authoritative_artifacts] == [artifact_id]
    assert capsule.read_receipt_ids == [selected["read_receipt_id"]]
    assert selected["capsule_version"] == capsule.version


@pytest.mark.parametrize("source_release", ["harness-v2-focus-test", "previous-compatible-patch"])
def test_mcp_runs_and_collects_real_no_api_smoke_stage(tmp_path: Path, monkeypatch, source_release) -> None:
    tmp_path = tmp_path.resolve()  # Windows temporary roots may use 8.3 aliases.
    monkeypatch.setattr(server, "_solution_root", lambda: tmp_path)
    release_id = "harness-v2-focus-test"
    monkeypatch.setattr(
        server,
        "_server_identity",
        lambda: {
            "release_id": release_id,
            "source_root_sha256": "a" * 64,
            "plugin_version": "0.6.0",
            "mcp_schema_sha256": "b" * 64,
            "profile_id": "test",
            "capabilities": ["wait_sidecar"],
            "host_resume_mode": "manual",
        },
    )
    output = tmp_path / "artifacts"
    spec_path = tmp_path / "smoke.execution_spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "task_id": "harness_smoke",
                "job_type": "bounded_smoke",
                "workspace": {"root": str(tmp_path), "cwd": str(tmp_path)},
                "command": {
                    "script_id": "solution_agent_smoke_write_metrics",
                    "args": {"output_dir": str(output), "message": "harness MCP smoke ok"},
                    "timeout_sec": 30,
                },
                "resources": {"gpu_required": False, "gpu_count": 0},
                "artifacts": {
                    "required": [str(output / "metrics.json"), str(output / "artifact.txt")],
                    "optional": [],
                },
                "success_criteria": {
                    "exit_code": 0,
                    "metrics_file": str(output / "metrics.json"),
                    "metric_keys": ["ok", "message"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    capsule_store = server._capsule_store()
    created = capsule_store.create(
        TaskCapsule(
            task_id="focus-smoke",
            objective="bounded smoke",
            source_release_id=source_release,
            approval_owner="user",
            scope=ScopeContract(readable_paths=[str(tmp_path)], writable_paths=[str(tmp_path)]),
        )
    )
    assert created.active_work_unit is None

    async def run() -> dict:
        async with Client(mcp) as client:
            launched = await client.call_tool(
                "stage_execute",
                {
                    "spec_path": str(spec_path),
                    "task_capsule_id": "focus-smoke",
                    "work_unit_id": "bounded-smoke-1",
                    "release_id": release_id,
                },
            )
            assert launched.structured_content["ok"] is True, launched.structured_content
            job_id = launched.structured_content["job_id"]
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                status = await client.call_tool("stage_status", {"job_id": job_id})
                if status.structured_content["terminal"]:
                    break
                await asyncio.sleep(0.1)
            unchanged = await client.call_tool(
                "stage_status",
                {
                    "job_id": job_id,
                    "after_state_version": status.structured_content["state_version"],
                },
            )
            collected = await client.call_tool("stage_collect", {"job_id": job_id})
            replayed = await client.call_tool("stage_collect", {"job_id": job_id})
            return {
                "launched": launched.structured_content,
                "collected": collected.structured_content,
                "replayed": replayed.structured_content,
                "unchanged": unchanged.structured_content,
            }

    run_result = asyncio.run(run())
    assert run_result["launched"]["work_unit_claimed"] is True
    result = run_result["collected"]
    assert result["collected"] is True
    assert result["status"] == "succeeded"
    assert result["metrics_summary"]["ok"] is True
    assert result["metrics_summary"]["message"] == "harness MCP smoke ok"
    assert result["focus_runtime_integrated"] is True
    assert run_result["unchanged"]["unchanged"] is True
    assert "state" not in run_result["unchanged"]
    assert run_result["replayed"]["capsule_version"] == result["capsule_version"]
    closed_capsule = capsule_store.load(created.task_id)
    assert closed_capsule.source_release_id == source_release
    assert closed_capsule.active_work_unit is None
    assert closed_capsule.lifecycle_state == "CLOSED"
    ledger = UsageLedger(tmp_path / "executions" / result["job_id"], run_id=result["job_id"])
    events = ledger.read()
    assert [events[0].phase, events[-1].phase] == ["dispatch", "collect"]
    assert all(event.operation == "tool_call" for event in events)
    assert all(event.usage_source == "deterministic_counter" for event in events)
    assert sum(event.tool_call_delta for event in events) >= 3
    summary = ledger.summarize(write=False)
    assert summary["totals"]["tool_calls"] == len(events)
    assert summary["by_phase"]["wait"]["tool_calls"] >= 1


@pytest.mark.parametrize("access_mode", ["read_only", "writer"])
@pytest.mark.parametrize("schema_version", [1, 2])
def test_terminal_capsule_compatibility_is_not_release_equality(tmp_path, monkeypatch, access_mode, schema_version):
    calls = []
    manager = SimpleNamespace(
        profile=SimpleNamespace(default_terminal="main", slots={"main": SimpleNamespace(cwd=tmp_path)}),
        exec=lambda *args, **kwargs: calls.append(kwargs) or {
            "ok": True, "state": "running", "task_id": "OLD", "command_id": "a" * 32},
    )
    monkeypatch.setattr(server, "_solution_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_terminal_manager", lambda: manager)
    monkeypatch.setattr(server, "_with_server", lambda value: value)
    monkeypatch.setattr(server, "_server_identity", lambda: {"release_id": "new-patch"})
    capsule = server._capsule_store().create(TaskCapsule(
        task_id="OLD", objective="upgrade", source_release_id="old-patch", approval_owner="user",
        scope=ScopeContract(readable_paths=[str(tmp_path)], writable_paths=[str(tmp_path)])))
    path = tmp_path / "state/task_capsules/OLD/capsule.json"
    payload = json.loads(path.read_text())
    payload["schema_version"] = schema_version
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    result = server.terminal_exec("OLD", command="not actually launched", access_mode=access_mode,
                                  work_unit_id="next", wait_ms=0)
    assert result["ok"] is (schema_version == 1), result
    assert len(calls) == (1 if schema_version == 1 else 0)
    if schema_version == 1:
        assert server._capsule_store().load(capsule.task_id).source_release_id == "old-patch"
    else:
        assert path.read_bytes() == before


@pytest.mark.parametrize("code", ["not_found", "receipt_corrupt", "job_scope_conflict"])
def test_command_receipt_error_remains_an_error_not_wait_or_collection(tmp_path, monkeypatch, code):
    from solution_agent.harness.terminal_commands import CommandReceiptError
    def execute(*args, **kwargs):
        raise CommandReceiptError(code, "a" * 32)
    monkeypatch.setattr(server, "_solution_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_terminal_manager", lambda: SimpleNamespace(exec=execute))
    monkeypatch.setattr(server, "_with_server", lambda value: value)
    result = server.terminal_exec("OLD", command_id="a" * 32, wait_ms=0)
    assert not result["ok"]
    assert result["error"]["code"] == code
    assert "do not resubmit" in result["error"]["next_action"]
    assert "artifacts" not in result
    assert not (tmp_path / "state").exists()


def test_compatible_upgrade_does_not_take_over_active_work_unit(tmp_path, monkeypatch):
    manager = SimpleNamespace(exec=lambda *args, **kwargs: pytest.fail("must not launch another work unit"))
    monkeypatch.setattr(server, "_solution_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_terminal_manager", lambda: manager)
    monkeypatch.setattr(server, "_with_server", lambda value: value)
    monkeypatch.setattr(server, "_server_identity", lambda: {"release_id": "new-patch"})
    store = server._capsule_store()
    store.create(TaskCapsule(task_id="OLD", objective="upgrade", source_release_id="old-patch",
                            approval_owner="user", active_work_unit="running-unit", lifecycle_state="ACTIVE",
                            scope=ScopeContract(readable_paths=[str(tmp_path)], writable_paths=[str(tmp_path)])))
    before = store.load("OLD").model_dump()
    result = server.terminal_exec("OLD", command="never launch", work_unit_id="different-unit")
    assert result["error"]["code"] == "task_capsule_conflict"
    assert store.load("OLD").model_dump() == before
