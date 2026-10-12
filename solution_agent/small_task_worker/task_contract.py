"""Parse task.md into a TaskContract dataclass."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass
class TaskContract:
    task_id: str
    title: str
    task_type: str = "small_probe"
    execution_stage: str = "prepare"      # prepare | run | analysis
    allow_long_running: bool = False
    run_command: str = ""                 # legacy: free-form shell command
    execution: dict = field(default_factory=dict)  # execution_port spec params
    goal: str = ""
    context_paths: List[str] = field(default_factory=list)
    current_problem: str = ""
    expected_behavior: str = ""
    allowed_modify: List[str] = field(default_factory=list)
    allowed_commands: List[str] = field(default_factory=list)
    forbidden_actions: List[str] = field(default_factory=list)
    expected_outputs: List[str] = field(default_factory=list)
    done_definition: str = ""
    escalation_rules: List[str] = field(default_factory=list)
    raw_front_matter: dict = field(default_factory=dict)


_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)

_ALLOWED_TASK_TYPES = {
    "small_probe", "small_module", "adapter_fix",
    "validator_fix", "artifact_writer_fix", "smoke_test_fix", "analysis_script_fix",
}

_DEFAULT_FORBIDDEN = [
    "full training", "dataset split changes", "checkpoint overwrite",
    "hidden codex/claude call", "destructive cleanup", "git push",
    "rm -rf", "sudo", "pip install", "conda install",
]

_DEFAULT_ALLOWED_COMMANDS = [
    "python -m pytest",
    "python -m py_compile",
    "python scripts/probes/",
    "python scripts/run_",
    "git diff",
    "git status",
]


def _extract_section(body: str, heading: str) -> str:
    pattern = re.compile(
        r"^##\s+" + re.escape(heading) + r"\s*$(.+?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(body)
    if not m:
        return ""
    return m.group(1).strip()


def _extract_list_section(body: str, heading: str) -> List[str]:
    raw = _extract_section(body, heading)
    if not raw:
        return []
    items = []
    for line in raw.splitlines():
        # strip markdown list markers and code fences
        stripped = re.sub(r"^[-*+]\s+|^```\w*\s*$", "", line.strip())
        if stripped and not stripped.startswith("```"):
            items.append(stripped)
    return items


def parse_task_md(path: Path) -> TaskContract:
    text = path.read_text(encoding="utf-8-sig")

    # split front matter
    front_matter: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                front_matter = yaml.safe_load(text[3:end]) or {}
            except yaml.YAMLError:
                front_matter = {}
            body = text[end + 4:]

    task_id = str(front_matter.get("task_id", path.stem))
    title = str(front_matter.get("title", ""))
    raw_type = str(front_matter.get("task_type", front_matter.get("type", "small_probe")))
    task_type = raw_type if raw_type in _ALLOWED_TASK_TYPES else "small_probe"

    raw_stage = str(front_matter.get("execution_stage", "prepare"))
    execution_stage = raw_stage if raw_stage in {"prepare", "run", "analysis"} else "prepare"
    allow_long_running = bool(front_matter.get("allow_long_running_commands", False))
    execution = dict(front_matter.get("execution") or {})

    allowed_modify = _extract_list_section(body, "Allowed Modify")
    allowed_commands = _extract_list_section(body, "Allowed Commands")
    forbidden_actions = _extract_list_section(body, "Forbidden Actions")
    expected_outputs = _extract_list_section(body, "Expected Outputs")
    escalation_rules = _extract_list_section(body, "Escalation Rules")
    context_paths = _extract_list_section(body, "Context Paths")

    # Run Command section — single command for execute-stage tasks
    run_command = _extract_section(body, "Run Command").strip()

    if not allowed_commands:
        allowed_commands = list(_DEFAULT_ALLOWED_COMMANDS)
    if not forbidden_actions:
        forbidden_actions = list(_DEFAULT_FORBIDDEN)

    return TaskContract(
        task_id=task_id,
        title=title,
        task_type=task_type,
        execution_stage=execution_stage,
        allow_long_running=allow_long_running,
        run_command=run_command,
        execution=execution,
        goal=_extract_section(body, "Goal"),
        context_paths=context_paths,
        current_problem=_extract_section(body, "Current Problem"),
        expected_behavior=_extract_section(body, "Expected Behavior"),
        allowed_modify=allowed_modify,
        allowed_commands=allowed_commands,
        forbidden_actions=forbidden_actions,
        expected_outputs=expected_outputs,
        done_definition=_extract_section(body, "Done Definition"),
        escalation_rules=escalation_rules,
        raw_front_matter=front_matter,
    )
