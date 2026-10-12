"""ExecutionSpec — schema, dataclasses, and validator for execution_port.

Path validation uses posixpath (not pathlib.Path) so that Linux-style paths
like /raid/fan/... validate correctly on Windows dev machines.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml

# ── Exceptions ────────────────────────────────────────────────────────────────

class SpecValidationError(Exception):
    def __init__(self, errors: List[str]) -> None:
        self.errors = errors
        super().__init__("spec validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class WorkspaceSpec:
    root: str   # stored as raw string — Linux path, posixpath for validation
    cwd: str
    input_roots: List[str] = field(default_factory=list)
    artifact_roots: List[str] = field(default_factory=list)
    authority_roots: List[str] = field(default_factory=list)


@dataclass
class CommandSpec:
    script_id: str
    args: Dict[str, Any] = field(default_factory=dict)
    timeout_sec: int = 0


@dataclass
class ResourcesSpec:
    gpu_required: bool = False
    gpu_count: int = 0


@dataclass
class ArtifactsSpec:
    required: List[str] = field(default_factory=list)
    optional: List[str] = field(default_factory=list)


@dataclass
class SuccessCriteriaSpec:
    exit_code: int = 0
    metrics_file: str = ""
    metric_keys: List[str] = field(default_factory=list)


@dataclass
class EvidenceSpec:
    token_guard_receipt_required: bool = False
    token_guard_receipt: str = ""


@dataclass
class ExecutionSpec:
    task_id: str
    job_type: str
    workspace: WorkspaceSpec
    command: CommandSpec
    resources: ResourcesSpec
    artifacts: ArtifactsSpec
    success_criteria: SuccessCriteriaSpec
    evidence: EvidenceSpec = field(default_factory=EvidenceSpec)


# ── Parser ────────────────────────────────────────────────────────────────────

_SCRIPT_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")
_MAX_TIMEOUT = 14400


def _parse(data: dict) -> ExecutionSpec:
    ws = data.get("workspace", {})
    cmd = data.get("command", {})
    res = data.get("resources", {})
    art = data.get("artifacts", {})
    sc = data.get("success_criteria", {})
    evidence = data.get("evidence", {})

    return ExecutionSpec(
        task_id=str(data.get("task_id", "")),
        job_type=str(data.get("job_type", "")),
        workspace=WorkspaceSpec(
            root=str(ws.get("root", "")),
            cwd=str(ws.get("cwd", "")),
            input_roots=list(ws.get("input_roots") or ws.get("allowed_input_roots") or []),
            artifact_roots=list(ws.get("artifact_roots") or ws.get("allowed_artifact_roots") or []),
            authority_roots=list(ws.get("authority_roots") or ws.get("managed_authority_roots") or []),
        ),
        command=CommandSpec(
            script_id=str(cmd.get("script_id", "")),
            args=dict(cmd.get("args") or {}),
            timeout_sec=int(cmd.get("timeout_sec", 0)),
        ),
        resources=ResourcesSpec(
            gpu_required=bool(res.get("gpu_required", False)),
            gpu_count=int(res.get("gpu_count", 0)),
        ),
        artifacts=ArtifactsSpec(
            required=list(art.get("required") or []),
            optional=list(art.get("optional") or []),
        ),
        success_criteria=SuccessCriteriaSpec(
            exit_code=int(sc.get("exit_code", 0)),
            metrics_file=str(sc.get("metrics_file", "")),
            metric_keys=list(sc.get("metric_keys") or []),
        ),
        evidence=EvidenceSpec(
            token_guard_receipt_required=bool(evidence.get("token_guard_receipt_required", False)),
            token_guard_receipt=str(evidence.get("token_guard_receipt", "")),
        ),
    )


# ── Validator ─────────────────────────────────────────────────────────────────

def _is_absolute_path(path: str) -> bool:
    """Accept POSIX (/...) and Windows (C:\\... or C:/...) absolute paths."""
    return path.startswith("/") or Path(path).is_absolute()


def _is_under_path(child: str, parent: str) -> bool:
    """Return True if child is equal to or under parent (cross-platform)."""
    if parent.startswith("/") and child.startswith("/"):
        # POSIX: use posixpath so ../ is normalised correctly
        child_norm = posixpath.normpath(child)
        parent_norm = posixpath.normpath(parent)
        return child_norm == parent_norm or child_norm.startswith(parent_norm + "/")
    # Windows (or mixed): use pathlib
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _entrypoint_script_path(entrypoint: Any, cwd: str) -> str:
    if not isinstance(entrypoint, list) or len(entrypoint) < 2:
        return ""
    runner = str(entrypoint[0]).lower()
    script = str(entrypoint[1])
    if "python" not in runner or script == "-m" or not script.endswith(".py"):
        return ""
    if _is_absolute_path(script):
        return script
    if cwd.startswith("/"):
        return posixpath.normpath(posixpath.join(cwd, script))
    return str((Path(cwd) / script).resolve())


def _resolve_arg_path(value: str, cwd: str) -> str:
    if _is_absolute_path(value):
        return posixpath.normpath(value) if value.startswith("/") else str(Path(value).resolve())
    if cwd.startswith("/"):
        return posixpath.normpath(posixpath.join(cwd, value))
    return str((Path(cwd) / value).resolve())


def _join_root(root: str, relative: str) -> str:
    if root.startswith("/"):
        return posixpath.normpath(posixpath.join(root, relative))
    return str((Path(root) / relative).resolve())


def validate_spec(spec: ExecutionSpec, registry: dict) -> List[str]:
    """
    Return a list of error strings.  Empty list means valid.

    Uses posixpath for all path operations so Linux-style paths validate
    correctly on Windows dev machines.

    registry format:
      {"scripts": {"script_id": {"args": {...}, "gpu_allowed": bool,
                                 "max_timeout_sec": int, ...}}}
    """
    errors: List[str] = []
    scripts: dict = registry.get("scripts", {})
    root = spec.workspace.root
    cwd = spec.workspace.cwd
    input_roots = [root, *[str(item) for item in spec.workspace.input_roots if str(item).strip()]]
    artifact_roots = [root, *[str(item) for item in spec.workspace.artifact_roots if str(item).strip()]]
    authority_roots = [
        _join_root(root, "research/records"),
        _join_root(root, ".research"),
        *[str(item) for item in spec.workspace.authority_roots if str(item).strip()],
    ]
    timeout_limit = _MAX_TIMEOUT

    # ── 9: workspace.root / cwd must be absolute (POSIX) ─────────────────────
    root_abs = _is_absolute_path(root)
    cwd_abs = _is_absolute_path(cwd)
    if not root_abs:
        errors.append(f"workspace.root is not absolute: {root!r}")
    if not cwd_abs:
        errors.append(f"workspace.cwd is not absolute: {cwd!r}")

    # ── 4: cwd must be under root ─────────────────────────────────────────────
    if root_abs and cwd_abs and not _is_under_path(cwd, root):
        errors.append(
            f"workspace.cwd {cwd!r} is not under workspace.root {root!r}"
        )

    # ── 3: script_id must look like an identifier (no shell chars) ────────────
    sid = spec.command.script_id
    if not sid:
        errors.append("command.script_id is empty")
    elif not _SCRIPT_ID_RE.match(sid):
        errors.append(
            f"command.script_id {sid!r} looks like a shell string — "
            "use a registry script_id instead"
        )
    else:
        # ── 1: script_id must exist in registry ──────────────────────────────
        if sid not in scripts:
            errors.append(f"command.script_id {sid!r} not found in script registry")
        else:
            reg_script = scripts[sid]
            try:
                timeout_limit = max(_MAX_TIMEOUT, int(reg_script.get("max_timeout_sec") or _MAX_TIMEOUT))
            except (TypeError, ValueError):
                timeout_limit = _MAX_TIMEOUT
            allowed_args: dict = reg_script.get("args", {})
            entrypoint_script = _entrypoint_script_path(reg_script.get("entrypoint"), cwd)
            if entrypoint_script:
                if root_abs and not _is_under_path(entrypoint_script, root):
                    errors.append(
                        f"registry entrypoint for script {sid!r} resolves outside workspace.root"
                    )
                if reg_script.get("entrypoint_must_exist", False):
                    try:
                        cwd_exists = Path(cwd).exists()
                        entrypoint_exists = Path(entrypoint_script).exists()
                    except OSError:
                        cwd_exists = False
                        entrypoint_exists = False
                    if cwd_exists and not entrypoint_exists:
                        errors.append(
                            f"registry entrypoint for script {sid!r} does not exist: {entrypoint_script!r}"
                        )

            # ── 2: every arg key must be in registry's allowed args ───────────
            for key in spec.command.args:
                if key not in allowed_args:
                    errors.append(
                        f"command.args key {key!r} is not in allowed args for "
                        f"script {sid!r}"
                    )
                    continue
                arg_def = allowed_args.get(key, {})
                value = spec.command.args.get(key)
                if isinstance(arg_def, dict) and arg_def.get("type") == "path" and value not in (None, ""):
                    role = str(arg_def.get("path_role") or "").strip().lower()
                    values = value if isinstance(value, list) else [value]
                    for raw_value in values:
                        resolved_arg = _resolve_arg_path(str(raw_value), cwd)
                        if role == "input" and not any(
                            _is_under_path(resolved_arg, allowed_root)
                            for allowed_root in input_roots
                            if _is_absolute_path(allowed_root)
                        ):
                            errors.append(
                                f"command.args.{key} input path resolves outside workspace.root/input_roots: {resolved_arg!r}"
                            )
                        if role == "output":
                            if not any(
                                _is_under_path(resolved_arg, allowed_root)
                                for allowed_root in artifact_roots
                                if _is_absolute_path(allowed_root)
                            ):
                                errors.append(
                                    f"command.args.{key} output path resolves outside workspace.root/artifact_roots: {resolved_arg!r}"
                                )
                            if any(
                                _is_under_path(resolved_arg, managed_root)
                                for managed_root in authority_roots
                                if _is_absolute_path(managed_root)
                            ):
                                errors.append(
                                    f"command.args.{key} output path targets a managed authority root: {resolved_arg!r}"
                                )
                if isinstance(arg_def, dict) and arg_def.get("type") == "path" and arg_def.get("must_exist") and value not in (None, ""):
                    values = value if isinstance(value, list) else [value]
                    for raw_value in values:
                        resolved_arg = _resolve_arg_path(str(raw_value), cwd)
                        try:
                            cwd_exists = Path(cwd).exists()
                            arg_exists = Path(resolved_arg).exists()
                        except OSError:
                            cwd_exists = False
                            arg_exists = False
                        if cwd_exists and not arg_exists:
                            errors.append(
                                f"command.args.{key} path must exist but was not found: {resolved_arg!r}"
                            )

            # ── 8: gpu_required but script does not allow GPU ─────────────────
            if spec.resources.gpu_required and not reg_script.get("gpu_allowed", False):
                errors.append(
                    f"resources.gpu_required=true but script {sid!r} has "
                    "gpu_allowed=false in registry"
                )

    # ── 6: timeout_sec must be present and within bounds ─────────────────────
    if spec.command.timeout_sec <= 0:
        errors.append(
            "command.timeout_sec is missing or <= 0 (must be 1–14400)"
        )
    elif spec.command.timeout_sec > timeout_limit:
        errors.append(
            f"command.timeout_sec {spec.command.timeout_sec} exceeds maximum "
            f"{timeout_limit}"
        )

    # ── 7: artifacts.required must not be empty ───────────────────────────────
    if not spec.artifacts.required:
        errors.append(
            "artifacts.required is empty — at least one required artifact must be declared"
        )

    # ── 5: artifact paths must resolve under workspace.root ──────────────────
    if root_abs:
        for input_root in input_roots:
            if not _is_absolute_path(input_root):
                errors.append(f"workspace.input_roots contains non-absolute path: {input_root!r}")
        for artifact_root in artifact_roots:
            if not _is_absolute_path(artifact_root):
                errors.append(f"workspace.artifact_roots contains non-absolute path: {artifact_root!r}")
        for authority_root in authority_roots:
            if not _is_absolute_path(authority_root):
                errors.append(f"workspace.authority_roots contains non-absolute path: {authority_root!r}")
            elif not any(
                _is_under_path(authority_root, allowed_root)
                for allowed_root in artifact_roots
                if _is_absolute_path(allowed_root)
            ):
                errors.append(
                    f"workspace.authority_roots entry is outside workspace.root/artifact_roots: {authority_root!r}"
                )
        for rel in list(spec.artifacts.required) + list(spec.artifacts.optional):
            if _is_absolute_path(rel):
                resolved = posixpath.normpath(rel) if rel.startswith("/") else str(Path(rel).resolve())
            elif root.startswith("/"):
                resolved = posixpath.normpath(posixpath.join(root, rel))
            else:
                resolved = str((Path(root) / rel).resolve())
            under = any(
                _is_under_path(resolved, artifact_root)
                for artifact_root in artifact_roots
                if _is_absolute_path(artifact_root)
            )
            if not under:
                errors.append(
                    f"artifact path {rel!r} resolves outside workspace.root/artifact_roots"
                )

    # Token Guard evidence is optional for legacy/small jobs, but fail closed
    # when a high-output stage explicitly requires it.
    receipt = spec.evidence.token_guard_receipt
    if spec.evidence.token_guard_receipt_required and not receipt:
        errors.append("evidence.token_guard_receipt is required")
    if receipt and root_abs:
        resolved_receipt = _resolve_arg_path(receipt, root)
        if not _is_under_path(resolved_receipt, root):
            errors.append("evidence.token_guard_receipt resolves outside workspace.root")

    return errors


def load_spec(path: Path, registry: dict) -> ExecutionSpec:
    """Load, parse, and validate an execution_spec.yaml.  Raises SpecValidationError."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SpecValidationError(["execution_spec.yaml must be a YAML mapping"])
    spec = _parse(data)
    errors = validate_spec(spec, registry)
    if errors:
        raise SpecValidationError(errors)
    return spec
