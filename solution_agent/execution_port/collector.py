"""collector.py — evaluate job outcome, write final_result.yaml and _worker_out/result.yaml."""
from __future__ import annotations

import json
import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .io_utils import atomic_write_json, read_json
from .script_registry import get_closure_contract, get_expected_metrics, get_warning_rules, load_registry
from .spec import _parse
from ..failure_taxonomy import failure_contract, recommended_action_for_failure
from ..model_execution_evidence import (
    extract_model_execution_evidence,
    normalize_model_execution_evidence,
    proxy_fallback_warning,
    validate_model_execution_evidence,
)
from ..harness.receipt_adapter import verify_token_guard_receipt

# States that are ready for collection
_PRE_COLLECT = {"exited", "timeout", "killed", "lost_process", "failed_launch"}

# States whose outcome is already determined by the cause (not by artifacts)
_TERMINAL_AS_IS = {"timeout", "killed", "lost_process", "failed_launch"}


def to_worker_status(final_status: str) -> str:
    if final_status == "succeeded":
        return "success"
    if final_status in {
        "artifact_missing",
        "metrics_missing",
        "metrics_invalid",
        "missing_evidence",
        "missing_capability",
        "invalid_model_execution_evidence",
    }:
        return "partial"
    return "failed"


def _check_artifacts(
    required: list[str], workspace_root: str
) -> tuple[list[str], list[str]]:
    """Return (found, missing) for required artifact paths."""
    found, missing = [], []
    for rel in required:
        raw = Path(rel)
        p = raw if raw.is_absolute() else Path(workspace_root) / raw
        (found if p.exists() else missing).append(rel)
    return found, missing


def _artifact_path(reference: str, workspace_root: str) -> Path:
    raw = Path(reference)
    return raw if raw.is_absolute() else Path(workspace_root) / raw


def _artifact_manifest(found: list[str], workspace_root: str) -> list[dict]:
    manifest: list[dict] = []
    for reference in found:
        path = _artifact_path(reference, workspace_root)
        if path.is_file():
            raw = path.read_bytes()
            manifest.append(
                {
                    "path": reference,
                    "kind": "file",
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        elif path.is_dir():
            manifest.append({"path": reference, "kind": "directory", "bytes": None, "sha256": None})
    return manifest


def verify_collection_seal(job_dir: Path, result: dict) -> None:
    """Re-verify immutable evidence before returning a cached collection result."""
    spec_data = yaml.safe_load((job_dir / "execution_spec.yaml").read_text(encoding="utf-8"))
    spec = _parse(spec_data)
    manifest = result.get("artifact_manifest")
    if not isinstance(manifest, list):
        raise ValueError("collected result has no artifact manifest; recollect with the current harness")
    for item in manifest:
        if not isinstance(item, dict) or not item.get("path"):
            raise ValueError("collected result contains an invalid artifact manifest entry")
        path = _artifact_path(str(item["path"]), spec.workspace.root)
        if item.get("kind") == "directory":
            if not path.is_dir():
                raise ValueError(f"collected artifact changed or disappeared: {item['path']}")
            continue
        if not path.is_file():
            raise ValueError(f"collected artifact changed or disappeared: {item['path']}")
        raw = path.read_bytes()
        if len(raw) != item.get("bytes") or hashlib.sha256(raw).hexdigest() != item.get("sha256"):
            raise ValueError(f"collected artifact hash mismatch: {item['path']}")

    if spec.evidence.token_guard_receipt_required:
        receipt_path = Path(spec.evidence.token_guard_receipt)
        if not receipt_path.is_absolute():
            receipt_path = Path(spec.workspace.root) / receipt_path
        verified = verify_token_guard_receipt(receipt_path, root=Path(spec.workspace.root))
        if verified.exit_code != result.get("exit_code") or verified.timed_out:
            raise ValueError("token guard receipt no longer matches the collected execution")


def _closure_required_artifacts(registry: dict, script_id: str, output_dir: str) -> list[str]:
    contract = get_closure_contract(registry, script_id)
    if contract.get("enabled") is False:
        return []
    files = contract.get("required_files")
    if not isinstance(files, list) or not files:
        files = contract.get("default_required_files")
    if not isinstance(files, list) or not output_dir:
        return []
    base = Path(str(output_dir))
    return [str(base / str(item).strip()) for item in files if str(item).strip()]


def _check_metrics(
    metrics_file: str,
    metric_keys: list[str],
    workspace_root: str,
    numeric_keys: list[str] | None = None,
    finite_required: bool = False,
) -> tuple[dict, list[str], list[str]]:
    """Return (metrics_found, missing_keys, invalid_keys)."""
    if not metric_keys:
        return {}, [], []
    raw_metrics = Path(metrics_file) if metrics_file else None
    path = (raw_metrics if raw_metrics and raw_metrics.is_absolute() else Path(workspace_root) / raw_metrics) if raw_metrics else None
    if path is None or not path.exists():
        return {}, list(metric_keys), []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}, list(metric_keys), []
    found = {}
    missing = []
    invalid = []
    numeric_set = set(numeric_keys or [])
    evidence_keys = {
        "failure_taxonomy",
        "contract_status",
        "science_status",
        "model_execution_evidence",
        "requested_model",
        "executed_model",
        "fallback_reason",
        "proxy_only",
        "formal_evidence",
        "allowed_claims",
        "forbidden_claims",
        "evidence_scope",
    }
    for key in metric_keys:
        if key not in data:
            missing.append(key)
            continue
        value = data[key]
        if value is None or value == "":
            missing.append(key)
            continue
        if key in numeric_set:
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                invalid.append(key)
                continue
            if finite_required and not math.isfinite(numeric_value):
                invalid.append(key)
                continue
        found[key] = value
    for key in evidence_keys:
        value = data.get(key)
        if value is not None and value != "":
            found.setdefault(key, value)
    return found, missing, invalid


def _judge_for_status(
    final_status: str,
    metrics_summary: dict,
    warning_rules: dict | None = None,
    model_execution_evidence: dict | None = None,
) -> dict:
    status_failure_map = {
        "failed_exit_code": "execution_failed_exit_code",
        "artifact_missing": "artifact_missing",
        "metrics_missing": "metrics_missing",
        "metrics_invalid": "metrics_invalid",
        "missing_evidence": "missing_evidence",
        "missing_capability": "missing_capability",
        "invalid_model_execution_evidence": "invalid_model_execution_evidence",
    }
    failure_type = None
    warning_type = None
    outcome = "completed" if final_status == "succeeded" else "failed"
    execution_success = final_status == "succeeded"
    if final_status in status_failure_map:
        failure_type = status_failure_map[final_status]
        contract = failure_contract(failure_type)
        outcome = "partial"
        execution_success = bool(contract.get("execution_success", False))

    rules = warning_rules if isinstance(warning_rules, dict) else {}
    weak_rule = rules.get("weak_metric_result", {})
    recommended = {"type": "no_retry", "message": "Execution completed."}
    if final_status == "succeeded" and isinstance(weak_rule, dict):
        key = str(weak_rule.get("key") or "")
        if key in metrics_summary:
            try:
                value = float(metrics_summary[key])
                lt = weak_rule.get("lt")
                lte = weak_rule.get("lte")
                is_weak = (lt is not None and value < float(lt)) or (lte is not None and value <= float(lte))
            except (TypeError, ValueError):
                is_weak = False
            if is_weak:
                outcome = "completed_with_warning"
                warning_type = "weak_metric_result"
                weak_contract = failure_contract("weak_metric_result")
                rec = weak_rule.get("recommended_action", {})
                recommended = rec if isinstance(rec, dict) else recommended_action_for_failure("weak_metric_result")
                execution_success = bool(weak_contract.get("execution_success", True))
    if final_status == "succeeded" and proxy_fallback_warning(model_execution_evidence):
        outcome = "completed_with_warning"
        warning_type = "proxy_model_fallback"
        contract = failure_contract("proxy_model_fallback")
        recommended = recommended_action_for_failure("proxy_model_fallback")
        execution_success = bool(contract.get("execution_success", True))
    if failure_type:
        recommended = recommended_action_for_failure(
            failure_type,
            message=f"Execution evidence is incomplete: {failure_type}.",
        )
    contract = failure_contract(failure_type or warning_type)
    return {
        "outcome": outcome,
        "warning_type": warning_type,
        "failure_type": failure_type,
        "routing_type": failure_type or warning_type,
        "repairable": bool(contract.get("repairable", False)),
        "user_action_required": bool(contract.get("user_action_required", False)),
        "codex_recommended": bool(contract.get("codex_recommended", False)),
        "warn_only": bool(contract.get("warn_only", False)),
        "execution_success": execution_success,
        "recommended_action": recommended,
    }


def _status_from_metrics_contract(metrics_summary: dict) -> Optional[str]:
    """Promote script-emitted evidence contract failures into judge status.

    This keeps execution success separate from task/evidence success: a script may
    exit 0 and produce every required file while still reporting that the evidence
    is insufficient to prove the experiment goal.
    """
    if not isinstance(metrics_summary, dict):
        return None
    taxonomy = str(metrics_summary.get("failure_taxonomy") or "").strip().lower()
    if taxonomy == "missing_evidence":
        return "missing_evidence"
    if taxonomy == "missing_capability":
        return "missing_capability"

    contract_status = str(metrics_summary.get("contract_status") or "").strip().lower()
    science_status = str(metrics_summary.get("science_status") or "").strip().lower()
    incomplete_contract = contract_status in {"incomplete", "not_proven", "failed", "fail"}
    unproven_science = science_status in {"not_proven", "incomplete", "failed", "fail"}
    if incomplete_contract or unproven_science:
        return "missing_evidence"
    return None


def _model_execution_evidence_from_metrics(metrics_summary: dict) -> tuple[dict | None, list[str]]:
    evidence = extract_model_execution_evidence(metrics_summary)
    if evidence is None:
        return None, []
    normalized = normalize_model_execution_evidence(evidence)
    return normalized, validate_model_execution_evidence(evidence)


def collect(job_dir: Path) -> dict:
    """
    Evaluate job outcome and write result files.

    Requires state.json.status in {exited, timeout, killed, lost_process, failed_launch}.
    Writes: collect_report.yaml, final_result.yaml, _worker_out/result.yaml.
    Updates state.json: → collecting → <final_status>.

    Only this function writes _worker_out/result.yaml.
    """
    state_path = job_dir / "state.json"
    state = read_json(state_path)
    status = state.get("status")

    if status not in _PRE_COLLECT:
        raise RuntimeError(
            f"job {job_dir.name!r} is not collectable "
            f"(status={status!r}, need one of {sorted(_PRE_COLLECT)})"
        )

    state["status"] = "collecting"
    atomic_write_json(state_path, state)

    spec_data = yaml.safe_load(
        (job_dir / "execution_spec.yaml").read_text(encoding="utf-8")
    )
    spec = _parse(spec_data)

    raw_exit = state.get("exit_code")
    exit_code: int = raw_exit if raw_exit is not None else -1
    root = spec.workspace.root
    now = datetime.now(timezone.utc).isoformat()

    # ── Determine final status ────────────────────────────────────────────────
    artifacts_found: list[str] = []
    artifacts_missing: list[str] = []
    metrics_summary: dict = {}
    metrics_missing: list[str] = []
    metrics_invalid: list[str] = []
    model_execution_evidence: dict | None = None
    model_execution_evidence_errors: list[str] = []
    token_guard_receipt: dict | None = None
    token_guard_receipt_errors: list[str] = []
    artifact_manifest: list[dict] = []
    registry = load_registry(job_dir / "script_registry.yaml") if (job_dir / "script_registry.yaml").exists() else {}
    expected_metrics = get_expected_metrics(registry, spec.command.script_id)
    warning_rules = get_warning_rules(registry, spec.command.script_id)
    metric_keys = list(spec.success_criteria.metric_keys)
    metrics_file = spec.success_criteria.metrics_file
    if expected_metrics:
        if not metric_keys:
            metric_keys = [str(item) for item in expected_metrics.get("required_keys", []) if str(item).strip()]
        if not metrics_file:
            metrics_file = str(expected_metrics.get("file") or "")
            output_dir = str(spec.command.args.get("output_dir") or "")
            if metrics_file and output_dir:
                metrics_file = str(Path(output_dir) / metrics_file)
    numeric_keys = [str(item) for item in expected_metrics.get("numeric_keys", [])] if isinstance(expected_metrics, dict) else []
    finite_required = bool(expected_metrics.get("finite_required", False)) if isinstance(expected_metrics, dict) else False

    if status in _TERMINAL_AS_IS:
        # timeout / killed / lost_process / failed_launch — outcome is the cause
        final_status = status
    elif exit_code != spec.success_criteria.exit_code:
        final_status = "failed_exit_code"
    else:
        required_artifacts = list(spec.artifacts.required)
        for item in _closure_required_artifacts(registry, spec.command.script_id, str(spec.command.args.get("output_dir") or "")):
            if item not in required_artifacts:
                required_artifacts.append(item)
        artifacts_found, artifacts_missing = _check_artifacts(required_artifacts, root)
        artifact_manifest = _artifact_manifest(artifacts_found, root)
        if artifacts_missing:
            final_status = "artifact_missing"
        else:
            metrics_summary, metrics_missing, metrics_invalid = _check_metrics(
                metrics_file,
                metric_keys,
                root,
                numeric_keys=numeric_keys,
                finite_required=finite_required,
            )
            if metrics_missing:
                final_status = "metrics_missing"
            elif metrics_invalid:
                final_status = "metrics_invalid"
            else:
                model_execution_evidence, model_execution_evidence_errors = _model_execution_evidence_from_metrics(metrics_summary)
                if model_execution_evidence_errors:
                    final_status = "invalid_model_execution_evidence"
                else:
                    final_status = _status_from_metrics_contract(metrics_summary) or "succeeded"
                if final_status == "succeeded" and spec.evidence.token_guard_receipt_required:
                    receipt_ref = spec.evidence.token_guard_receipt
                    receipt_path = Path(receipt_ref)
                    if not receipt_path.is_absolute():
                        receipt_path = Path(root) / receipt_path
                    try:
                        verified = verify_token_guard_receipt(receipt_path, root=Path(root))
                        if verified.exit_code != exit_code:
                            raise ValueError(
                                f"receipt exit_code {verified.exit_code!r} does not match execution exit_code {exit_code!r}"
                            )
                        if verified.timed_out:
                            raise ValueError("receipt reports timed_out=true for a succeeded execution")
                        token_guard_receipt = verified.model_dump(mode="json")
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        token_guard_receipt_errors.append(str(exc))
                        final_status = "missing_evidence"
    judge = _judge_for_status(final_status, metrics_summary, warning_rules, model_execution_evidence)

    # ── Write collect_report.yaml (full detail) ───────────────────────────────
    report = {
        "job_id": state.get("job_id"),
        "task_id": spec.task_id,
        "final_status": final_status,
        "collected_at": now,
        "exit_code": exit_code,
        "artifacts_found": artifacts_found,
        "artifacts_missing": artifacts_missing,
        "artifact_manifest": artifact_manifest,
        "metrics_summary": metrics_summary,
        "metrics_missing": metrics_missing,
        "metrics_invalid": metrics_invalid,
        "model_execution_evidence": model_execution_evidence,
        "model_execution_evidence_errors": model_execution_evidence_errors,
        "token_guard_receipt": token_guard_receipt,
        "token_guard_receipt_errors": token_guard_receipt_errors,
        "judge": judge,
        "failure_type": judge.get("failure_type"),
        "warning_type": judge.get("warning_type"),
        "recommended_action": judge.get("recommended_action"),
        "pre_collect_status": status,
    }
    (job_dir / "collect_report.yaml").write_text(
        yaml.safe_dump(report, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

    # ── Write final_result.yaml ───────────────────────────────────────────────
    final = {
        "task_id": spec.task_id,
        "job_id": state.get("job_id"),
        "status": final_status,
        "exit_code": exit_code,
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "collected_at": now,
        "artifacts_found": artifacts_found,
        "artifacts_missing": artifacts_missing,
        "artifact_manifest": artifact_manifest,
        "metrics_summary": metrics_summary,
        "metrics_missing": metrics_missing,
        "metrics_invalid": metrics_invalid,
        "model_execution_evidence": model_execution_evidence,
        "model_execution_evidence_errors": model_execution_evidence_errors,
        "token_guard_receipt": token_guard_receipt,
        "token_guard_receipt_errors": token_guard_receipt_errors,
        "judge": judge,
        "failure_type": judge.get("failure_type"),
        "warning_type": judge.get("warning_type"),
        "recommended_action": judge.get("recommended_action"),
    }
    (job_dir / "final_result.yaml").write_text(
        yaml.safe_dump(final, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

    # ── Write _worker_out/result.yaml (collect-compatible, collector-only) ────
    worker_out = job_dir / "_worker_out"
    worker_out.mkdir(exist_ok=True)
    worker_result = {
        "schema_version": 1,
        "task_id": spec.task_id,
        "status": to_worker_status(final_status),
        "execution_status": final_status,
        "worker": "execution_port",
        "job_id": state.get("job_id"),
        "exit_code": exit_code,
        "artifacts_found": artifacts_found,
        "missing_artifacts": artifacts_missing,
        "artifact_manifest": artifact_manifest,
        "metrics_summary": metrics_summary,
        "metrics_missing": metrics_missing,
        "metrics_invalid": metrics_invalid,
        "model_execution_evidence": model_execution_evidence,
        "model_execution_evidence_errors": model_execution_evidence_errors,
        "token_guard_receipt": token_guard_receipt,
        "token_guard_receipt_errors": token_guard_receipt_errors,
        "judge": judge,
        "failure_type": judge.get("failure_type"),
        "warning_type": judge.get("warning_type"),
        "recommended_action": judge.get("recommended_action"),
    }
    (worker_out / "result.yaml").write_text(
        yaml.safe_dump(worker_result, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

    # ── Finalize state.json ───────────────────────────────────────────────────
    state["status"] = final_status
    state["collected_at"] = now
    atomic_write_json(state_path, state)

    return dict(final)
