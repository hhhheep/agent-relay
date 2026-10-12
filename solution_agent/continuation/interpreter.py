from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .schema import ResultInterpretation


RESULT_ARTIFACT_CANDIDATES = (
    "_worker_out/result.yaml",
    "result.yaml",
    "final_result.yaml",
    "decision.json",
    "decision_summary.json",
    "action_requests.json",
    "artifact_summary.md",
    "judge_report.yaml",
)

_SUCCESS_STRINGS = ("success", "succeeded", "completed")
_PARTIAL_STRINGS = ("partial", "incomplete", "interrupted")
_FAILURE_STRINGS = ("failed", "failure", "error")
_WEAK_SIGNALS = (
    "weak",
    "boundary_f1_gain",
    "boundary_f1",
    "low confidence",
    "confidence was weak",
    "weakly",
)


def _read_file_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _parse_json_or_yaml(path: Path) -> Optional[Dict[str, Any]]:
    text = _read_file_text(path)
    if text is None:
        return None
    payload = text.strip()
    if not payload:
        return None
    for loader in ("json", "yaml"):
        try:
            if loader == "json":
                parsed = json.loads(payload)
            else:
                import yaml

                parsed = yaml.safe_load(payload)
            if isinstance(parsed, dict):
                return parsed
            return None
        except Exception:
            continue
    return None


def _lowered(text: str) -> str:
    return text.lower()


def _contains_any(haystack: str, needles: Tuple[str, ...]) -> bool:
    lower = _lowered(haystack)
    return any(needle in lower for needle in needles)


def _normalize_bool(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return None


def _iter_text_blob(discovered: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    for entry in discovered["discovered"]:
        text = entry.get("text")
        if isinstance(text, str) and text.strip():
            values.append(text)
        parsed = entry.get("parsed")
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, str) and v.strip():
                    values.append(v)
    return values


def _derive_evidence(
    *,
    discovered: Dict[str, Any],
    parsed_artifacts: Dict[str, Dict[str, Any]],
    has_discovery: bool,
) -> Dict[str, Any]:
    text_blobs = _iter_text_blob(discovered)
    evidence: Dict[str, Any] = {
        "deterministic_only": True,
        "artifact_discovery": discovered,
    }

    for payload in parsed_artifacts.values():
        if "evidence" in payload and isinstance(payload["evidence"], dict):
            nested = payload["evidence"]
            if "missing_artifact_detected" in nested:
                val = _normalize_bool(nested["missing_artifact_detected"])
                if val is not None:
                    evidence["missing_artifact_detected"] = val
            if "source_code_required" in nested:
                val = _normalize_bool(nested["source_code_required"])
                if val is not None:
                    evidence["source_code_required"] = val
            if "needs_codex" in nested:
                val = _normalize_bool(nested["needs_codex"])
                if val is not None:
                    evidence["needs_codex"] = val
            if "missing_capability" in nested:
                val = _normalize_bool(nested["missing_capability"])
                if val is not None:
                    evidence["missing_capability_detected"] = val
            if "missing_capability_detected" in nested:
                val = _normalize_bool(nested["missing_capability_detected"])
                if val is not None:
                    evidence["missing_capability_detected"] = val

        if "missing_capability" in payload:
            val = _normalize_bool(payload["missing_capability"])
            if val is not None:
                evidence["missing_capability_detected"] = val
        if "missing_capability_detected" in payload:
            val = _normalize_bool(payload["missing_capability_detected"])
            if val is not None:
                evidence["missing_capability_detected"] = val
        if "needs_codex" in payload:
            val = _normalize_bool(payload["needs_codex"])
            if val is not None:
                evidence["needs_codex"] = val
        if "source_code_required" in payload:
            val = _normalize_bool(payload["source_code_required"])
            if val is not None:
                evidence["source_code_required"] = val
        if "execution_success" in payload:
            val = _normalize_bool(payload["execution_success"])
            if val is not None:
                evidence["execution_success"] = val
        if "status" in payload:
            status = str(payload["status"]).strip().lower()
            if status:
                evidence[f"status_{status}"] = True
        if "result_status" in payload:
            status = str(payload["result_status"]).strip().lower()
            if status:
                evidence[f"status_{status}"] = True
        if "artifact_text" in payload:
            artifact_text = str(payload["artifact_text"])
            if artifact_text:
                evidence["artifact_text"] = artifact_text
                text_blobs.append(artifact_text)

    missing_capability_markers = (
        "missing capability",
        "capability is missing",
        "missing_capability_detected",
        "capability unavailable",
    )
    source_code_markers = (
        "source code",
        "source wrapper",
        "implement source",
        "source change required",
        "modify source",
    )
    weak_markers = (
        "weak",
        "boundary_f1_gain",
        "boundary gain",
        "low confidence",
    )
    missing_artifact_markers = (
        "missing artifact",
        "missing file",
        "no artifact",
        "artifact is missing",
        "artifact missing",
    )

    joined_text = " ".join(text_blobs)
    if joined_text:
        evidence["artifact_text_snippet"] = joined_text[:2048]

    # explicit risk and policy-shape signals (kept as simple deterministic
    # traces for review)
    evidence["injection_detected"] = any(
        phrase in joined_text for phrase in ("ignore previous", "ignore instructions", "do not")
    )
    evidence["cross_workplace_risk_detected"] = "cross workplace" in joined_text
    evidence["cross_line_risk_detected"] = "line b" in joined_text
    evidence["remote_path_guess_detected"] = "/ssd" in joined_text
    evidence["direct_dispatch_detected"] = any(
        keyword in joined_text for keyword in ("dispatch", "run-next", "schedule_remote")
    )
    evidence["missing_capability_detected"] = evidence.get(
        "missing_capability_detected",
        bool(_contains_any(joined_text, missing_capability_markers)),
    )
    evidence["source_code_required_detected"] = bool(_contains_any(joined_text, source_code_markers))
    evidence["weak_evidence_detected"] = bool(_contains_any(joined_text, weak_markers))
    evidence["missing_artifact_detected"] = evidence.get(
        "missing_artifact_detected",
        bool(_contains_any(joined_text, missing_artifact_markers)),
    )

    if not has_discovery:
        evidence["no_local_artifacts"] = True
        evidence.setdefault("missing_artifact_detected", True)

    return evidence


def _extract_parsed_artifacts(result_dir: Path) -> Dict[str, Dict[str, Any]]:
    payloads: Dict[str, Dict[str, Any]] = {}
    for relative in RESULT_ARTIFACT_CANDIDATES:
        path = result_dir / relative
        parsed = _parse_json_or_yaml(path)
        if parsed:
            payloads[relative] = parsed
    return payloads


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_result_artifacts(result_dir: Path) -> Dict[str, Any]:
    """Discover local continuation input artifacts without interpreting content."""

    discovered: List[Dict[str, Any]] = []
    missing: List[str] = []
    for relative in RESULT_ARTIFACT_CANDIDATES:
        path = result_dir / relative
        if not path.is_file():
            missing.append(relative)
            continue
        parsed = _parse_json_or_yaml(path)
        raw = _read_file_text(path) or ""
        discovered.append(
            {
                "relative_path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
                "text": raw,
                "parsed": parsed,
            }
        )
    return {
        "result_dir": str(result_dir),
        "exists": result_dir.exists(),
        "discovered": discovered,
        "missing_candidates": missing,
    }


def _discovery_hash(source_task_id: str, discovery: Dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(source_task_id.encode("utf-8"))
    for item in discovery["discovered"]:
        digest.update(item["relative_path"].encode("utf-8"))
        digest.update(item["sha256"].encode("utf-8"))
    if not discovery["discovered"]:
        digest.update(str(discovery["exists"]).encode("utf-8"))
    return digest.hexdigest()


def interpret_stub(source_task_id: str) -> ResultInterpretation:
    """P0 skeleton interpreter: classify as unknown/no-safe-continuation."""

    result_hash = hashlib.sha256(source_task_id.encode("utf-8")).hexdigest()
    return ResultInterpretation(
        source_task_id=source_task_id,
        source_result_hash=result_hash,
        idempotency_key=f"{source_task_id}:{result_hash}:0.1.0:interpret",
        category="unknown",
        recommended_continuation_kinds=["no_safe_continuation"],
        needs_user_decision=True,
        claim_limitations=["P0 skeleton interpreter does not inspect artifacts."],
    )


def interpret_result_dir_stub(source_task_id: str, result_dir: Path) -> ResultInterpretation:
    """P0 deterministic interpreter: discover local artifacts, do not infer conclusions."""

    discovery = discover_result_artifacts(result_dir)
    parsed_artifacts = _extract_parsed_artifacts(result_dir)
    result_hash = _discovery_hash(source_task_id, discovery)
    evidence = _derive_evidence(
        discovered=discovery,
        parsed_artifacts=parsed_artifacts,
        has_discovery=bool(discovery["discovered"]),
    )

    category = "unknown"
    execution_success = False
    needs_codex = False
    needs_user_decision = False
    recommended_kinds: List[str] = ["no_safe_continuation"]

    explicit = None
    for payload in parsed_artifacts.values():
        explicit_val = payload.get("category")
        if explicit_val in (
            "success_but_weak_evidence",
            "missing_artifact",
            "missing_capability",
            "source_code_required",
            "needs_user_decision",
        ):
            explicit = str(explicit_val)
            break
        if "expected_category" in payload and payload["expected_category"] in (
            "success_but_weak_evidence",
            "missing_artifact",
            "missing_capability",
            "source_code_required",
        ):
            explicit = str(payload["expected_category"])
            break

    status_text = ""
    if isinstance(parsed_artifacts.get("result.yaml"), dict):
        status_text = str(parsed_artifacts.get("result.yaml", {}).get("status", "")).lower()
    if not status_text and isinstance(parsed_artifacts.get("final_result.yaml"), dict):
        status_text = str(parsed_artifacts.get("final_result.yaml", {}).get("status", "")).lower()

    if status_text:
        execution_success = "success" in status_text or "complete" in status_text
        needs_user_decision = bool(_contains_any(status_text, _FAILURE_STRINGS))

    text_blob = (str(evidence.get("artifact_text_snippet", ""))).lower()

    category_explicit = explicit
    if category_explicit:
        if category_explicit == "needs_user_decision":
            category = "needs_user_decision"
            needs_user_decision = True
            recommended_kinds = ["no_safe_continuation"]
        else:
            category = category_explicit
    elif evidence.get("source_code_required"):
        category = "source_code_required"
        needs_codex = True
    elif evidence.get("needs_codex"):
        category = "source_code_required"
        needs_codex = True
    elif evidence.get("missing_capability_detected"):
        category = "missing_capability"
    elif evidence.get("missing_artifact_detected"):
        category = "missing_artifact"
    elif not discovery["discovered"]:
        category = "missing_artifact"
    elif evidence.get("status_partial") or evidence.get("status_incomplete") or evidence.get("status_interrupted"):
        category = "needs_user_decision"
    elif evidence.get("weak_evidence_detected") and ("success" in text_blob or execution_success):
        category = "success_but_weak_evidence"
        execution_success = True
        needs_user_decision = False
        recommended_kinds = ["bounded_probe", "decision_synthesis"]
    elif execution_success:
        category = "success_but_weak_evidence"
        recommended_kinds = ["bounded_probe", "decision_synthesis"]
    elif evidence.get("source_code_required") or evidence.get("source_code_required_detected"):
        category = "source_code_required"
        needs_codex = True
    elif evidence.get("weak_evidence_detected"):
        category = "success_but_weak_evidence"
        recommended_kinds = ["bounded_probe", "decision_synthesis"]
    elif evidence.get("status_failed") or evidence.get("status_error"):
        category = "needs_user_decision"
        needs_user_decision = True
        recommended_kinds = ["no_safe_continuation"]

    if category == "missing_artifact":
        needs_user_decision = True
        recommended_kinds = ["inventory_audit"]
    elif category == "missing_capability":
        recommended_kinds = ["missing_capability_request"]
    elif category == "source_code_required":
        needs_codex = True
        recommended_kinds = ["codex_debug_request"]
    elif category == "success_but_weak_evidence":
        execution_success = True
        needs_user_decision = False
        recommended_kinds = ["bounded_probe", "decision_synthesis"]

    claim = []
    if category in {"success_but_weak_evidence", "missing_artifact", "missing_capability", "source_code_required"}:
        claim.append("Deterministic local artifact interpretation.")

    if category == "missing_artifact":
        claim.append(
            "Artifacts appear absent locally or explicitly marked as missing in result metadata."
        )
    if category == "missing_capability":
        claim.append("Interpreter detected missing capability markers in local result metadata.")
    if category == "source_code_required":
        claim.append("Interpreter detected source-update requirement in local result metadata.")
    if category == "success_but_weak_evidence":
        claim.append("Execution appears successful but evidence appears weak; requires bounded follow-up.")

    return ResultInterpretation(
        source_task_id=source_task_id,
        source_result_hash=result_hash,
        idempotency_key=f"{source_task_id}:{result_hash}:0.1.0:interpret",
        category=category,
        execution_success=bool(execution_success),
        blocks_execution=category == "needs_user_decision",
        blocks_claim_not_execution=category in {"needs_user_decision"},
        needs_codex=needs_codex,
        needs_user_decision=needs_user_decision,
        recommended_continuation_kinds=recommended_kinds,
        evidence=evidence,
        claim_limitations=claim or ["P0 deterministic interpreter inferred category from local artifacts."],
    )
