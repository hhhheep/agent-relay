from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List

from .schema import TaskBook


@dataclass(frozen=True)
class ContractFinding:
    rule_id: str
    severity: str
    layer: str
    message: str
    field: str | None = None

    def to_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "layer": self.layer,
            "message": self.message,
        }
        if self.field:
            payload["field"] = self.field
        return payload


@dataclass(frozen=True)
class ContractValidationResult:
    checked: bool
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    findings: List[ContractFinding] = field(default_factory=list)
    details: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "checked": self.checked,
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "findings": [finding.to_dict() for finding in self.findings],
            "details": dict(self.details),
        }


def _string_list(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _candidate_file(result_root: Path, name: str) -> Path | None:
    for candidate in (result_root / name, result_root / "artifacts" / name):
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _is_safe_contract_file_path(name: str) -> bool:
    text = str(name or "").replace("\\", "/").strip()
    if not text or text in {".", ".."} or Path(text).is_absolute():
        return False
    return ".." not in PurePosixPath(text).parts


def _load_json_file(path: Path) -> tuple[dict | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"{path.name} is not valid JSON: {type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, f"{path.name} must be a JSON object"
    return payload, None


def _field_value(payload: Dict[str, Any], dotted: str) -> tuple[bool, Any]:
    current: Any = payload
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _field_type_errors(field_name: str, value: Any, expected_type: str) -> List[str]:
    kind = str(expected_type or "").strip().lower()
    if not kind:
        return []
    if kind in {"number", "float", "int"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return [f"decision_contract field has wrong type: {field_name} expected {kind}"]
        if not math.isfinite(float(value)):
            return [f"decision_contract numeric field is not finite: {field_name}"]
        if kind == "int" and not isinstance(value, int):
            return [f"decision_contract field has wrong type: {field_name} expected int"]
        return []
    if kind in {"string", "str"}:
        lowered = str(value).strip().lower() if isinstance(value, str) else ""
        if lowered in {"nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}:
            return [f"decision_contract field contains non-finite numeric string: {field_name}"]
        return [] if isinstance(value, str) else [f"decision_contract field has wrong type: {field_name} expected string"]
    if kind in {"bool", "boolean"}:
        return [] if isinstance(value, bool) else [f"decision_contract field has wrong type: {field_name} expected bool"]
    if kind in {"object", "dict"}:
        return [] if isinstance(value, dict) else [f"decision_contract field has wrong type: {field_name} expected object"]
    if kind in {"array", "list"}:
        return [] if isinstance(value, list) else [f"decision_contract field has wrong type: {field_name} expected array"]
    return [f"decision_contract unsupported field type for {field_name}: {expected_type}"]


def _finding(rule_id: str, severity: str, layer: str, message: str, field: str | None = None) -> ContractFinding:
    return ContractFinding(rule_id=rule_id, severity=severity, layer=layer, message=message, field=field)


def _field_type_findings(field_name: str, value: Any, expected_type: str) -> List[ContractFinding]:
    return [
        _finding("field_type_invalid", "error", "hard_contract", message, field_name)
        for message in _field_type_errors(field_name, value, expected_type)
    ]


def _numeric_value(payload: Dict[str, Any], field_name: str) -> float | None:
    exists, value = _field_value(payload, field_name)
    if not exists or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _bool_value(payload: Dict[str, Any], field_name: str) -> bool | None:
    exists, value = _field_value(payload, field_name)
    return value if exists and isinstance(value, bool) else None


def _semantic_contract_findings(payload: Dict[str, Any], contract: Dict[str, Any]) -> List[ContractFinding]:
    checks = contract.get("semantic_checks")
    if isinstance(checks, str):
        requested = {checks}
    elif isinstance(checks, list):
        requested = {str(item) for item in checks}
    elif checks is True:
        requested = {"a_residual_decision_consistency"}
    else:
        requested = set()
    findings: List[ContractFinding] = []
    if "a_residual_decision_consistency" not in requested:
        return findings

    fusion_beats = _bool_value(payload, "fusion_beats_detector")
    best_iou_gain = _numeric_value(payload, "best_iou_gain")
    boundary_gain = _numeric_value(payload, "best_boundary_f1_gain")
    fp_reduction = _numeric_value(payload, "real_side_fp_reduction")
    recall_loss = _numeric_value(payload, "fake_core_recall_loss")
    recommended_exists, recommended_use = _field_value(payload, "recommended_use")

    if fusion_beats is True and best_iou_gain is not None and best_iou_gain <= 0:
        findings.append(_finding("a_residual_fusion_claim_iou_nonpositive", "error", "semantic_contract", "decision_contract semantic inconsistency: fusion_beats_detector is true but best_iou_gain is not positive", "fusion_beats_detector"))
    if fusion_beats is False and best_iou_gain is not None and best_iou_gain > 0:
        findings.append(_finding("a_residual_fusion_reject_iou_positive", "error", "semantic_contract", "decision_contract semantic inconsistency: fusion_beats_detector is false but best_iou_gain is positive", "fusion_beats_detector"))
    if isinstance(recommended_use, str):
        normalized = recommended_use.strip().lower()
        if normalized in {"adopt", "use", "production", "recommended"}:
            if best_iou_gain is not None and best_iou_gain <= 0:
                findings.append(_finding("a_residual_adopt_iou_nonpositive", "error", "semantic_contract", "decision_contract semantic inconsistency: recommended_use claims adoption but best_iou_gain is not positive", "recommended_use"))
            if fp_reduction is not None and fp_reduction < 0:
                findings.append(_finding("a_residual_adopt_fp_reduction_negative", "error", "semantic_contract", "decision_contract semantic inconsistency: recommended_use claims adoption but real_side_fp_reduction is negative", "recommended_use"))
        if normalized in {"diagnostic_only", "do_not_use", "reject"} and fusion_beats is True and best_iou_gain is not None and best_iou_gain > 0:
            if boundary_gain is not None and boundary_gain >= 0 and (fp_reduction is None or fp_reduction >= 0) and (recall_loss is None or recall_loss <= 0):
                findings.append(_finding("a_residual_diagnostic_conflicts_with_positive_gains", "warning", "semantic_contract", "decision_contract semantic warning: diagnostic/reject recommendation conflicts with positive gains and no recorded tradeoff", "recommended_use"))
    elif recommended_exists:
        findings.append(_finding("a_residual_recommended_use_not_string", "error", "semantic_contract", "decision_contract semantic inconsistency: recommended_use must be a string for a_residual_decision_consistency", "recommended_use"))
    return findings


def validate_final_contract(task: TaskBook, result_root: Path) -> ContractValidationResult:
    """Validate explicit decision/final artifact contracts.

    This is modeled after experiment-tracking artifact validation: a worker
    success is not enough for a final decision task. If the task book declares
    required files or fields, collection must verify them before marking the
    run completed.
    """
    raw = task.raw_front_matter or {}
    contract = raw.get("decision_contract") or raw.get("final_contract")
    if not isinstance(contract, dict) or not contract:
        return ContractValidationResult(checked=False, ok=True)

    required_files = _string_list(contract.get("required_files"))
    required_fields = _string_list(contract.get("required_fields"))
    nullable_fields = set(_string_list(contract.get("nullable_fields") or contract.get("allow_null_fields")))
    required_non_null = set(_string_list(contract.get("required_non_null_fields")))
    field_types = contract.get("field_types") if isinstance(contract.get("field_types"), dict) else {}
    primary_json = str(contract.get("primary_json") or "decision_summary.json").strip()
    if required_fields and primary_json and primary_json not in required_files:
        required_files.append(primary_json)

    errors: List[str] = []
    warnings: List[str] = []
    findings: List[ContractFinding] = []
    found_files: Dict[str, str] = {}
    missing_files: List[str] = []
    for name in required_files:
        if not _is_safe_contract_file_path(name):
            message = f"decision_contract required file path is unsafe: {name}"
            errors.append(message)
            findings.append(_finding("unsafe_required_file_path", "error", "hard_contract", message, name))
            continue
        path = _candidate_file(result_root, name)
        if path is None:
            missing_files.append(name)
        else:
            found_files[name] = str(path)
    for name in missing_files:
        message = f"decision_contract missing required file: {name}"
        errors.append(message)
        findings.append(_finding("required_file_missing", "error", "hard_contract", message, name))

    json_payload: dict | None = None
    json_path = _candidate_file(result_root, primary_json) if primary_json else None
    if required_fields:
        if json_path is None:
            message = f"decision_contract required_fields need missing primary_json: {primary_json}"
            errors.append(message)
            findings.append(_finding("primary_json_missing", "error", "hard_contract", message, primary_json))
        else:
            json_payload, json_error = _load_json_file(json_path)
            if json_error:
                errors.append(json_error)
                findings.append(_finding("primary_json_invalid", "error", "hard_contract", json_error, primary_json))

    missing_fields: List[str] = []
    null_fields: List[str] = []
    type_errors: List[str] = []
    semantic_errors: List[str] = []
    if json_payload is not None:
        for field_name in required_fields:
            exists, value = _field_value(json_payload, field_name)
            if not exists:
                missing_fields.append(field_name)
                continue
            if value is None and field_name not in nullable_fields:
                null_fields.append(field_name)
            if field_name in required_non_null and value is None:
                null_fields.append(field_name)
            if value is not None and field_name in field_types:
                type_findings = _field_type_findings(field_name, value, str(field_types.get(field_name)))
                type_errors.extend(finding.message for finding in type_findings)
                findings.extend(type_findings)
        for field_name in missing_fields:
            message = f"decision_contract missing required field: {field_name}"
            errors.append(message)
            findings.append(_finding("required_field_missing", "error", "hard_contract", message, field_name))
        for field_name in sorted(set(null_fields)):
            message = f"decision_contract required field is null: {field_name}"
            errors.append(message)
            findings.append(_finding("required_field_null", "error", "hard_contract", message, field_name))
        errors.extend(type_errors)
        semantic_findings = _semantic_contract_findings(json_payload, contract)
        semantic_errors = [finding.message for finding in semantic_findings if finding.severity == "error"]
        semantic_warnings = [finding.message for finding in semantic_findings if finding.severity != "error"]
        errors.extend(semantic_errors)
        warnings.extend(semantic_warnings)
        findings.extend(semantic_findings)

    if not required_files and not required_fields:
        message = "decision_contract declared but has no required_files or required_fields"
        warnings.append(message)
        findings.append(_finding("empty_contract", "warning", "advisory_contract", message))

    return ContractValidationResult(
        checked=True,
        ok=not errors,
        errors=errors,
        warnings=warnings,
        findings=findings,
        details={
            "required_files": required_files,
            "required_fields": required_fields,
            "nullable_fields": sorted(nullable_fields),
            "primary_json": primary_json,
            "found_files": found_files,
            "missing_files": missing_files,
            "missing_fields": missing_fields,
            "null_fields": sorted(set(null_fields)),
            "field_types": {str(key): str(value) for key, value in field_types.items()},
            "type_errors": type_errors,
            "semantic_errors": semantic_errors,
        },
    )
