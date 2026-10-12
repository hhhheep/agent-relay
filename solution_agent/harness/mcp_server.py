from __future__ import annotations

import json
import os
import re
import atexit
import difflib
import hashlib
import uuid
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from solution_agent.execution_port.collector import collect, verify_collection_seal
from solution_agent.execution_port.io_utils import read_json
from solution_agent.execution_port.monitor import check_once
from solution_agent.execution_port.registry import register_execution
from solution_agent.execution_port.safe_runner import run_execution
from solution_agent.harness.remote_terminal import (
    ProfileTerminalManager,
    TerminalError,
    load_terminal_profile,
    verify_workplace_identity,
)
from solution_agent.harness.release_identity import build_runtime_identity
from solution_agent.harness.terminal_commands import CommandReceiptError
from solution_agent.harness.protocol import ActivationGate, EndpointIdentity, ProtocolMismatch, PROTOCOL_VERSION
from solution_agent.harness.artifact_apply import ArtifactApplyEngine
from solution_agent.harness.artifact_runtime import ArtifactStore, Selector
from solution_agent.harness.task_capsule import (
    ArtifactRef,
    CapsuleConflict,
    ScopeContract,
    TaskCapsule,
    TaskCapsuleStore,
    WaitRef,
    WriterLeaseRef,
    atomic_write_json,
)
from solution_agent.harness.telemetry_contracts import UsageEvent
from solution_agent.harness.usage_ledger import UsageLedger, token_guard_receipt_event
from solution_agent.harness.contracts import VerifiedReceipt
from solution_agent.harness.wait_runtime import (
    initialize_wait,
    start_wait_monitor,
    synchronize_collection_result,
    synchronize_execution_state,
    wait_disposition,
)


def _solution_root() -> Path:
    configured = os.environ.get("SOLUTION_AGENT_ROOT", "").strip()
    root = Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]
    if not (root / "solution_agent").is_dir():
        raise RuntimeError(f"invalid SOLUTION_AGENT_ROOT: {root}")
    return root


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path is outside solution-agent root: {path}") from exc
    return resolved


def _job_dir(job_id: str) -> Path:
    if not job_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in job_id):
        raise ValueError("job_id contains unsupported characters")
    return _solution_root() / "executions" / job_id


def _compact_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: state.get(key)
        for key in (
            "job_id",
            "task_id",
            "job_type",
            "status",
            "registered_at",
            "started_at",
            "finished_at",
            "collected_at",
            "exit_code",
            "source_spec_sha256",
            "materialized_spec_sha256",
            "entrypoint_sha256",
        )
        if key in state
    }


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics_summary") if isinstance(result.get("metrics_summary"), dict) else {}
    return {
        "task_id": result.get("task_id"),
        "job_id": result.get("job_id"),
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "artifacts_found": list(result.get("artifacts_found") or [])[:64],
        "artifacts_missing": list(result.get("artifacts_missing") or [])[:64],
        "artifact_manifest": list(result.get("artifact_manifest") or [])[:64],
        "metrics_summary": metrics,
        "metrics_missing": list(result.get("metrics_missing") or [])[:64],
        "metrics_invalid": list(result.get("metrics_invalid") or [])[:64],
        "model_execution_evidence": result.get("model_execution_evidence"),
        "token_guard_receipt": result.get("token_guard_receipt"),
        "token_guard_receipt_errors": result.get("token_guard_receipt_errors"),
        "judge": result.get("judge"),
        "failure_type": result.get("failure_type"),
        "warning_type": result.get("warning_type"),
        "recommended_action": result.get("recommended_action"),
    }


_USAGE_SIDES = {
    "local_controller",
    "remote_controller",
    "remote_worker",
    "external_writer",
}


def _usage_side() -> str:
    configured = os.environ.get("SOLUTION_AGENT_USAGE_SIDE", "local_controller").strip()
    return configured if configured in _USAGE_SIDES else "local_controller"


def _record_stage_usage(job_dir: Path, *, phase: str, result: dict[str, Any]) -> None:
    """Best-effort P0 observation; telemetry must not change stage behavior."""

    try:
        payload_bytes = len(
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        ledger = UsageLedger(job_dir, run_id=job_dir.name)
        ledger.append(
            UsageEvent(
                run_id=job_dir.name,
                side=_usage_side(),
                phase=phase,
                operation="tool_call",
                provider="none",
                usage_source="deterministic_counter",
                tool_call_delta=1,
                result_payload_bytes=payload_bytes,
            )
        )
        ledger.summarize()
    except (OSError, ValueError, TypeError, RuntimeError):
        # P0 is observation-only. A later enforcement phase may make this a gate.
        return


def _record_token_guard_usage(job_dir: Path, result: dict[str, Any]) -> None:
    """Merge a verified command receipt once, even when collect is replayed."""

    receipt_payload = result.get("token_guard_receipt")
    if not isinstance(receipt_payload, dict):
        return
    try:
        receipt = VerifiedReceipt.model_validate(receipt_payload)
        payload_bytes = len(
            json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        ledger = UsageLedger(job_dir, run_id=job_dir.name)
        ledger.append(
            token_guard_receipt_event(
                run_id=job_dir.name,
                side="remote_worker",
                phase="execute",
                receipt=receipt,
                result_payload_bytes=payload_bytes,
            )
        )
        ledger.summarize()
    except (OSError, ValueError, TypeError, RuntimeError):
        return


mcp = MCPServer(
    "solution-agent-harness",
    description="Run bounded solution-agent stages and control profile-declared persistent terminals.",
    version="0.6.0",
)


_terminal_manager_instance: ProfileTerminalManager | None = None
_activation_gate = ActivationGate()


def _require_activation() -> None:
    """Fail closed only for managed launches; unit/library callers remain inspectable."""
    if os.environ.get("SOLUTION_AGENT_PROTOCOL_REQUIRED") == "1":
        _activation_gate.require()


def activate_protocol(local: dict[str, Any], remote: dict[str, Any], generation: int = 1) -> dict[str, Any]:
    left = EndpointIdentity(
        str(local.get("protocol_version", "")), str(local.get("release_id", "")),
        str(local.get("payload_sha256", local.get("source_root_sha256", ""))),
        str(local.get("mcp_schema_sha256", "")),
    )
    right = EndpointIdentity(
        str(remote.get("protocol_version", "")), str(remote.get("release_id", "")),
        str(remote.get("payload_sha256", remote.get("source_root_sha256", ""))),
        str(remote.get("mcp_schema_sha256", "")),
    )
    _activation_gate.activate(left, right, generation)
    return {"protocol_version": PROTOCOL_VERSION, "generation": generation, "release_id": left.release_id}


def _terminal_manager() -> ProfileTerminalManager:
    global _terminal_manager_instance
    if _terminal_manager_instance is None:
        _terminal_manager_instance = ProfileTerminalManager(load_terminal_profile(_solution_root()))
    return _terminal_manager_instance


def _release_terminal_leases() -> None:
    if _terminal_manager_instance is not None:
        _terminal_manager_instance.release_all()


atexit.register(_release_terminal_leases)


_SECRET_PATTERNS = (
    re.compile(r"(?i)(?:api[_-]?key|token|password|secret)\s*[=:]\s*\S+"),
    re.compile(r"\b(?:sk|hf|ghp|github_pat)-[A-Za-z0-9_-]{12,}\b"),
)


def _sanitized_error(exc: Exception) -> str:
    message = str(exc)[:1200]
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub("[REDACTED]", message)
    return message


@lru_cache(maxsize=8)
def _cached_server_identity(root: str, profile_id: str | None) -> dict[str, Any]:
    return build_runtime_identity(Path(root), profile_id=profile_id, host_resume_mode="manual").compact()


def _server_identity() -> dict[str, Any]:
    root = _solution_root()
    profile_id = os.environ.get("SOLUTION_AGENT_BINDING_ID") or None
    if profile_id is None:
        try:
            profile_id = load_terminal_profile(root).profile_id
        except (OSError, RuntimeError, ValueError, yaml.YAMLError):
            profile_id = os.environ.get("SOLUTION_AGENT_PROFILE_ID") or None
    return _cached_server_identity(str(root), profile_id)


def _with_server(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the exact source identity without letting diagnostics break a tool call."""

    try:
        server = _server_identity()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        server = {"identity_error": _sanitized_error(exc)}
    return {**payload, "server": server}


def _capsule_store() -> TaskCapsuleStore:
    return TaskCapsuleStore(_solution_root() / "state")


def _artifact_store() -> ArtifactStore:
    return ArtifactStore(_solution_root() / "state")


def _ensure_capsule(task_id: str, *, readable_paths: list[str] | None = None) -> TaskCapsule:
    store = _capsule_store()
    try:
        capsule = store.load(task_id)
        requested = [str(Path(item).resolve()) for item in (readable_paths or [])]
        missing = [item for item in requested if item not in capsule.scope.readable_paths]
        if not missing:
            return capsule

        def extend_scope(current: TaskCapsule) -> TaskCapsule:
            scope = current.scope.model_copy(
                update={"readable_paths": [*current.scope.readable_paths, *missing]}
            )
            return current.model_copy(update={"scope": scope})

        return store.compare_and_swap(task_id, capsule.version, extend_scope)
    except FileNotFoundError:
        root = _solution_root()
        identity = _server_identity()
        read_scope = [str(root), *(readable_paths or [])]
        capsule = TaskCapsule(
            task_id=task_id,
            objective="Integrated focus runtime operation; one active user-requested work unit at a time.",
            source_release_id=str(identity["release_id"]),
            approval_owner="user",
            scope=ScopeContract(readable_paths=read_scope, writable_paths=[str(root)]),
        )
        try:
            return store.create(capsule)
        except CapsuleConflict:
            return store.load(task_id)


def _wait_disposition(job_dir: Path, state: Any) -> dict[str, Any]:
    try:
        release_id = str(_server_identity()["release_id"])
    except Exception:
        release_id = "runtime-unbound"
    return wait_disposition(job_dir, state, release_id=release_id, host_resume_mode="manual")


def _focus_record_path(job_dir: Path) -> Path:
    return job_dir / "focus_runtime.json"


def _load_focus_record(job_dir: Path) -> dict[str, Any]:
    path = _focus_record_path(job_dir)
    if not path.is_file():
        return {"focus_runtime_integrated": False}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {"focus_runtime_integrated": False}


def _sync_capsule_wait(job_dir: Path, disposition: dict[str, Any]) -> int | None:
    focus = _load_focus_record(job_dir)
    if not focus.get("focus_runtime_integrated"):
        return None
    store = _capsule_store()
    capsule = store.load(str(focus["capsule_id"]))
    wait = WaitRef(
        wait_handle=str(disposition["wait_handle"]),
        run_id=str(disposition["run_id"]),
        attempt_id=str(disposition["attempt_id"]),
        state_version=int(disposition["state_version"]),
        resume_trigger=list(disposition["resume_trigger"]),
        manual_resume_required=bool(disposition["manual_resume_required"]),
    )
    if capsule.wait == wait:
        return capsule.version
    return store.set_wait(capsule.task_id, wait, expected_version=capsule.version).version


def _queue_capsule_events(job_dir: Path, wait_state: Any) -> int | None:
    focus = _load_focus_record(job_dir)
    if not focus.get("focus_runtime_integrated"):
        return None
    store = _capsule_store()
    task_id = str(focus["capsule_id"])
    capsule = store.load(task_id)
    material_kinds = {
        "COMPLETION_CANDIDATE",
        "EXECUTION_FAILURE_VERIFIED",
        "STALLED",
        "DECISION_REQUIRED",
        "BUDGET_EXCEEDED",
        "COLLECTION_VERIFIED",
        "COLLECTION_REJECTED",
    }
    material_events: list[tuple[str, str]] = []
    for path in (job_dir / "wait" / "events").glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("kind") in material_kinds and payload.get("event_id") in wait_state.applied_event_ids:
            material_events.append((str(payload.get("occurred_at") or ""), str(payload["event_id"])))
    for _, event_id in sorted(material_events):
        if event_id not in capsule.queued_events:
            capsule = store.queue_event(task_id, event_id, expected_version=capsule.version)
    return capsule.version


def _collect_capsule_artifacts(
    job_dir: Path, result: dict[str, Any], wait_state: Any
) -> tuple[int | None, list[dict[str, Any]]]:
    focus = _load_focus_record(job_dir)
    if not focus.get("focus_runtime_integrated"):
        return None, []
    capsules = _capsule_store()
    task_id = str(focus["capsule_id"])
    capsule = capsules.load(task_id)
    artifacts = _artifact_store()
    refs: list[ArtifactRef] = []
    for item in result.get("artifacts_found") or []:
        if not isinstance(item, str):
            continue
        raw = Path(item)
        candidates = [raw] if raw.is_absolute() else [_solution_root() / raw, job_dir / raw]
        path = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
        if path is None:
            continue
        manifest = artifacts.put_path(
            capsule,
            path,
            source_kind="stage",
            provenance={"job_id": job_dir.name, "collection_status": result.get("status")},
        )
        refs.append(ArtifactRef(artifact_id=manifest.artifact_id, sha256=manifest.sha256, role="stage_output"))

    def update(current: TaskCapsule) -> TaskCapsule:
        known = {item.artifact_id for item in current.working_artifacts}
        merged = [*current.working_artifacts, *(item for item in refs if item.artifact_id not in known)]
        lifecycle = "CLOSED" if wait_state.lifecycle_state == "CLOSED" else "REVIEW_REQUIRED"
        return current.model_copy(
            update={
                "working_artifacts": merged,
                "wait": None if lifecycle == "CLOSED" else current.wait,
                "next_material_event": None,
                "active_work_unit": None,
                "lifecycle_state": lifecycle,
            }
        )

    proposed = update(capsule)
    if proposed == capsule:
        version = capsule.version
    else:
        version = capsules.compare_and_swap(task_id, capsule.version, update).version
    return version, [item.model_dump(mode="json") for item in refs]


def _stage_error(operation: str, phase: str, exc: Exception, **details: Any) -> dict[str, Any]:
    return _with_server({
        "ok": False,
        "operation": operation,
        "error": {
            "phase": phase,
            "code": str(getattr(exc, "code", type(exc).__name__)),
            "message": _sanitized_error(exc),
            "recoverable": isinstance(exc, (OSError, RuntimeError, ValueError, yaml.YAMLError)),
        },
        **details,
    })


def _terminal_error(operation: str, terminal_id: str | None, exc: Exception) -> dict[str, Any]:
    profile_id = os.environ.get("SOLUTION_AGENT_PROFILE_ID") or None
    if isinstance(exc, TerminalError):
        return _with_server(exc.result(operation, profile_id, terminal_id))
    if isinstance(exc, CommandReceiptError):
        return _with_server(TerminalError(
            exc.code, str(exc), recoverable=False,
            next_action="Inspect the existing command evidence through maintenance; do not resubmit or infer completion.",
        ).result(operation, profile_id, terminal_id))
    if isinstance(exc, CapsuleConflict):
        return _with_server(
            TerminalError("task_capsule_conflict", str(exc)).result(operation, profile_id, terminal_id)
        )
    return _with_server(
        TerminalError("terminal_internal_error", _sanitized_error(exc)).result(operation, profile_id, terminal_id)
    )


class ClientUpgradeRequired(ValueError):
    code = "CLIENT_SCHEMA_UPGRADE_REQUIRED"


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def stage_execute(
    spec_path: str,
    task_capsule_id: str | None = None,
    work_unit_id: str | None = None,
    release_id: str | None = None,
) -> dict[str, Any]:
    """Launch one reviewed spec and atomically claim an idle Task Capsule work unit."""

    try:
        _require_activation()
    except ProtocolMismatch as exc:
        return _stage_error("stage_execute", "activation_gate", exc)
    required = {
        "task_capsule_id": task_capsule_id,
        "work_unit_id": work_unit_id,
        "release_id": release_id,
    }
    missing = [name for name, value in required.items() if not isinstance(value, str) or not value.strip()]
    if missing:
        return _stage_error(
            "stage_execute",
            "client_contract",
            ClientUpgradeRequired(
                "This stage_execute request uses the unsupported legacy contract; "
                "upgrade the client and provide task_capsule_id, work_unit_id, and release_id."
            ),
            missing_arguments=missing,
            required_arguments=list(required),
            full_output_fallback=False,
        )

    root = _solution_root()
    candidate = Path(spec_path)
    try:
        path = _inside(candidate if candidate.is_absolute() else root / candidate, root)
        if path.suffix.lower() not in {".yaml", ".yml"} or not path.is_file():
            raise ValueError("spec_path must name an existing YAML file under SOLUTION_AGENT_ROOT")
        spec_payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        capsule_id = task_capsule_id.strip()
        work_unit_id = work_unit_id.strip()
        release_id = release_id.strip()
        declared_capsule = str(spec_payload.get("capsule_id") or capsule_id).strip()
        declared_work_unit = str(spec_payload.get("work_unit_id") or work_unit_id).strip()
        if declared_capsule != capsule_id or declared_work_unit != work_unit_id:
            raise CapsuleConflict("execution spec identity does not match the requested Task Capsule/work unit")
        active_release_id = str(_server_identity()["release_id"])
        if release_id != active_release_id:
            raise CapsuleConflict("requested release_id does not match the active MCP runtime")
        capsules = _capsule_store()
        capsule = capsules.load(capsule_id)
        # load() validates the supported capsule contract. Source release is provenance.
        work_unit_claimed = False
        if capsule.active_work_unit is None:
            capsule = capsules.start_work_unit(
                capsule_id,
                work_unit_id,
                expected_version=capsule.version,
            )
            work_unit_claimed = True
        elif capsule.active_work_unit != work_unit_id:
            raise CapsuleConflict("requested work unit is not the capsule active work unit")
        capsule_version: int | None = capsule.version
        executions_dir = root / "executions"
        try:
            job_id = register_execution(path, executions_dir=executions_dir)
        except Exception as exc:
            if work_unit_claimed:
                try:
                    capsule = capsules.finish_work_unit(
                        capsule_id,
                        work_unit_id,
                        expected_version=capsule.version,
                    )
                    capsule_version = capsule.version
                except Exception:
                    # Preserve the primary validation error. A concurrent capsule
                    # change must not be overwritten by a compensating rollback.
                    pass
            return _stage_error(
                "stage_execute",
                "validate_and_register",
                exc,
                resolved_spec_path=str(path),
                profile_id=os.environ.get("SOLUTION_AGENT_PROFILE_ID") or None,
                work_unit_claimed=False,
                capsule_version=capsule_version,
            )
        job_dir = executions_dir / job_id
        atomic_write_json(
            _focus_record_path(job_dir),
            {
                "schema_version": 1,
                "focus_runtime_integrated": True,
                "capsule_id": capsule_id,
                "work_unit_id": work_unit_id,
                "release_id": release_id,
            },
        )
        try:
            state = run_execution(job_dir)
        except Exception as exc:
            return _stage_error(
                "stage_execute",
                "launch",
                exc,
                job_id=job_id,
                resolved_spec_path=str(path),
                profile_id=os.environ.get("SOLUTION_AGENT_PROFILE_ID") or None,
            )
        wait_state = synchronize_execution_state(job_dir, state)
        if wait_state.lifecycle_state == "WAITING_EXTERNAL":
            try:
                monitor = start_wait_monitor(job_dir)
            except (OSError, RuntimeError, ValueError) as exc:
                monitor = {
                    "started": False,
                    "pid": None,
                    "reason": "monitor_launch_failed",
                    "error": _sanitized_error(exc),
                }
        else:
            monitor = {"started": False, "pid": None, "reason": "material_state_already_reached"}
        disposition = _wait_disposition(job_dir, wait_state)
        capsule_version = _sync_capsule_wait(job_dir, disposition)
        queued_version = _queue_capsule_events(job_dir, wait_state)
        if queued_version is not None:
            capsule_version = queued_version
        response = {
            "ok": True,
            "operation": "stage_execute",
            "job_id": job_id,
            "state": _compact_state(state),
            "wait_disposition": disposition,
            "wait_monitor": monitor,
            "focus_runtime_integrated": True,
            "work_unit_claimed": work_unit_claimed,
            "capsule_version": capsule_version,
            "run_id": disposition["run_id"],
            "attempt_id": disposition["attempt_id"],
            "wait_handle": disposition["wait_handle"],
            "state_version": disposition["state_version"],
            "resume_trigger": disposition["resume_trigger"],
            "manual_resume_required": disposition["manual_resume_required"],
            "next_action": (
                "End this model turn. The deterministic monitor records material events; resume externally or manually."
                if disposition["suppress_model_continuation"]
                else "Call stage_collect."
            ),
        }
        _record_stage_usage(job_dir, phase="dispatch", result=response)
        return _with_server(response)
    except Exception as exc:
        return _stage_error(
            "stage_execute",
            "resolve_spec",
            exc,
            resolved_spec_path=None,
            profile_id=os.environ.get("SOLUTION_AGENT_PROFILE_ID") or None,
        )


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def stage_status(job_id: str, after_state_version: int | None = None) -> dict[str, Any]:
    """Return compact lifecycle state for one execution without returning raw logs."""

    try:
        job_dir = _job_dir(job_id)
        if not job_dir.is_dir():
            raise ValueError(f"unknown job_id: {job_id}")
        state = check_once(job_dir)
        wait_state = synchronize_execution_state(job_dir, state)
        disposition = _wait_disposition(job_dir, wait_state)
        capsule_version = _sync_capsule_wait(job_dir, disposition)
        queued_version = _queue_capsule_events(job_dir, wait_state)
        if queued_version is not None:
            capsule_version = queued_version
    except Exception as exc:
        return _stage_error("stage_status", "status", exc, job_id=job_id)
    terminal = state.get("status") in {
        "exited",
        "timeout",
        "killed",
        "lost_process",
        "failed_launch",
        "succeeded",
        "failed_exit_code",
        "artifact_missing",
        "metrics_missing",
        "metrics_invalid",
        "missing_evidence",
        "missing_capability",
        "invalid_model_execution_evidence",
    }
    state_version = int(disposition["state_version"])
    if after_state_version is not None and after_state_version == state_version:
        response = {
            "ok": True,
            "operation": "stage_status",
            "job_id": job_id,
            "unchanged": True,
            "state_version": state_version,
            "terminal": terminal,
            "focus_runtime_integrated": True,
            "capsule_version": capsule_version,
        }
        _record_stage_usage(job_dir, phase="wait", result=response)
        return _with_server(response)
    response = {
        "ok": True,
        "operation": "stage_status",
        "state": _compact_state(state),
        "terminal": terminal,
        "wait_disposition": disposition,
        "focus_runtime_integrated": bool(_load_focus_record(job_dir).get("focus_runtime_integrated")),
        "capsule_version": capsule_version,
        "unchanged": False,
        "state_version": state_version,
        "next_action": (
            "Call stage_collect."
            if terminal
            else "End this model turn. Do not poll in a model loop; resume only on a material event or manual check."
        ),
    }
    _record_stage_usage(job_dir, phase="wait", result=response)
    return _with_server(response)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def stage_collect(job_id: str) -> dict[str, Any]:
    """Collect a terminal execution into a compact result; keep full logs and artifacts on disk."""

    try:
        job_dir = _job_dir(job_id)
        if not job_dir.is_dir():
            raise ValueError(f"unknown job_id: {job_id}")
        final_path = job_dir / "final_result.yaml"
        if final_path.is_file():
            result = yaml.safe_load(final_path.read_text(encoding="utf-8")) or {}
            verify_collection_seal(job_dir, result)
            initialize_wait(job_dir, read_json(job_dir / "state.json"))
        else:
            state = check_once(job_dir)
            wait_state = synchronize_execution_state(job_dir, state)
            if state.get("status") not in {"exited", "timeout", "killed", "lost_process", "failed_launch"}:
                response = {
                    "ok": True,
                    "operation": "stage_collect",
                    "collected": False,
                    "state": _compact_state(state),
                    "wait_disposition": _wait_disposition(job_dir, wait_state),
                    "focus_runtime_integrated": bool(_load_focus_record(job_dir).get("focus_runtime_integrated")),
                    "next_action": "End this model turn. Do not poll in a model loop.",
                }
                _record_stage_usage(job_dir, phase="collect", result=response)
                return _with_server(response)
            result = collect(job_dir)
            verify_collection_seal(job_dir, result)
        wait_state = synchronize_collection_result(job_dir, result)
    except Exception as exc:
        return _stage_error("stage_collect", "collect", exc, job_id=job_id)
    compact = _compact_result(result)
    capsule_version, artifact_refs = _collect_capsule_artifacts(job_dir, result, wait_state)
    compact.update(
        ok=True,
        operation="stage_collect",
        collected=True,
        evidence_paths={
            "final_result": str(final_path),
            "collect_report": str(job_dir / "collect_report.yaml"),
            "stdout": str(job_dir / "stdout.log"),
            "stderr": str(job_dir / "stderr.log"),
        },
        next_action="Use the structured result for the gate; retrieve exact evidence only when the decision requires it.",
        wait_disposition=_wait_disposition(job_dir, wait_state),
        focus_runtime_integrated=bool(_load_focus_record(job_dir).get("focus_runtime_integrated")),
        capsule_version=capsule_version,
        artifact_refs=artifact_refs,
    )
    _record_token_guard_usage(job_dir, result)
    _record_stage_usage(job_dir, phase="collect", result=compact)
    return _with_server(compact)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def terminal_open(
    terminal_id: str | None = None,
    restart: bool = False,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Create once or attach to one profile-declared persistent terminal and acquire its writer lease."""

    try:
        _require_activation()
        return _with_server(_terminal_manager().open(terminal_id, restart=restart, task_id=task_id))
    except Exception as exc:
        return _terminal_error("terminal_open", terminal_id, exc)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
)
def terminal_exec(
    task_id: str,
    command: str | None = None,
    command_id: str | None = None,
    work_unit_id: str | None = None,
    access_mode: Literal["read_only", "writer"] = "writer",
    timeout_seconds: float | None = None,
    cancel: bool = False,
    wait_ms: int = 30000,
    max_preview_bytes: int = 4096,
    worker_task: str | None = None,
    worker_phase: Literal["prepare_code", "execute_run", "full"] = "prepare_code",
    finish_maintenance: bool = False,
    recover_worker: bool = False,
) -> dict[str, Any]:
    """Run/resume a supervised job. For remote Codex delegation pass worker_task Markdown,
    task_id, work_unit_id, access_mode=writer and timeout_seconds (no command).
    Defaults to prepare_code, not research execution. Resume/cancel with command_id only.
    Uses the existing transport; no terminal_open, YAML spec or research registry required.
    """

    writer_lease_id: str | None = None
    work_unit_claimed = False
    job_result_received = False
    executor_entered = False
    capsules: TaskCapsuleStore | None = None
    capsule: TaskCapsule | None = None
    packet = None
    try:
        _require_activation()
        manager = _terminal_manager()
        if recover_worker and worker_task is None:
            raise ValueError('recover_worker requires the original worker_task binding')
        if worker_task is not None:
            if command is not None or command_id is not None or cancel:
                raise ValueError("worker_task cannot be combined with command, command_id or cancel")
            if access_mode != "writer" or not work_unit_id or timeout_seconds is None:
                raise ValueError("worker_task requires writer access, work_unit_id and timeout_seconds")
            from solution_agent.harness.codex_worker import worker_packet, submission
            packet = worker_packet(_solution_root(), manager.profile.deployment_profile_name, task_id,
                                   work_unit_id, worker_task, worker_phase)
            if Path(packet["cwd"]).resolve() != manager.profile.slots[manager.profile.default_terminal].cwd.resolve():
                raise ValueError("worker workplace differs from supervised job cwd")
            command, command_id = (submission(packet, jobs_root=manager.base / 'jobs')
                                   if hasattr(manager, 'base') else submission(packet))
            if recover_worker and command_id is None:
                return _with_server({'ok': True, 'state': 'submission_uncertain',
                                     'task_id': task_id, 'work_unit_id': work_unit_id,
                                     'command_id': None, 'lookup_only': True,
                                     'reason': 'original worker command not positively identified; no launch attempted'})
        if command is not None:
            capsules = _capsule_store()
            capsule = _ensure_capsule(task_id)
            # A compatible persisted task survives runtime patch releases.
            if access_mode == "writer":
                if not isinstance(work_unit_id, str) or not work_unit_id.strip():
                    raise CapsuleConflict("writer terminal_exec requires work_unit_id")
                work_unit_id = work_unit_id.strip()
                if capsule.active_work_unit is None:
                    capsule = capsules.start_work_unit(
                        task_id,
                        work_unit_id,
                        expected_version=capsule.version,
                    )
                    work_unit_claimed = True
                elif capsule.active_work_unit != work_unit_id:
                    raise CapsuleConflict("requested work unit is not the capsule active work unit")
                writer_lease_id = f"terminal-job-{uuid.uuid4().hex}"
                lifetime = max(300.0, (timeout_seconds or 3600.0) + 60.0)
                lease = WriterLeaseRef(
                    lease_id=writer_lease_id,
                    target=str(manager.profile.slots[manager.profile.default_terminal].cwd),
                    owner_work_unit=work_unit_id,
                    expires_at=datetime.now(timezone.utc) + timedelta(seconds=lifetime),
                )
                try:
                    capsule = capsules.acquire_writer_lease(
                        task_id,
                        lease,
                        expected_version=capsule.version,
                    )
                except Exception:
                    if work_unit_claimed:
                        try:
                            capsules.finish_work_unit(
                                task_id,
                                work_unit_id,
                                expected_version=capsule.version,
                            )
                        except Exception:
                            pass
                    raise
            elif work_unit_id and capsule.active_work_unit not in {None, work_unit_id}:
                raise CapsuleConflict("requested work unit is not the capsule active work unit")
        executor_entered = True
        result = manager.exec(
            None,
            command=command,
            command_id=command_id,
            wait_ms=wait_ms,
            max_preview_bytes=max_preview_bytes,
            task_id=task_id,
            work_unit_id=work_unit_id,
            timeout_seconds=timeout_seconds,
            cancel=cancel,
            access_mode=access_mode,
            writer_lease_id=writer_lease_id,
            runtime_release_id=str(_server_identity()["release_id"]) if command is not None else None,
            **({'worker_packet': packet} if packet is not None and command is not None else {}),
        )
        job_result_received = True
        bound_task = result.get("task_id")
        if bound_task is not None and bound_task != task_id:
            raise TerminalError(
                "job_scope_conflict",
                f"command {result.get('command_id')} belongs to a different Task Capsule",
                recoverable=False,
            )
        if result.get("state") in {"succeeded", "failed", "cancelled", "lost"}:
            command_dir = Path(str(result["command_dir"]))
            task = _ensure_capsule(task_id, readable_paths=[str(command_dir)])
            artifacts = _artifact_store()
            manifests = {
                name: artifacts.put_path(
                    task,
                    Path(str(result[name]["path"])),
                    source_kind="terminal",
                    provenance={"command_id": result["command_id"], "stream": name},
                )
                for name in ("stdout", "stderr")
            }
            worker_link = command_dir / "worker.json"
            if worker_link.is_file():
                worker = read_json(worker_link)
                if worker.get("task_id") != task_id:
                    raise ValueError("worker result task mismatch")
                control = _inside(Path(worker["control"]), manager.profile.run_root)
                output = _inside(Path(worker["output"]), manager.profile.slots[manager.profile.default_terminal].cwd)
                task = _ensure_capsule(task_id, readable_paths=[str(control), str(output)])
                receipt_path = control / "receipt.json"
                result["worker"] = read_json(receipt_path) if receipt_path.is_file() else {
                    "contract_valid": False, "task_status": "unverified",
                    "reason": "worker interrupted or failed before result receipt; do not relaunch"}
                for name, file in (("worker_receipt", receipt_path),
                                   ("worker_result", output / "result.yaml"),
                                   ("worker_notes", output / "worker_notes.md")):
                    if file.is_file():
                        _inside(file, control if name == "worker_receipt" else output)
                        manifests[name] = artifacts.put_path(task, file, source_kind="terminal",
                                                           provenance={"command_id": result["command_id"]})
            for name, manifest in manifests.items():
                result.setdefault(name, {})["artifact_id"] = manifest.artifact_id
            raw_transcript = result.get("raw_transcript")
            if isinstance(raw_transcript, dict) and raw_transcript.get("path"):
                raw_manifest = artifacts.put_path(
                    task,
                    Path(str(raw_transcript["path"])),
                    source_kind="terminal",
                    provenance={"command_id": result["command_id"], "stream": "raw_transcript"},
                )
                raw_transcript["artifact_id"] = raw_manifest.artifact_id
                result["raw_transcript_ref"] = raw_manifest.artifact_id
                manifests["raw_transcript"] = raw_manifest
            capsules = _capsule_store()
            current = capsules.load(task_id)
            completed_lease_id = result.get("writer_lease_id")
            if isinstance(completed_lease_id, str) and any(
                item.lease_id == completed_lease_id for item in current.writer_leases
            ):
                current = capsules.release_writer_lease(
                    task_id,
                    completed_lease_id,
                    expected_version=current.version,
                )
                result["writer_lease_released"] = True
            else:
                release_receipt = result.get("writer_lease_release")
                result["writer_lease_released"] = bool(
                    isinstance(release_receipt, dict) and release_receipt.get("released")
                )
            for name, manifest in manifests.items():
                if not any(
                    item.artifact_id == manifest.artifact_id and item.role == f"terminal_{name}"
                    for item in current.working_artifacts
                ):
                    current = capsules.attach_artifact(
                        task_id,
                        ArtifactRef(
                            artifact_id=manifest.artifact_id,
                            sha256=manifest.sha256,
                            role=f"terminal_{name}",
                        ),
                        authoritative=False,
                        expected_version=current.version,
                    )
            event_id = result.get("event_id")
            if isinstance(event_id, str) and event_id and event_id not in current.queued_events:
                current = capsules.queue_event(task_id, event_id, expected_version=current.version)
            result["artifacts"] = {name: manifest.artifact_id for name, manifest in manifests.items()}
            result["artifact_task_id"] = task_id
            if worker_link.is_file() and current.active_work_unit == worker["work_unit_id"] and not current.writer_leases:
                current = capsules.finish_work_unit(task_id, worker["work_unit_id"], expected_version=current.version)
            elif finish_maintenance and not worker_link.is_file():
                from solution_agent.harness.maintenance_completion import finish_owned_maintenance
                result["maintenance"] = finish_owned_maintenance(
                    capsules, result, task_id=task_id, work_unit_id=work_unit_id)
                current = capsules.load(task_id)
            result["capsule_version"] = current.version
        return _with_server(result)
    except Exception as exc:
        # An exception after entering the runner may follow a real launch.
        # Keep its lease/claim for same-command recovery, never infer absence.
        if not executor_entered and command is not None and writer_lease_id and capsules is not None:
            try:
                current = capsules.load(task_id)
                if any(item.lease_id == writer_lease_id for item in current.writer_leases):
                    current = capsules.release_writer_lease(
                        task_id,
                        writer_lease_id,
                        expected_version=current.version,
                    )
                if work_unit_claimed and not job_result_received and current.active_work_unit == work_unit_id:
                    capsules.finish_work_unit(
                        task_id,
                        str(work_unit_id),
                        expected_version=current.version,
                    )
            except Exception:
                pass
        failure = _terminal_error("terminal_exec", None, exc)
        if executor_entered and job_result_received and result.get("command_id"):
            failure.update(task_id=task_id, work_unit_id=work_unit_id, command_id=result["command_id"])
        if worker_task is not None:
            failure.update(task_id=task_id, work_unit_id=work_unit_id)
            failure["submission"] = {
                "schema": "harness-submission/v1",
                "outcome": "unknown" if executor_entered or recover_worker else "rejected",
                "phase": "recovery_lookup" if recover_worker else "executor_entered" if executor_entered else "before_executor",
                "worker_task_sha256": hashlib.sha256(worker_task.encode("utf-8")).hexdigest(),
                "worker_phase": worker_phase, "timeout_seconds": timeout_seconds,
            }
            if executor_entered and command_id:
                failure["command_id"] = command_id
        return failure


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True),
)
def terminal_send(
    chars: str | None = None,
    control: str | None = None,
    terminal_id: str | None = None,
    after_cursor: int | None = None,
    wait_ms: int = 1000,
    max_output_bytes: int = 8192,
) -> dict[str, Any]:
    """Send bounded input to one attached interactive child terminal and return bounded incremental output."""

    try:
        _require_activation()
        return _with_server(_terminal_manager().send(
            terminal_id,
            chars=chars,
            control=control,
            after_cursor=after_cursor,
            wait_ms=wait_ms,
            max_output_bytes=max_output_bytes,
        ))
    except Exception as exc:
        return _terminal_error("terminal_send", terminal_id, exc)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
)
def terminal_read(
    terminal_id: str | None = None,
    after_cursor: int = 0,
    max_output_bytes: int = 8192,
) -> dict[str, Any]:
    """Read a bounded sanitized preview from one profile-declared persistent terminal."""

    try:
        return _with_server(_terminal_manager().read(
            terminal_id,
            after_cursor=after_cursor,
            max_output_bytes=max_output_bytes,
        ))
    except Exception as exc:
        return _terminal_error("terminal_read", terminal_id, exc)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
)
def terminal_close(terminal_id: str | None = None, action: str = "detach") -> dict[str, Any]:
    """Release this client or explicitly terminate one managed interactive child terminal."""

    try:
        _require_activation()
        return _with_server(_terminal_manager().close(terminal_id, action=action))
    except Exception as exc:
        return _terminal_error("terminal_close", terminal_id, exc)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def artifact_inspect(
    task_id: str,
    artifact_id: str,
    mode: str = "describe",
    selector: dict[str, Any] | None = None,
    other_artifact_id: str | None = None,
    replay_content: bool = False,
    max_output_bytes: int = 8192,
) -> dict[str, Any]:
    """Describe, selectively read, or compare immutable task-scoped artifacts."""

    try:
        task = _ensure_capsule(task_id)
        artifacts = _artifact_store()
        if mode == "describe":
            result = artifacts.describe(task, artifact_id)
        elif mode == "read":
            if selector is None:
                raise ValueError("artifact_inspect read mode requires selector")
            result = artifacts.read(
                task,
                artifact_id,
                Selector.model_validate(selector),
                replay_content=replay_content,
                max_output_bytes=max_output_bytes,
            )
            receipt_id = result.get("read_receipt_id")
            if receipt_id and receipt_id not in task.read_receipt_ids:
                task = _capsule_store().attach_read_receipt(
                    task_id,
                    str(receipt_id),
                    expected_version=task.version,
                )
        elif mode == "diff":
            if not other_artifact_id:
                raise ValueError("artifact_inspect diff mode requires other_artifact_id")
            with artifacts.open_content(task, artifact_id) as left_handle:
                left = left_handle.read().decode("utf-8", errors="strict")
            with artifacts.open_content(task, other_artifact_id) as right_handle:
                right = right_handle.read().decode("utf-8", errors="strict")
            diff = "".join(
                difflib.unified_diff(
                    left.splitlines(keepends=True),
                    right.splitlines(keepends=True),
                    fromfile=artifact_id,
                    tofile=other_artifact_id,
                )
            )
            encoded = diff.encode("utf-8")
            result = {
                "content": encoded[:max_output_bytes].decode("utf-8", errors="ignore"),
                "returned_bytes": min(len(encoded), max_output_bytes),
                "truncated": len(encoded) > max_output_bytes,
            }
        else:
            raise ValueError("artifact_inspect mode must be describe, read, or diff")
        return _with_server(
            {
                "ok": True,
                "operation": "artifact_inspect",
                "mode": mode,
                "capsule_version": task.version,
                **result,
            }
        )
    except Exception as exc:
        return _stage_error("artifact_inspect", mode, exc, task_id=task_id, artifact_id=artifact_id)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def artifact_store(
    task_id: str,
    path: str,
    alias: str | None = None,
    expected_alias_version: int | None = None,
) -> dict[str, Any]:
    """Store one in-scope local file by content hash and return only its compact manifest."""

    try:
        root = _solution_root()
        candidate = _inside(Path(path) if Path(path).is_absolute() else root / path, root)
        task = _ensure_capsule(task_id)
        manifest = _artifact_store().put_path(
            task,
            candidate,
            alias=alias,
            expected_alias_version=expected_alias_version,
        )
        capsules = _capsule_store()
        if not any(item.artifact_id == manifest.artifact_id and item.role == "authoritative_input" for item in task.authoritative_artifacts):
            task = capsules.attach_artifact(
                task_id,
                ArtifactRef(
                    artifact_id=manifest.artifact_id,
                    sha256=manifest.sha256,
                    role="authoritative_input",
                ),
                authoritative=True,
                expected_version=task.version,
            )
        return _with_server(
            {
                "ok": True,
                "operation": "artifact_store",
                "manifest": manifest.model_dump(mode="json"),
                "alias": alias,
                "capsule_version": task.version,
            }
        )
    except Exception as exc:
        return _stage_error("artifact_store", "store", exc, task_id=task_id, path=path)


@mcp.tool(
    structured_output=True,
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
)
def artifact_apply(
    task_id: str,
    mode: str,
    request: dict[str, Any] | None = None,
    plan_id: str | None = None,
    writer_lease_id: str | None = None,
    commit: bool = False,
) -> dict[str, Any]:
    """Plan or atomically commit materialize, compose, or single-file patch operations."""

    try:
        _require_activation()
        capsules = _capsule_store()
        task = _ensure_capsule(task_id)
        engine = ArtifactApplyEngine(_solution_root() / "state", _artifact_store(), capsules)
        if commit:
            if not plan_id or not writer_lease_id:
                raise ValueError("commit requires plan_id and writer_lease_id")
            receipt = engine.commit(task, plan_id, writer_lease_id)
            return _with_server(
                {"ok": True, "operation": "artifact_apply", "committed": True, "receipt": receipt.model_dump(mode="json")}
            )
        if request is None:
            raise ValueError("plan request is required when commit=false")
        if mode not in {"materialize", "compose", "patch"}:
            raise ValueError("artifact_apply mode must be materialize, compose, or patch")
        if "expected_target_sha256" not in request:
            raise ValueError("artifact_apply requires explicit expected_target_sha256 (null for a new target)")
        target = str(request.get("target") or "")
        if not target:
            raise ValueError("artifact_apply requires target")
        resolved = _inside(Path(target) if Path(target).is_absolute() else _solution_root() / target, _solution_root())
        before = request.get("expected_target_sha256")
        lease_seed = f"{task_id}\0{resolved}\0{before}".encode()
        lease_id = f"lease-{hashlib.sha256(lease_seed).hexdigest()[:24]}"
        existing = next((item for item in task.writer_leases if item.lease_id == lease_id), None)
        acquired = False
        if existing is None:
            task = capsules.acquire_writer_lease(
                task_id,
                WriterLeaseRef(
                    lease_id=lease_id,
                    target=str(resolved),
                    base_sha256=before,
                    owner_work_unit=task.active_work_unit or "artifact_apply",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                ),
                expected_version=task.version,
            )
            acquired = True
        try:
            if mode == "materialize":
                plan = engine.plan_materialize(task, str(request.get("artifact_id") or ""), str(resolved), before)
            elif mode == "compose":
                plan = engine.plan_compose(task, request.get("recipe") or {}, str(resolved), before)
            else:
                plan = engine.plan_patch(task, str(request.get("patch_artifact_id") or ""), str(resolved), before)
        except Exception:
            if acquired:
                latest = capsules.load(task_id)
                if any(item.lease_id == lease_id for item in latest.writer_leases):
                    capsules.release_writer_lease(task_id, lease_id, expected_version=latest.version)
            raise
        return _with_server(
            {
                "ok": True,
                "operation": "artifact_apply",
                "committed": False,
                "plan": plan.model_dump(mode="json"),
                "writer_lease_id": lease_id,
                "next_action": "Review the diff artifact, then call artifact_apply with commit=true and the same plan/lease IDs.",
            }
        )
    except Exception as exc:
        return _stage_error("artifact_apply", "commit" if commit else "plan", exc, task_id=task_id, mode=mode)


def main() -> None:
    if os.environ.get("SOLUTION_AGENT_PROTOCOL_REQUIRED") == "1":
        import base64
        from solution_agent.harness.release_identity import build_runtime_identity
        encoded = os.environ.get("SOLUTION_AGENT_EXPECTED_IDENTITY_B64")
        if not encoded:
            raise ProtocolMismatch("managed launch requires the client's expected release identity")
        local = json.loads(base64.b64decode(encoded, validate=True))
        remote = build_runtime_identity(_solution_root()).compact()
        remote["protocol_version"] = PROTOCOL_VERSION
        activate_protocol(local, remote, int(os.environ.get("SOLUTION_AGENT_ACTIVATION_GENERATION", "1")))
    verify_workplace_identity(load_terminal_profile(_solution_root()))
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
