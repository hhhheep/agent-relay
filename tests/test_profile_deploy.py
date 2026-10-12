from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from solution_agent.harness.profile_deploy import (
    _INSPECT,
    _remote_python_command,
    _ssh_python,
    _verify_conflict_snapshot,
    deploy_profile_bundle,
)


def test_remote_inventory_script_continues_for_non_git_workplace(tmp_path: Path) -> None:
    solution_root = tmp_path / "solution"
    workplace = tmp_path / "workplace"
    run_root = tmp_path / "runs"
    for path in (solution_root, workplace, run_root):
        path.mkdir()
    (workplace / "data.txt").write_text("present", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-c", _INSPECT, str(solution_root), str(workplace), str(run_root)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    payload = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert payload["project_workdir"]["is_git_checkout"] is False
    assert payload["project_workdir"]["exists"] is True
    assert payload["roles_distinct"] is True


def test_remote_python_command_contains_no_unquoted_user_code() -> None:
    command = _remote_python_command("/usr/bin/python3", "print('hello')", ["/path/with space"])
    assert "print('hello')" not in command
    assert "base64.b64decode" in command
    assert "'/path/with space'" in command


def test_deploy_rejects_local_hash_mismatch_before_network(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles.yaml"
    profiles.write_text(
        """schema_version: 1
profiles:
  TEST:
    host: declared-host
    solution_agent_root: /opt/solution-agent
    project_workdir: /work/project
    remote_run_root: /work/runs
    codex_path: /usr/bin/codex
    python_bootstrap: /usr/bin/python3
    plugin_home: /home/test/plugins
    personal_marketplace: /home/test/.agents/plugins/marketplace.json
    provider_env_file: /home/test/provider.env
    remote_terminals:
      hub_name: solution-agent-hub
      default: main
      slots:
        main: {window_name: main, cwd: /work/project, shell: /bin/bash}
    jobs:
      backend: systemd_transient
      max_concurrent: 4
    initialize_checkout: false
    bootstrap: {mode: existing_checkout}
""",
        encoding="utf-8",
    )
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"bundle")
    installer = tmp_path / "installer.py"
    installer.write_text("pass\n", encoding="utf-8")
    actual = hashlib.sha256(bundle.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="local bundle SHA-256 mismatch"):
        deploy_profile_bundle(
            profiles_path=profiles,
            profile_name="TEST",
            bundle=bundle,
            installer=installer,
            expected_sha256="0" * 64 if actual != "0" * 64 else "1" * 64,
        )


def test_ssh_python_preserves_structured_safety_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 5
        stdout = '{"ok": false, "error": {"code": "dry_run_gate_failed"}}'
        stderr = ""

    monkeypatch.setattr("solution_agent.harness.profile_deploy._run", lambda *args, **kwargs: Result())
    profile = type("Profile", (), {"python_bootstrap": "/usr/bin/python3", "host": "declared-host"})()
    result = _ssh_python(profile, "print('{}')", [])
    assert result["error"]["code"] == "dry_run_gate_failed"
    assert result["transport_returncode"] == 5


def test_verify_conflict_snapshot_checks_declared_file_hashes(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.zip"
    payload = b"remote-current\n"
    current_hash = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema_version": "1.0",
        "kind": "solution-agent-conflict-snapshot",
        "bundle_sha256": "1" * 64,
        "target_root": "/opt/solution-agent",
        "conflicts": [
            {
                "path": "pkg/file.txt",
                "current_sha256": current_hash,
                "base_sha256": "2" * 64,
                "overlay_sha256": "3" * 64,
                "present": True,
            }
        ],
    }
    with zipfile.ZipFile(snapshot, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("files/pkg/file.txt", payload)
    observed = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    assert _verify_conflict_snapshot(snapshot, observed) == manifest


def test_verify_conflict_snapshot_rejects_tampered_file(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.zip"
    manifest = {
        "schema_version": "1.0",
        "kind": "solution-agent-conflict-snapshot",
        "bundle_sha256": "1" * 64,
        "target_root": "/opt/solution-agent",
        "conflicts": [
            {
                "path": "pkg/file.txt",
                "current_sha256": hashlib.sha256(b"expected").hexdigest(),
                "base_sha256": None,
                "overlay_sha256": "3" * 64,
                "present": True,
            }
        ],
    }
    with zipfile.ZipFile(snapshot, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("files/pkg/file.txt", b"tampered")
    observed = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="file hash mismatch"):
        _verify_conflict_snapshot(snapshot, observed)
