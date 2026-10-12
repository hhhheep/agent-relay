"""register_execution — validate, materialize, and register one execution."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .io_utils import atomic_write_json
from .script_registry import load_registry
from .spec import SpecValidationError, _parse, load_spec, validate_spec

_EXECUTIONS_DIR = Path(__file__).parent.parent.parent / "executions"


def _job_id(task_id: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return f"{task_id}_{ts}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _materialize(value: Any, job_id: str) -> Any:
    """Replace the explicit per-run token in scalar strings only."""
    if isinstance(value, dict):
        return {key: _materialize(item, job_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_materialize(item, job_id) for item in value]
    if isinstance(value, str):
        return value.replace("{job_id}", job_id)
    return value


def _entrypoint_sha256(spec, registry: dict) -> str | None:
    entrypoint = registry.get("scripts", {}).get(spec.command.script_id, {}).get("entrypoint")
    if not isinstance(entrypoint, list) or len(entrypoint) < 2:
        return None
    script = Path(str(entrypoint[1]))
    if not script.is_absolute():
        script = Path(spec.workspace.cwd) / script
    try:
        if script.is_file() and not script.is_symlink():
            return _sha256(script.read_bytes())
    except OSError:
        pass
    return None


def register_execution(
    spec_path: Path,
    registry_path: Path | None = None,
    executions_dir: Path = _EXECUTIONS_DIR,
) -> str:
    """
    Validate spec, create job directory, write initial state.json.

    Returns the job_id.  Raises SpecValidationError on invalid spec.
    """
    registry = load_registry(registry_path) if registry_path else load_registry()
    source_bytes = spec_path.read_bytes()
    source_spec = load_spec(spec_path, registry)

    source_data = yaml.safe_load(source_bytes.decode("utf-8"))
    executions_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        job_id = _job_id(source_spec.task_id)
        materialized_data = _materialize(source_data, job_id)
        spec = _parse(materialized_data)
        errors = validate_spec(spec, registry)
        if errors:
            raise SpecValidationError(errors)
        job_dir = executions_dir / job_id
        try:
            job_dir.mkdir(exist_ok=False)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("could not allocate a unique execution job_id")
    materialized_bytes = yaml.safe_dump(
        materialized_data,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")

    # Preserve both the reviewed source and the exact per-run materialization.
    (job_dir / "source_execution_spec.yaml").write_bytes(source_bytes)
    (job_dir / "execution_spec.yaml").write_bytes(materialized_bytes)
    reg_src = registry_path if registry_path else Path(__file__).parent / "script_registry.yaml"
    (job_dir / "script_registry.yaml").write_bytes(reg_src.read_bytes())

    state = {
        "status": "registered",
        "job_id": job_id,
        "task_id": spec.task_id,
        "job_type": spec.job_type,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "finished_at": None,
        "pid": None,
        "exit_code": None,
        "timeout_sec": spec.command.timeout_sec,
        "source_spec_sha256": _sha256(source_bytes),
        "materialized_spec_sha256": _sha256(materialized_bytes),
        "entrypoint_sha256": _entrypoint_sha256(spec, registry),
    }
    atomic_write_json(job_dir / "state.json", state)

    return job_id
