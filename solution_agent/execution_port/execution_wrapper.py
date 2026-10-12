"""execution_wrapper — launched by safe_runner, runs the actual command.

Called as:
    python -m solution_agent.execution_port.execution_wrapper <job_dir>

Writes to job_dir:
    stdout.log, stderr.log, exit_code.txt, wrapper_status.json
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .io_utils import atomic_write_json, atomic_write_text
from .script_registry import build_argv, load_registry
from .spec import _parse
from ..utils.subprocess_utils import hidden_subprocess_kwargs


_PROVIDER_ENV_KEYS = {"DASHSCOPE_API_KEY", "BAILIAN_BASE_URL"}


def _load_provider_environment(environment: dict[str, str], spec) -> dict[str, str]:
    """Load only allowlisted report-writer keys without logging their values."""
    if spec.command.script_id != "research_report_writer":
        return environment
    configured = environment.get("REPORT_WRITER_ENV_FILE", "").strip()
    if not configured:
        return environment
    path = Path(configured)
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError("REPORT_WRITER_ENV_FILE must be an existing absolute file")
    if path.stat().st_size > 65536:
        raise RuntimeError("REPORT_WRITER_ENV_FILE exceeds 64 KiB")
    if os.name != "nt" and path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
        raise RuntimeError("REPORT_WRITER_ENV_FILE must not be group/world readable")
    loaded = dict(environment)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in _PROVIDER_ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            loaded[key] = value
    if not loaded.get("DASHSCOPE_API_KEY", "").strip():
        raise RuntimeError("REPORT_WRITER_ENV_FILE does not configure DASHSCOPE_API_KEY")
    return loaded


def run(job_dir: Path) -> int:
    spec_path = job_dir / "execution_spec.yaml"
    data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec = _parse(data)

    reg_path = job_dir / "script_registry.yaml"
    registry = load_registry(reg_path)

    argv = build_argv(spec.command.script_id, spec.command.args, registry)
    cwd = spec.workspace.cwd
    env = dict(os.environ)
    env["SOLUTION_AGENT_INPUT_ROOTS"] = os.pathsep.join(
        [spec.workspace.root, *spec.workspace.input_roots]
    )
    env["SOLUTION_AGENT_OUTPUT_ROOTS"] = os.pathsep.join(
        [spec.workspace.root, *spec.workspace.artifact_roots]
    )
    implicit_authorities = [
        str(Path(spec.workspace.root) / "research" / "records"),
        str(Path(spec.workspace.root) / ".research"),
    ]
    env["SOLUTION_AGENT_AUTHORITY_ROOTS"] = os.pathsep.join(
        [*implicit_authorities, *spec.workspace.authority_roots]
    )
    env["SOLUTION_AGENT_USAGE_RUN_DIR"] = str(job_dir.resolve())
    env["SOLUTION_AGENT_USAGE_RUN_ID"] = job_dir.name
    env["SOLUTION_AGENT_USAGE_SIDE"] = (
        "external_writer" if spec.command.script_id == "research_report_writer" else "remote_worker"
    )
    env = _load_provider_environment(env, spec)

    started_at = datetime.now(timezone.utc).isoformat()

    with (job_dir / "stdout.log").open("w", encoding="utf-8") as out_fh, \
         (job_dir / "stderr.log").open("w", encoding="utf-8") as err_fh:
        result = subprocess.run(
            argv,
            stdout=out_fh,
            stderr=err_fh,
            cwd=cwd,
            env=env,
            **hidden_subprocess_kwargs(),
        )

    exit_code = result.returncode
    finished_at = datetime.now(timezone.utc).isoformat()
    _ensure_closure_package(spec, exit_code)

    atomic_write_json(job_dir / "wrapper_status.json", {
        "started_at": started_at,
        "finished_at": finished_at,
        "argv": argv,
        "exit_code": exit_code,
    })
    # Readers treat this marker as completion, so publish it last and atomically.
    atomic_write_text(job_dir / "exit_code.txt", str(exit_code))

    return exit_code


def _ensure_closure_package(spec, exit_code: int) -> None:
    output_dir = str(spec.command.args.get("output_dir") or "").strip()
    if not output_dir:
        return
    target = Path(output_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    notes = target / "worker_notes.md"
    if not notes.exists():
        notes.write_text(
            "# Worker Notes\n\n"
            f"- script_id: {spec.command.script_id}\n"
            f"- exit_code: {exit_code}\n",
            encoding="utf-8",
        )
    summary = target / "artifact_summary.md"
    if not summary.exists():
        summary.write_text(
            "# Artifact Summary\n\n"
            "Closure package generated by execution_wrapper because the script did not write one.\n\n"
            f"- script_id: {spec.command.script_id}\n"
            f"- exit_code: {exit_code}\n",
            encoding="utf-8",
        )
    requests = target / "action_requests.json"
    if not requests.exists():
        requests.write_text(
            json.dumps(
                {
                    "script_id": spec.command.script_id,
                    "exit_code": exit_code,
                    "action_requests": [],
                    "generated_by": "execution_wrapper",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m solution_agent.execution_port.execution_wrapper <job_dir>",
              file=sys.stderr)
        sys.exit(1)
    sys.exit(run(Path(sys.argv[1])))
