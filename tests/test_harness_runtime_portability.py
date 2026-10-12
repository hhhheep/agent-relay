from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from solution_agent.execution_port.execution_wrapper import _load_provider_environment
from solution_agent.execution_port.spec import _parse


ROOT = Path(__file__).resolve().parents[1]


def _spec(script_id: str):
    return _parse(
        {
            "task_id": "portable-runtime",
            "job_type": "smoke",
            "workspace": {"root": str(ROOT), "cwd": str(ROOT)},
            "command": {"script_id": script_id, "args": {}, "timeout_sec": 30},
            "resources": {},
            "artifacts": {"required": ["README.md"]},
            "success_criteria": {},
        }
    )


def test_provider_env_file_loads_only_allowlisted_keys(tmp_path: Path) -> None:
    env_file = tmp_path / "provider.env"
    env_file.write_text(
        "DASHSCOPE_API_KEY='secret-value'\n"
        "BAILIAN_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1\n"
        "UNRELATED_SECRET=must-not-propagate\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        env_file.chmod(0o600)
    loaded = _load_provider_environment(
        {"REPORT_WRITER_ENV_FILE": str(env_file), "UNCHANGED": "yes"},
        _spec("research_report_writer"),
    )
    assert loaded["DASHSCOPE_API_KEY"] == "secret-value"
    assert loaded["BAILIAN_BASE_URL"].endswith("/compatible-mode/v1")
    assert "UNRELATED_SECRET" not in loaded
    assert loaded["UNCHANGED"] == "yes"


def test_provider_env_file_is_not_loaded_for_other_stages(tmp_path: Path) -> None:
    missing = tmp_path / "missing.env"
    original = {"REPORT_WRITER_ENV_FILE": str(missing)}
    assert _load_provider_environment(original, _spec("solution_agent_smoke_write_metrics")) is original


def test_plugin_cache_launcher_propagates_checkout_import_path(tmp_path: Path) -> None:
    cached_script = tmp_path / "plugin-cache" / "scripts" / "server.py"
    cached_script.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "plugins" / "solution-agent-harness" / "scripts" / "server.py", cached_script)
    environment = dict(os.environ)
    environment["SOLUTION_AGENT_ROOT"] = str(ROOT)
    environment.pop("PYTHONPATH", None)
    environment.pop("SOLUTION_AGENT_VENV_REEXECED", None)
    completed = subprocess.run(
        [sys.executable, str(cached_script), "--check"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert Path(payload["root"]).resolve() == ROOT.resolve()
    identity = payload["runtime_identity"]
    from solution_agent.harness.release_identity import build_runtime_identity
    assert identity["release_id"] == build_runtime_identity(ROOT).release_id
    assert len(identity["source_root_sha256"]) == 64
    assert identity["plugin_version"] == build_runtime_identity(ROOT).plugin_version
    assert identity["host_resume_mode"] == "manual"
    assert "wait_sidecar" in identity["capabilities"]
    tool_manifest = payload["tool_manifest"]
    assert tool_manifest["tool_count"] == 11
    assert tool_manifest["ordered_tool_names"] == [
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
    assert tool_manifest["canonical_schema_bytes"] > 0
    assert len(tool_manifest["canonical_schema_sha256"]) == 64
