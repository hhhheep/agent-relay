from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml

from .paths import solution_continuation_dir


def _load_yaml(path: Path) -> Dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return payload


def load_planner_policy() -> Dict[str, Any]:
    return _load_yaml(solution_continuation_dir() / "planner_policy.yaml")


def load_state_machine() -> Dict[str, Any]:
    return _load_yaml(solution_continuation_dir() / "state_machine.yaml")


def load_permission_matrix() -> Dict[str, Any]:
    return _load_yaml(solution_continuation_dir() / "permission_matrix.yaml")


def load_workflow_spec() -> Dict[str, Any]:
    return _load_yaml(solution_continuation_dir() / "workflow_spec.yaml")

