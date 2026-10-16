from __future__ import annotations

import pytest

from solution_agent.harness.remote_profile import parse_remote_deploy_profile


def test_pool_is_explicit_and_legacy_public_profile_stays_unchanged():
    from tests.test_worker_session_pool import policy
    value = _profile()
    assert "worker_pool" not in parse_remote_deploy_profile("FAN", value).public_dict()
    value["worker_pool"] = policy()
    parsed = parse_remote_deploy_profile("FAN", value)
    assert parsed.worker_pool["max_running"] == 3
    assert parsed.public_dict()["worker_pool"] == parsed.worker_pool
    value["worker_pool"]["max_running"] = True
    with pytest.raises(ValueError, match="worker_pool"):
        parse_remote_deploy_profile("FAN", value)


def _profile() -> dict:
    return {
        "host": "EXAMPLE_WORKER_A",
        "solution_agent_root": "/opt/solution-agent-control",
        "project_workdir": "/srv/example-workspaces/fan-workspace",
        "remote_run_root": "/srv/example-workspaces/harness-runs",
        "codex_path": "/home/agent-user/.local/bin/codex",
        "python_bootstrap": "/usr/bin/python3",
        "plugin_home": "/home/agent-user/plugins",
        "personal_marketplace": "/home/agent-user/.agents/plugins/marketplace.json",
        "provider_env_file": "/home/agent-user/.remote_materializer/.env",
        "remote_terminals": {
            "hub_name": "solution-agent-hub",
            "default": "main",
            "max_children": 8,
            "segment_bytes": 4194304,
            "retain_segments": 16,
            "slots": {
                "main": {
                    "window_name": "interactive-main",
                    "cwd": "/srv/example-workspaces/fan-workspace",
                    "shell": "/bin/bash",
                }
            },
        },
        "jobs": {"backend": "systemd_transient", "max_concurrent": 4},
        "initialize_checkout": False,
        "bootstrap": {"mode": "existing_checkout"},
    }


def test_remote_profile_separates_control_project_and_run_roots() -> None:
    value = _profile()
    value["binding_id"] = "example_worker_a_fan"
    value["remote_terminals"]["storage_profile_id"] = "EXAMPLE_WORKER_A_FAN"
    profile = parse_remote_deploy_profile("EXAMPLE_WORKER_A_FAN", value)
    assert profile.solution_agent_root != profile.project_workdir
    assert profile.remote_run_root != profile.project_workdir
    assert profile.bootstrap_mode == "existing_checkout"
    assert profile.terminal_default == "main"
    assert profile.terminal_hub_name == "solution-agent-hub"
    assert profile.terminal_max_children == 8
    assert profile.terminal_slots["main"]["window_name"] == "interactive-main"
    assert profile.binding_id == "example_worker_a_fan"
    assert profile.terminal_storage_profile_id == "EXAMPLE_WORKER_A_FAN"
    assert profile.job_backend == "systemd_transient"
    assert profile.job_max_concurrent == 4


@pytest.mark.parametrize("bad_root", ["<choose-root>", "/ssd6/project/runs/old-run-1/solution_agent"])
def test_remote_profile_rejects_placeholder_and_run_snapshot(bad_root: str) -> None:
    value = _profile()
    value["solution_agent_root"] = bad_root
    with pytest.raises(ValueError):
        parse_remote_deploy_profile("bad", value)


def test_remote_profile_rejects_role_aliasing() -> None:
    value = _profile()
    value["solution_agent_root"] = value["project_workdir"]
    with pytest.raises(ValueError, match="must be distinct"):
        parse_remote_deploy_profile("bad", value)


def test_remote_profile_requires_bootstrap_source_for_new_checkout() -> None:
    value = _profile()
    value["initialize_checkout"] = True
    with pytest.raises(ValueError, match="requires release_archive or git_ref"):
        parse_remote_deploy_profile("bad", value)


def test_remote_profile_accepts_hashed_release_archive_bootstrap() -> None:
    value = _profile()
    value["initialize_checkout"] = True
    value["bootstrap"] = {
        "mode": "release_archive",
        "source": "C:/release/solution-agent.tar.gz",
        "sha256": "a" * 64,
    }
    profile = parse_remote_deploy_profile("new", value)
    assert profile.initialize_checkout is True
    assert profile.bootstrap_sha256 == "a" * 64


def test_remote_profile_accepts_shared_workplace_for_distinct_child_windows() -> None:
    value = _profile()
    value["remote_terminals"]["slots"]["worker"] = {
        "window_name": "interactive-worker",
        "cwd": "/srv/example-workspaces/fan-workspace",
        "shell": "/bin/bash",
    }
    profile = parse_remote_deploy_profile("shared", value)
    assert profile.terminal_slots["main"]["cwd"] == profile.terminal_slots["worker"]["cwd"]


def test_remote_profile_rejects_duplicate_child_window_names() -> None:
    value = _profile()
    value["remote_terminals"]["slots"]["worker"] = {
        "window_name": "interactive-main",
        "cwd": "/srv/example-workspaces/fan-workspace",
        "shell": "/bin/bash",
    }
    with pytest.raises(ValueError, match="distinct tmux window"):
        parse_remote_deploy_profile("bad", value)


def test_external_profiles_file_override_and_legacy_fallback(tmp_path, monkeypatch):
    from solution_agent.harness.remote_profile import deployment_profiles_path, load_remote_deploy_profile
    import json
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"schema_version":1,"profiles":{"TEST":_profile()}}),encoding="utf-8")
    monkeypatch.setenv("SOLUTION_AGENT_PROFILES_FILE",str(path))
    assert deployment_profiles_path(tmp_path / "checkout") == path.resolve()
    assert load_remote_deploy_profile(deployment_profiles_path(tmp_path),"TEST").name == "TEST"
    monkeypatch.delenv("SOLUTION_AGENT_PROFILES_FILE")
    assert deployment_profiles_path(tmp_path) == tmp_path / "config/harness_remote_deploy_profiles.yaml"
