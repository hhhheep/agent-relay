from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List

from . import launch_intent, manual_launch
from .knowledge_to_proposal import _coerce_nested_readiness_payload
from .line_progression import assess_line_progression
from .night_auto_pending_staging import write_night_auto_pending_staging_artifact
from .proposal_staging import stage_proposal_candidate_dry_run
from .repair_executor_selector import select_repair_executor, write_repair_execution_artifacts
from .research_prerequisites import write_research_prerequisite_gate
from .project_knowledge_ingestion import (
    build_project_knowledge_from_artifact_index_dry_run,
    build_project_knowledge_from_context_index_dry_run,
)
from .scheduler_explain import explain_night_auto_pending_scheduler_dry_run, parse_task_front_matter
from .source_repair import (
    build_source_repair_plan_from_taskbook,
    materialize_source_repair_patch,
    write_source_repair_plan,
)


THREE_AGENT_FLOW_SCHEMA_VERSION = 1
REPAIR_TO_SCHEDULER_GATE_SCHEMA_VERSION = 1
AUTONOMY_ASSESSMENT_SCHEMA_VERSION = 1
WORKPLACE_PATCH_VALIDATION_SCHEMA_VERSION = 1
SCHEDULER_ADMISSION_REHEARSAL_SCHEMA_VERSION = 1
L72_ADMISSION_PACKET_SCHEMA_VERSION = 1
L72_DRAFT_SCHEDULER_DRY_RUN_SCHEMA_VERSION = 1
L72_STAGING_CANDIDATE_SCHEMA_VERSION = 1
L72_MANUAL_LAUNCH_READINESS_SCHEMA_VERSION = 1


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _read_action_requests(path: Path) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    if isinstance(payload, dict):
        value = payload.get("action_requests", [])
        return value if isinstance(value, list) else []
    return payload if isinstance(payload, list) else []


def _coerce_optional_next_step_readiness(*values: Any) -> Dict[str, Any] | None:
    for value in values:
        if not isinstance(value, dict):
            continue
        if not any(
            key in value
            for key in (
                "can_stage",
                "route",
                "status",
                "blockers",
                "bounded_validation",
            )
        ):
            continue
        readiness = _coerce_nested_readiness_payload(value)
        if (
            readiness
            and (
                readiness.get("can_stage") is True
                or bool(readiness.get("route"))
                or bool(readiness.get("status"))
                or bool(readiness.get("blockers"))
                or isinstance(readiness.get("bounded_validation"), dict)
            )
        ):
            return readiness
    return None


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_research_prerequisite_packet(
    *,
    project_knowledge: Dict[str, Any],
    discussion_payload: Dict[str, Any] | None,
    output_dir: Path | str,
) -> Dict[str, Any]:
    """Write a shared prerequisite gate artifact for research-driving flows.

    This is a local deterministic adapter only. It does not dispatch scheduler
    work, mutate registries, or execute remote side effects.
    """

    written = write_research_prerequisite_gate(project_knowledge, discussion_payload, output_dir)
    gate = written.get("gate", {}) if isinstance(written, dict) else {}
    can_continue = bool(gate.get("can_continue_to_mission_gate"))
    chosen_action = _normalize(gate.get("chosen_action"))
    if can_continue:
        next_recommendation = "continue_to_mission_gate"
    elif chosen_action == "request_inventory":
        next_recommendation = "run_inventory_before_any_execution"
    elif chosen_action == "request_human_boundary":
        next_recommendation = "ask_human_for_boundary_decision"
    elif chosen_action == "hold":
        next_recommendation = "design_validation_protocol_or_continue_discussion"
    else:
        next_recommendation = "continue_discussion_until_prerequisites_complete"

    report = {
        "schema_version": 1,
        "status": "packet_ready",
        "artifact_type": "research_prerequisite_packet",
        "can_continue_to_mission_gate": can_continue,
        "chosen_action": chosen_action,
        "reason_codes": list(gate.get("reason_codes") or []),
        "reason_text": _normalize(gate.get("reason_text")),
        "research_prerequisite_gate_path": _normalize(written.get("gate_path")),
        "next_recommendation": next_recommendation,
        "side_effects": {
            "source_edit": False,
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "remote_execution": False,
            "api_call": False,
        },
    }
    report_path = Path(output_dir) / "research_prerequisite_packet.json"
    _write_json(report_path, report)
    report["report_path"] = str(report_path)
    return report


def build_context_index_research_prerequisite_packet(
    *,
    repo_root: Path | str,
    query: str,
    output_dir: Path | str,
    discussion_payload: Dict[str, Any] | None = None,
    project_id: str | None = None,
    workplace_profile: str | None = None,
    knowledge_root_ref: str | None = None,
    limit: int = 8,
) -> Dict[str, Any]:
    """Build project knowledge from context-index and emit prerequisite packet."""

    project_knowledge = build_project_knowledge_from_context_index_dry_run(
        repo_root,
        query,
        project_id=project_id,
        workplace_profile=workplace_profile,
        knowledge_root_ref=knowledge_root_ref,
        limit=limit,
    )
    output_path = Path(output_dir)
    bundle_path = output_path / "project_knowledge_bundle.json"
    _write_json(bundle_path, project_knowledge)
    packet = build_research_prerequisite_packet(
        project_knowledge=project_knowledge,
        discussion_payload=discussion_payload or {},
        output_dir=output_path,
    )
    packet["project_knowledge_bundle_path"] = str(bundle_path)
    packet["project_knowledge_summary"] = {
        "project_id": _normalize(project_knowledge.get("project_id")),
        "mainline": _normalize(
            project_knowledge.get("current_state", {}).get("mainline")
            if isinstance(project_knowledge.get("current_state"), dict)
            else ""
        ),
        "evidence_ref_count": len(project_knowledge.get("evidence_refs") or []),
        "ingestion_mode": _normalize(
            project_knowledge.get("ingestion", {}).get("mode")
            if isinstance(project_knowledge.get("ingestion"), dict)
            else ""
        ),
    }
    packet["context_index_query"] = query
    packet["context_index_limit"] = int(limit)
    packet["side_effects"]["docs_dir_scan"] = False
    _write_json(output_path / "research_prerequisite_packet.json", packet)
    return packet


def build_artifact_index_research_prerequisite_packet(
    *,
    repo_root: Path | str,
    query: str,
    output_dir: Path | str,
    discussion_payload: Dict[str, Any] | None = None,
    project_id: str | None = None,
    workplace_profile: str | None = None,
    knowledge_root_ref: str | None = None,
    limit: int = 8,
) -> Dict[str, Any]:
    """Build project knowledge from artifact-index and emit prerequisite packet."""

    project_knowledge = build_project_knowledge_from_artifact_index_dry_run(
        repo_root,
        query,
        project_id=project_id,
        workplace_profile=workplace_profile,
        knowledge_root_ref=knowledge_root_ref,
        limit=limit,
    )
    output_path = Path(output_dir)
    bundle_path = output_path / "project_knowledge_bundle.json"
    _write_json(bundle_path, project_knowledge)
    packet = build_research_prerequisite_packet(
        project_knowledge=project_knowledge,
        discussion_payload=discussion_payload or {},
        output_dir=output_path,
    )
    packet["project_knowledge_bundle_path"] = str(bundle_path)
    packet["project_knowledge_summary"] = {
        "project_id": _normalize(project_knowledge.get("project_id")),
        "mainline": _normalize(
            project_knowledge.get("current_state", {}).get("mainline")
            if isinstance(project_knowledge.get("current_state"), dict)
            else ""
        ),
        "evidence_ref_count": len(project_knowledge.get("evidence_refs") or []),
        "ingestion_mode": _normalize(
            project_knowledge.get("ingestion", {}).get("mode")
            if isinstance(project_knowledge.get("ingestion"), dict)
            else ""
        ),
    }
    packet["artifact_index_query"] = query
    packet["artifact_index_limit"] = int(limit)
    packet["side_effects"]["docs_dir_scan"] = False
    packet["side_effects"]["context_index_query"] = False
    _write_json(output_path / "research_prerequisite_packet.json", packet)
    return packet


def _summary_output_dir(summary_path: Path | None, output_dir: Path | str | None) -> Path:
    if output_dir is not None:
        return Path(output_dir)
    if summary_path is not None:
        return summary_path.parent
    return Path.cwd()


def _read_json_file(path: Path) -> Dict[str, Any]:
    return _read_json(path)


def _normalized_text_for_hash(text: str) -> str:
    normalized = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return normalized + "\n"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_patch_intent_from_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    agents = summary.get("agents") if isinstance(summary.get("agents"), dict) else {}
    discussion_outputs = agents.get("discussion_continuation_agent", {}).get("outputs", {})
    patch_intent_raw = _normalize(discussion_outputs.get("patch_intent_path", ""))
    if patch_intent_raw:
        patch_intent_path = Path(patch_intent_raw)
        patch_intent = _read_json_file(patch_intent_path)
        if patch_intent:
            return patch_intent
    plan_path_raw = _normalize(discussion_outputs.get("source_repair_plan_path", ""))
    if plan_path_raw:
        plan_path = Path(plan_path_raw)
        plan = _read_json_file(plan_path)
        patch_intent = plan.get("patch_intent")
        if isinstance(patch_intent, dict):
            return patch_intent
    return {}


def _load_validation_report_from_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    agents = summary.get("agents") if isinstance(summary.get("agents"), dict) else {}
    opencoder_outputs = agents.get("opencoder_agent", {}).get("outputs", {})
    embedded_report = opencoder_outputs.get("validation_report")
    if isinstance(embedded_report, dict) and embedded_report:
        return embedded_report

    report_path_raw = _normalize(opencoder_outputs.get("validation_report_path"))
    if not report_path_raw:
        repair_result = opencoder_outputs.get("repair_result", {})
        if isinstance(repair_result, dict):
            report_path_raw = _normalize(repair_result.get("validation_report_path"))
    if report_path_raw:
        return _read_json_file(Path(report_path_raw))
    return {}


def _update_repair_result_for_external_validation(
    repair_result: Dict[str, Any],
    validation_report_path: Path,
    *,
    validation_passed: bool,
    blockers: List[str],
) -> Dict[str, Any]:
    updated = copy.deepcopy(repair_result)
    updated["validation_report_path"] = str(validation_report_path)
    updated["execution_blockers"] = sorted(set(blockers))
    if validation_passed:
        updated["execution_status"] = "externally_applied"
        updated["status"] = "validated_external_repair"
    else:
        updated["execution_status"] = "not_attempted"
        updated["status"] = "validation_blocked"
    return updated


def validate_workplace_patch_adapter(
    summary: Dict[str, Any],
    *,
    workplace_root: Path | str,
    output_dir: Path | str | None = None,
    summary_path: Path | None = None,
    write_summary: bool = True,
) -> Dict[str, Any]:
    """Validate a bounded L71 workplace patch without applying or dispatching anything."""

    output_path = _summary_output_dir(summary_path, output_dir)
    validation_report_path = output_path / "validation_report.json"
    lifecycle_repair_path = output_path / "opencoder_lifecycle" / "repair_result.yaml"
    normalized_root = Path(workplace_root)
    patch_intent = _load_patch_intent_from_summary(summary)
    target_file = _normalize(patch_intent.get("target_file"))
    target_path = normalized_root / target_file if target_file else normalized_root
    report_blockers: List[str] = []

    target_file_exists = bool(target_file) and target_path.exists() and target_path.is_file()
    if not target_file_exists:
        report_blockers.append("target_file_missing")

    module_name = f"b14_bbox_sidecar_validation_{abs(hash(str(target_path.resolve() if target_path.exists() else target_path)))}"
    import_ok = False
    smoke_ok = False
    claim_boundary_ok = False
    no_dispatch_safety_ok = False
    smoke_result_preview: List[Dict[str, Any]] = []
    import_error = ""
    claim_boundary_checks: List[str] = []
    no_dispatch_checks: List[str] = []
    patch_fingerprint_ok = False
    expected_file_sha256 = ""
    actual_file_sha256 = ""

    if target_file_exists:
        try:
            spec = importlib.util.spec_from_file_location(module_name, target_path)
            module = importlib.util.module_from_spec(spec) if spec and spec.loader else None
            if module is None or spec is None or spec.loader is None:
                raise ImportError("module_spec_unavailable")
            spec.loader.exec_module(module)
            decode = getattr(module, "decode_p2_bbox_candidates", None)
            if not callable(decode):
                raise AttributeError("decode_p2_bbox_candidates_missing")
            import_ok = True

            smoke_input = [
                {
                    "score": 0.9,
                    "bbox_xyxy_original": [1, 2, 30, 40],
                    "bbox_xyxy_local32": [0, 1, 15, 20],
                    "q_iou": 0.7,
                    "scale_bucket": "large",
                    "crop_id": "crop_1",
                    "source": "candidate_a",
                    "evidence_summary": {"kind": "synthetic"},
                    "local32_to_original_mapping": True,
                },
                {
                    "score": 0.1,
                    "bbox_xyxy_original": [2, 3, 4, 5],
                },
            ]
            smoke_result = decode("image_001", copy.deepcopy(smoke_input), top_m=1)
            smoke_ok = (
                isinstance(smoke_result, list)
                and len(smoke_result) == 1
                and isinstance(smoke_result[0], dict)
                and smoke_result[0].get("image_id") == "image_001"
                and smoke_result[0].get("source") == "candidate_a"
                and smoke_result[0].get("score") == 0.9
            )
            if smoke_ok:
                smoke_result_preview = [smoke_result[0]]

            module_text = target_path.read_text(encoding="utf-8")
            allowed_keys = {
                "bbox_xyxy_local32",
                "bbox_xyxy_original",
                "crop_id",
                "evidence_summary",
                "image_id",
                "local32_to_original_mapping",
                "p_valid",
                "q_iou",
                "scale_bucket",
                "score",
                "source",
            }
            smoke_keys = set(smoke_result[0].keys()) if smoke_ok and smoke_result else set()
            if smoke_keys and smoke_keys.issubset(allowed_keys):
                claim_boundary_checks.append("smoke_output_keys_are_report_only")
            if "must not mutate" in module_text and "Report-only" in module_text:
                claim_boundary_checks.append("module_docstring_declares_report_only_boundary")
            must_change_only = patch_intent.get("expected_validation", {}).get("must_change_only", [])
            if must_change_only == [target_file]:
                claim_boundary_checks.append("patch_intent_must_change_only_single_target")

            expected_text = patch_intent.get("new_text")
            if isinstance(expected_text, str) and expected_text.strip():
                expected_file_sha256 = _sha256_text(_normalized_text_for_hash(expected_text))
                actual_file_sha256 = _sha256_text(_normalized_text_for_hash(module_text))
                if expected_file_sha256 == actual_file_sha256:
                    patch_fingerprint_ok = True
                    claim_boundary_checks.append("workplace_file_matches_patch_intent_text")
            claim_boundary_ok = len(claim_boundary_checks) == 4

            forbidden_markers = ("dispatch", "scheduler", "subprocess", "write_text(", "run(")
            if all(marker not in module_text for marker in forbidden_markers):
                no_dispatch_checks.append("module_text_has_no_dispatch_or_subprocess_markers")
            if all(not key.startswith("dispatch") for key in smoke_keys):
                no_dispatch_checks.append("smoke_output_does_not_claim_dispatch")
            no_dispatch_safety_ok = len(no_dispatch_checks) == 2
        except Exception as exc:  # pragma: no cover - exercised by failure assertions
            import_error = f"{type(exc).__name__}: {exc}"

    if not import_ok:
        report_blockers.append("import_smoke_failed")
    if not smoke_ok:
        report_blockers.append("sidecar_smoke_failed")
    if not claim_boundary_ok:
        report_blockers.append("claim_boundary_failed")
    if not no_dispatch_safety_ok:
        report_blockers.append("no_dispatch_safety_failed")

    agents = summary.get("agents") if isinstance(summary.get("agents"), dict) else {}
    opencoder_outputs = agents.get("opencoder_agent", {}).get("outputs", {})
    repair_result = opencoder_outputs.get("repair_result", {}) if isinstance(opencoder_outputs.get("repair_result"), dict) else {}
    updated_repair_result = _update_repair_result_for_external_validation(
        repair_result,
        validation_report_path,
        validation_passed=not report_blockers,
        blockers=report_blockers,
    )

    report = {
        "schema_version": WORKPLACE_PATCH_VALIDATION_SCHEMA_VERSION,
        "status": "validated" if not report_blockers else "blocked",
        "workplace_root": str(normalized_root),
        "target_file": target_file,
        "target_file_exists": target_file_exists,
        "import_ok": import_ok,
        "smoke_ok": smoke_ok,
        "claim_boundary_ok": claim_boundary_ok,
        "patch_fingerprint_ok": patch_fingerprint_ok,
        "no_dispatch_safety_ok": no_dispatch_safety_ok,
        "blockers": sorted(set(report_blockers)),
        "claim_boundary_checks": claim_boundary_checks,
        "no_dispatch_checks": no_dispatch_checks,
        "expected_file_sha256": expected_file_sha256,
        "actual_file_sha256": actual_file_sha256,
        "repair_execution_status_after_validation": updated_repair_result.get("execution_status"),
        "smoke_result_preview": smoke_result_preview,
        "import_error": import_error,
        "side_effects": {
            "source_edit": False,
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "remote_execution": False,
            "api_call": False,
        },
    }
    _write_json(validation_report_path, report)

    if lifecycle_repair_path.exists():
        _write_json(lifecycle_repair_path, updated_repair_result)

    if write_summary:
        updated_summary = copy.deepcopy(summary)
        updated_outputs = updated_summary["agents"]["opencoder_agent"]["outputs"]
        updated_outputs["repair_result"] = updated_repair_result
        updated_outputs["validation_report"] = report
        updated_outputs["validation_report_path"] = str(validation_report_path)
        updated_summary["next_recommendation"] = (
            "run_repair_scheduler_gate_then_stage_rerun_after_admission"
            if not report_blockers
            else "apply_patch_in_target_workplace_then_validate_import_and_stage_rerun"
        )
        if summary_path is not None:
            _write_json(summary_path, updated_summary)
        elif output_path.joinpath("three_agent_flow_summary.json").exists():
            _write_json(output_path / "three_agent_flow_summary.json", updated_summary)
        summary.clear()
        summary.update(updated_summary)

    return report


def _default_available_tests(repair_type: str) -> List[str]:
    if repair_type == "sandbox_thin_wrapper_missing":
        return [
            "sandbox_wrapper_import_test",
            "sandbox_wrapper_help_or_dry_run",
            "claim_boundary_test",
        ]
    if repair_type == "sandbox_cli_flag_integration":
        return [
            "sandbox_wrapper_help_or_dry_run",
            "mission_gate_test",
            "claim_boundary_test",
        ]
    if repair_type == "sandbox_probe_script_missing":
        return [
            "sandbox_probe_import_test",
            "sandbox_probe_help_or_dry_run",
            "claim_boundary_test",
        ]
    return []


def rehearse_three_agent_flow(
    *,
    mission_id: str,
    proposal_run_id: str,
    candidate_id: str,
    source_task_id: str,
    result_dir: Path | str,
    source_task_path: Path | str,
    downstream_task_id: str,
    output_dir: Path | str,
    available_tests: List[str] | None = None,
) -> Dict[str, Any]:
    """Run a side-effect-bounded three-agent handoff rehearsal.

    This deliberately does not dispatch scheduler work, apply source patches, or
    run remote jobs. It proves that the agents can hand structured artifacts to
    each other in the intended order.
    """

    output_path = Path(output_dir)
    artifacts = Path(result_dir)
    decision_summary = _read_json(artifacts / "decision_summary.json")
    action_requests = _read_action_requests(artifacts / "action_requests.json")

    source_repair_plan = build_source_repair_plan_from_taskbook(
        source_task_path,
        downstream_task_id=downstream_task_id,
    )
    source_repair_write = write_source_repair_plan(source_repair_plan)
    progression = assess_line_progression(
        source_task_id=source_task_id,
        decision_summary=decision_summary,
        action_requests=action_requests,
        source_repair_plan=source_repair_plan,
    )

    repair_type = _normalize(source_repair_plan.get("repair_type"))
    patch_intent = source_repair_plan.get("patch_intent") if isinstance(source_repair_plan.get("patch_intent"), dict) else {}
    target_file = _normalize(patch_intent.get("target_file")) if isinstance(patch_intent, dict) else ""
    tests = sorted(set(available_tests or _default_available_tests(repair_type)))
    repair_decision = select_repair_executor(
        source_repair_plan,
        repair_type=repair_type,
        target_files=[target_file] if target_file else None,
        available_tests=tests,
    )
    repair_route_blockers = repair_decision.get("blockers") or []
    repair_route_blockers = sorted(set([_normalize(item) for item in repair_route_blockers]))
    patch_artifact = materialize_source_repair_patch(
        source_repair_plan,
        output_dir=output_path / "opencoder_patch_artifact",
    )
    lifecycle = write_repair_execution_artifacts(
        repair_decision,
        source_repair_plan=source_repair_plan,
        output_dir=output_path / "opencoder_lifecycle",
    )
    repair_result = _read_json(Path(lifecycle.get("repair_result_path", ""))) if lifecycle.get("repair_result_path") else {}
    rerun_intent = _read_json(Path(lifecycle.get("rerun_intent_path", ""))) if lifecycle.get("rerun_intent_path") else {}

    work_order = {
        "mission_id": mission_id,
        "proposal_run_id": proposal_run_id,
        "candidate_id": candidate_id,
        "parent_decision_id": _normalize(progression.get("source_task_id")) or source_task_id,
        "decision_id": f"{mission_id}:{candidate_id}:three_agent_rehearsal",
        "continuation_lane": "night_auto",
        "mission_gate_pass": bool(
            progression.get("continue_allowed")
            and repair_decision.get("decision", {}).get("route") in {"bounded_self_repair", "deterministic_self"}
            and patch_artifact.get("ok") is True
        ),
        "status": "pending_scheduler_review",
        "work_order_id": f"{mission_id}_{candidate_id}_rerun",
        "source_decision": {
            "mission_id": mission_id,
            "candidate_id": candidate_id,
            "source_task_id": source_task_id,
            "next_step": progression.get("next_step"),
            "repair_route": repair_decision.get("decision", {}).get("route"),
            "repair_route_blockers": repair_route_blockers,
            "patch_artifact_ok": bool(patch_artifact.get("ok")),
            "rerun_intent": rerun_intent,
        },
        "claim_limits": [
            "no_scheduler_dispatch",
            "no_active_inbox_write",
            "no_registry_mutation",
            "no_remote_api_call",
            "no_dataset_or_checkpoint_mutation",
            "claim_upgrade_via_human_only",
        ],
        "side_effects": {
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "remote_api_call": False,
            "dataset_or_checkpoint_mutation": False,
        },
    }
    pending = write_night_auto_pending_staging_artifact(
        work_order,
        proposal_run_id=proposal_run_id,
        actor="three_agent_flow",
        scheduler_visible=False,
    )
    scheduler_explain = explain_night_auto_pending_scheduler_dry_run(
        mission_id=mission_id,
        proposal_run_id=proposal_run_id,
        candidate_id=candidate_id,
    )

    summary = {
        "schema_version": THREE_AGENT_FLOW_SCHEMA_VERSION,
        "status": "completed",
        "mission_id": mission_id,
        "source_task_id": source_task_id,
        "downstream_task_id": downstream_task_id,
        "agents": {
            "discussion_continuation_agent": {
                "status": "completed",
                "outputs": {
                    "line_progression": progression,
                    "source_repair_plan_path": source_repair_write.get("plan_path"),
                    "patch_intent_path": source_repair_write.get("patch_intent_path"),
                },
            },
            "opencoder_agent": {
                "status": "completed",
                "outputs": {
                    "repair_route": repair_decision.get("decision", {}).get("route"),
                    "repair_route_blockers": repair_route_blockers,
                    "repair_decision": repair_decision,
                    "patch_artifact": patch_artifact,
                    "repair_result": repair_result,
                    "rerun_intent": rerun_intent,
                },
            },
            "scheduler_agent": {
                "status": "dry_run_completed",
                "outputs": {
                    "pending_artifact_path": pending.get("pending_artifact_path"),
                    "scheduler_explain": scheduler_explain,
                },
            },
        },
        "handoff_order": [
            "discussion_continuation_agent",
            "opencoder_agent",
            "scheduler_agent",
            "discussion_continuation_agent",
        ],
        "side_effects": {
            "source_edit": False,
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "remote_execution": False,
            "api_call": False,
        },
        "next_recommendation": (
            "apply_patch_in_target_workplace_then_validate_import_and_stage_rerun"
            if patch_artifact.get("ok") is True
            else "return_to_discussion_continuation_for_repair_revision"
        ),
    }
    _write_json(output_path / "three_agent_flow_summary.json", summary)
    return summary


def assess_repair_to_scheduler_gate(
    *,
    discussion_decision: Dict[str, Any],
    patch_artifact: Dict[str, Any],
    repair_result: Dict[str, Any],
    rerun_intent: Dict[str, Any],
    validation_report: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Decide whether a repaired continuation may become scheduler-stageable."""

    validation = validation_report if isinstance(validation_report, dict) else {}
    blockers: List[str] = []
    continue_allowed = bool(discussion_decision.get("continue_allowed"))
    if not continue_allowed:
        blockers.append("discussion_did_not_allow_continuation")
    if _normalize(discussion_decision.get("requires_human")).lower() == "true" or discussion_decision.get("requires_human") is True:
        blockers.append("discussion_requires_human")
    if patch_artifact.get("ok") is not True:
        blockers.append("patch_artifact_not_ok")

    execution_status = _normalize(repair_result.get("execution_status")).lower()
    if execution_status not in {"applied", "externally_applied"}:
        blockers.append("repair_not_applied")
    if _normalize(repair_result.get("route")).lower() not in {"bounded_self_repair", "deterministic_self"}:
        blockers.append("repair_route_not_self_repair")

    if _normalize(rerun_intent.get("downstream_task_id")) == "":
        blockers.append("rerun_intent_missing_downstream_task")
    if _normalize(rerun_intent.get("repair_id")) and _normalize(repair_result.get("repair_id")):
        if _normalize(rerun_intent.get("repair_id")) != _normalize(repair_result.get("repair_id")):
            blockers.append("repair_rerun_id_mismatch")

    if validation:
        if validation.get("import_ok") is not True:
            blockers.append("validation_import_not_ok")
        if validation.get("smoke_ok") is not True:
            blockers.append("validation_smoke_not_ok")
        if validation.get("claim_boundary_ok") is not True:
            blockers.append("validation_claim_boundary_not_ok")
        if validation.get("no_dispatch_safety_ok") is not True:
            blockers.append("validation_no_dispatch_safety_not_ok")
    else:
        blockers.append("validation_report_missing")

    stageable = not blockers
    return {
        "schema_version": REPAIR_TO_SCHEDULER_GATE_SCHEMA_VERSION,
        "status": "stageable" if stageable else "blocked",
        "stageable": stageable,
        "blockers": sorted(set(blockers)),
        "leader": "discussion_continuation_agent",
        "agent_roles": {
            "discussion_continuation_agent": "owns continue/stop judgment",
            "opencoder_agent": "owns applied repair and validation evidence",
            "scheduler_agent": "owns admission and dispatch after gate pass",
        },
        "scheduler_instruction": (
            "may_stage_downstream_rerun_after_admission"
            if stageable
            else "keep_scheduler_dry_run_blocked"
        ),
        "side_effects": {
            "source_edit": False,
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "remote_execution": False,
            "api_call": False,
        },
    }


def assess_repair_to_scheduler_gate_from_summary(
    summary: Dict[str, Any],
    *,
    validation_report: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    agents = summary.get("agents") if isinstance(summary.get("agents"), dict) else {}
    discussion_outputs = agents.get("discussion_continuation_agent", {}).get("outputs", {})
    opencoder_outputs = agents.get("opencoder_agent", {}).get("outputs", {})
    resolved_validation = (
        validation_report
        if isinstance(validation_report, dict) and validation_report
        else _load_validation_report_from_summary(summary)
    )
    return assess_repair_to_scheduler_gate(
        discussion_decision=discussion_outputs.get("line_progression", {}),
        patch_artifact=opencoder_outputs.get("patch_artifact", {}),
        repair_result=opencoder_outputs.get("repair_result", {}),
        rerun_intent=opencoder_outputs.get("rerun_intent", {}),
        validation_report=resolved_validation,
    )


def assess_three_agent_autonomy(
    summary: Dict[str, Any],
    *,
    repair_scheduler_gate: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Assess whether the three-agent chain is ready to keep progressing.

    This is deliberately a local judgment artifact. It does not apply patches,
    dispatch scheduler work, call APIs, or mutate registry state.
    """

    agents = summary.get("agents") if isinstance(summary.get("agents"), dict) else {}
    discussion_outputs = agents.get("discussion_continuation_agent", {}).get("outputs", {})
    opencoder_outputs = agents.get("opencoder_agent", {}).get("outputs", {})
    scheduler_outputs = agents.get("scheduler_agent", {}).get("outputs", {})
    line_progression = discussion_outputs.get("line_progression", {})
    repair_decision = opencoder_outputs.get("repair_decision", {})
    patch_artifact = opencoder_outputs.get("patch_artifact", {})
    repair_result = opencoder_outputs.get("repair_result", {})
    rerun_intent = opencoder_outputs.get("rerun_intent", {})
    scheduler_explain = scheduler_outputs.get("scheduler_explain", {})

    gate = repair_scheduler_gate if isinstance(repair_scheduler_gate, dict) else {}
    if not gate:
        gate = assess_repair_to_scheduler_gate_from_summary(summary)

    blockers = set(gate.get("blockers") if isinstance(gate.get("blockers"), list) else [])
    warnings: List[str] = []
    missing_knowledge: List[str] = []

    continue_allowed = bool(line_progression.get("continue_allowed"))
    requires_human = bool(line_progression.get("requires_human"))
    next_step = _normalize(line_progression.get("next_step"))
    route = _normalize(repair_decision.get("decision", {}).get("route") or opencoder_outputs.get("repair_route"))
    patch_ok = patch_artifact.get("ok") is True
    execution_status = _normalize(repair_result.get("execution_status")).lower()
    validation_missing = "validation_report_missing" in blockers
    repair_not_applied = "repair_not_applied" in blockers
    stageable = bool(gate.get("stageable"))
    scheduler_eligible = bool(scheduler_explain.get("eligible"))

    if _normalize(summary.get("source_task_id")) == "":
        missing_knowledge.append("source_task_id")
    if _normalize(summary.get("downstream_task_id")) == "":
        missing_knowledge.append("downstream_task_id")
    if next_step == "":
        missing_knowledge.append("line_progression.next_step")
    if route == "":
        missing_knowledge.append("opencoder.repair_route")
    if not isinstance(rerun_intent, dict) or _normalize(rerun_intent.get("downstream_task_id")) == "":
        missing_knowledge.append("rerun_intent.downstream_task_id")

    project_understanding_score = 5.0
    if _normalize(summary.get("mission_id")):
        project_understanding_score += 0.7
    if _normalize(summary.get("source_task_id")) and _normalize(summary.get("downstream_task_id")):
        project_understanding_score += 1.0
    if continue_allowed and next_step:
        project_understanding_score += 1.0
    if route in {"bounded_self_repair", "deterministic_self"}:
        project_understanding_score += 0.8
    if patch_ok and rerun_intent:
        project_understanding_score += 0.7
    if summary.get("handoff_order") == [
        "discussion_continuation_agent",
        "opencoder_agent",
        "scheduler_agent",
        "discussion_continuation_agent",
    ]:
        project_understanding_score += 0.5
    project_understanding_score = min(10.0, project_understanding_score)

    knowledge_sufficiency_score = 4.5
    knowledge_sufficiency_score += max(0, 5 - len(missing_knowledge)) * 0.5
    if patch_ok:
        knowledge_sufficiency_score += 0.8
    if route in {"bounded_self_repair", "deterministic_self"}:
        knowledge_sufficiency_score += 0.7
    if line_progression.get("requires_codex") is False:
        knowledge_sufficiency_score += 0.4
    if requires_human:
        knowledge_sufficiency_score -= 1.0
    knowledge_sufficiency_score = max(0.0, min(10.0, knowledge_sufficiency_score))

    model_playthrough_readiness_score = 3.0
    if continue_allowed:
        model_playthrough_readiness_score += 1.0
    if patch_ok:
        model_playthrough_readiness_score += 1.0
    if execution_status in {"applied", "externally_applied"}:
        model_playthrough_readiness_score += 1.5
    if not validation_missing:
        model_playthrough_readiness_score += 1.0
    if stageable:
        model_playthrough_readiness_score += 2.0
    if repair_not_applied:
        model_playthrough_readiness_score -= 0.8
    model_playthrough_readiness_score = max(0.0, min(10.0, model_playthrough_readiness_score))

    can_continue_without_codex = (
        continue_allowed
        and route in {"bounded_self_repair", "deterministic_self"}
        and patch_ok
        and not requires_human
    )
    can_continue_without_human = can_continue_without_codex and not any(
        item in blockers
        for item in {
            "discussion_requires_human",
            "discussion_did_not_allow_continuation",
            "repair_route_not_self_repair",
        }
    )
    can_play_through_model = stageable and execution_status in {"applied", "externally_applied"}

    if scheduler_eligible and (repair_not_applied or validation_missing):
        autonomy_style = "overconfident"
        warnings.append("scheduler_eligible_before_repair_validation")
    elif requires_human:
        autonomy_style = "blocked_needs_human"
    elif stageable:
        autonomy_style = "mission_bounded_ready"
    elif can_continue_without_codex and (repair_not_applied or validation_missing):
        autonomy_style = "mission_bounded_cautious"
    elif continue_allowed and route not in {"bounded_self_repair", "deterministic_self"}:
        autonomy_style = "codex_dependent"
    elif continue_allowed and not patch_ok:
        autonomy_style = "repair_planning_incomplete"
    else:
        autonomy_style = "overcautious"

    if repair_not_applied:
        next_autonomous_step = "apply_or_mark_external_patch_then_run_import_smoke_claim_validation"
    elif validation_missing:
        next_autonomous_step = "run_import_smoke_claim_boundary_validation"
    elif stageable:
        next_autonomous_step = "stage_downstream_rerun_after_scheduler_admission"
    elif not continue_allowed:
        next_autonomous_step = "return_to_discussion_for_continue_stop_judgment"
    else:
        next_autonomous_step = "repair_plan_revision_or_missing_artifact_resolution"

    return {
        "schema_version": AUTONOMY_ASSESSMENT_SCHEMA_VERSION,
        "status": "completed",
        "leader": "discussion_continuation_agent",
        "can_play_through_model": can_play_through_model,
        "can_continue_without_codex": can_continue_without_codex,
        "can_continue_without_human": can_continue_without_human,
        "autonomy_style": autonomy_style,
        "scores": {
            "project_understanding": round(project_understanding_score, 1),
            "knowledge_sufficiency": round(knowledge_sufficiency_score, 1),
            "model_playthrough_readiness": round(model_playthrough_readiness_score, 1),
        },
        "next_autonomous_step": next_autonomous_step,
        "blockers": sorted(set(blockers)),
        "missing_knowledge": sorted(set(missing_knowledge)),
        "warnings": sorted(set(warnings)),
        "interpretation": {
            "summary": (
                "The chain can keep progressing inside the mission envelope, but it is not model-run ready yet."
                if can_continue_without_codex and not can_play_through_model
                else "The chain is ready to stage the downstream rerun after scheduler admission."
                if can_play_through_model
                else "The chain lacks enough validated repair evidence to continue autonomously."
            ),
            "style_explanation": {
                "mission_bounded_cautious": "continues bounded repair/validation, blocks model run until evidence exists",
                "mission_bounded_ready": "validated repair evidence exists and scheduler admission may proceed",
                "overconfident": "would expose scheduler/model run before repair validation",
                "overcautious": "stops despite available bounded continuation path",
                "codex_dependent": "routes continuation to Codex before bounded self-repair is exhausted",
                "repair_planning_incomplete": "continuation is allowed but patch evidence is incomplete",
                "blocked_needs_human": "discussion judgment explicitly crossed a human boundary",
            }.get(autonomy_style, ""),
        },
        "side_effects": {
            "source_edit": False,
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "remote_execution": False,
            "api_call": False,
        },
    }


def rehearse_scheduler_admission_after_repair(
    summary: Dict[str, Any],
    *,
    repair_scheduler_gate: Dict[str, Any] | None = None,
    output_dir: Path | str | None = None,
    summary_path: Path | None = None,
) -> Dict[str, Any]:
    """Build a local scheduler-admission rehearsal packet after repair validation.

    The packet proves the handoff can move from Discussion/OpenCoder evidence to
    Scheduler admission. It never writes active inbox, flips scheduler visibility,
    mutates registry state, dispatches, or launches the downstream task.
    """

    output_path = _summary_output_dir(summary_path, output_dir)
    admission_path = output_path / "scheduler_admission_rehearsal.json"
    gate = repair_scheduler_gate if isinstance(repair_scheduler_gate, dict) else {}
    if not gate:
        gate = assess_repair_to_scheduler_gate_from_summary(summary)

    agents = summary.get("agents") if isinstance(summary.get("agents"), dict) else {}
    discussion_outputs = agents.get("discussion_continuation_agent", {}).get("outputs", {})
    opencoder_outputs = agents.get("opencoder_agent", {}).get("outputs", {})
    scheduler_outputs = agents.get("scheduler_agent", {}).get("outputs", {})
    line_progression = discussion_outputs.get("line_progression", {})
    repair_result = opencoder_outputs.get("repair_result", {})
    rerun_intent = opencoder_outputs.get("rerun_intent", {})
    validation_report = _load_validation_report_from_summary(summary)
    scheduler_explain = scheduler_outputs.get("scheduler_explain", {})
    next_step_readiness = _coerce_optional_next_step_readiness(
        scheduler_explain.get("next_step_readiness"),
        line_progression.get("next_step_readiness"),
        {
            "can_stage": line_progression.get("can_stage"),
            "route": line_progression.get("route"),
            "status": line_progression.get("status"),
            "blockers": line_progression.get("blockers"),
            "bounded_validation": line_progression.get("bounded_validation_readiness"),
        },
    )

    blockers: List[str] = []
    if gate.get("stageable") is not True:
        blockers.append("repair_scheduler_gate_not_stageable")
    if _normalize(gate.get("scheduler_instruction")) != "may_stage_downstream_rerun_after_admission":
        blockers.append("scheduler_instruction_not_stageable")
    if bool(line_progression.get("continue_allowed")) is not True:
        blockers.append("discussion_continuation_not_allowed")
    if _normalize(repair_result.get("execution_status")).lower() not in {"applied", "externally_applied"}:
        blockers.append("repair_result_not_applied")
    if _normalize(rerun_intent.get("downstream_task_id")) == "":
        blockers.append("rerun_intent_missing_downstream_task")
    for key, reason in (
        ("import_ok", "validation_import_not_ok"),
        ("smoke_ok", "validation_smoke_not_ok"),
        ("claim_boundary_ok", "validation_claim_boundary_not_ok"),
        ("no_dispatch_safety_ok", "validation_no_dispatch_safety_not_ok"),
    ):
        if validation_report.get(key) is not True:
            blockers.append(reason)
    if validation_report.get("patch_fingerprint_ok") is not True:
        blockers.append("validation_patch_fingerprint_not_ok")

    claim_limits = [
        "admission_rehearsal_only",
        "no_scheduler_dispatch",
        "no_active_inbox_write",
        "no_registry_mutation",
        "no_remote_execution",
        "no_dataset_or_checkpoint_mutation",
        "claim_upgrade_via_human_only",
    ]
    admission_stageable = not blockers
    packet = {
        "schema_version": SCHEDULER_ADMISSION_REHEARSAL_SCHEMA_VERSION,
        "status": "admission_ready" if admission_stageable else "blocked",
        "admission_stageable": admission_stageable,
        "mission_id": _normalize(summary.get("mission_id")),
        "source_task_id": _normalize(summary.get("source_task_id")),
        "downstream_task_id": _normalize(
            rerun_intent.get("downstream_task_id")
            or summary.get("downstream_task_id")
        ),
        "leader": "scheduler_agent",
        "handoff_from": "discussion_continuation_agent+opencoder_agent",
        "gate_status": gate.get("status"),
        "gate_blockers": gate.get("blockers", []),
        "blockers": sorted(set(blockers)),
        "admission_instruction": (
            "may_prepare_scheduler_admission_packet_without_dispatch"
            if admission_stageable
            else "keep_scheduler_admission_blocked"
        ),
        "required_evidence": {
            "discussion_continue_allowed": bool(line_progression.get("continue_allowed")),
            "repair_execution_status": _normalize(repair_result.get("execution_status")),
            "validation_status": _normalize(validation_report.get("status")),
            "patch_fingerprint_ok": validation_report.get("patch_fingerprint_ok") is True,
            "import_ok": validation_report.get("import_ok") is True,
            "smoke_ok": validation_report.get("smoke_ok") is True,
            "claim_boundary_ok": validation_report.get("claim_boundary_ok") is True,
            "no_dispatch_safety_ok": validation_report.get("no_dispatch_safety_ok") is True,
        },
        "scheduler_context": {
            "prior_pending_artifact_path": scheduler_outputs.get("pending_artifact_path"),
            "prior_scheduler_explain_status": scheduler_explain.get("status"),
            "prior_scheduler_explain_eligible": scheduler_explain.get("eligible"),
            "prior_scheduler_explain_reasons": scheduler_explain.get("reasons", []),
        },
        "next_step": (
            "create_admission_packet_or_task_draft_for_l72_without_dispatch"
            if admission_stageable
            else "return_to_discussion_or_opencoder_for_missing_evidence"
        ),
        "claim_limits": claim_limits,
        "side_effects": {
            "source_edit": False,
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "remote_execution": False,
            "api_call": False,
            "scheduler_visible_write": False,
        },
    }
    if next_step_readiness:
        packet["next_step_readiness"] = next_step_readiness
    _write_json(admission_path, packet)
    return packet


def build_l72_task_admission_packet(
    *,
    admission_rehearsal: Dict[str, Any],
    task_path: Path | str,
    output_dir: Path | str,
) -> Dict[str, Any]:
    """Bind a stageable admission rehearsal to an L72 task draft.

    This is a review/staging packet only. It copies no task into active inbox,
    does not promote pending state, and does not dispatch.
    """

    output_path = Path(output_dir)
    packet_path = output_path / "l72_task_admission_packet.json"
    draft_path = output_path / "l72_task_draft.md"
    source_path = Path(task_path)
    blockers: List[str] = []

    if admission_rehearsal.get("admission_stageable") is not True:
        blockers.append("admission_rehearsal_not_stageable")
    if _normalize(admission_rehearsal.get("status")) != "admission_ready":
        blockers.append("admission_rehearsal_not_ready")

    try:
        task_text = source_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        task_text = ""
        blockers.append("taskbook_missing")

    front = parse_task_front_matter(task_text)
    if not front:
        blockers.append("taskbook_front_matter_missing")

    downstream_task_id = _normalize(admission_rehearsal.get("downstream_task_id"))
    task_id = _normalize(front.get("task_id")) if front else ""
    if downstream_task_id and task_id != downstream_task_id:
        blockers.append("task_id_does_not_match_admission_downstream")

    if _normalize(front.get("worker")) != "materialized_execution":
        blockers.append("worker_not_materialized_execution")
    if _normalize(front.get("resource_hint")).lower() != "gpu":
        blockers.append("resource_hint_not_gpu")
    if _normalize(front.get("claim_level")).lower() != "exploratory":
        blockers.append("claim_level_not_exploratory")
    if front.get("formal_claim_allowed") is not False:
        blockers.append("formal_claim_not_explicitly_disallowed")

    execution = front.get("execution") if isinstance(front.get("execution"), dict) else {}
    if _normalize(execution.get("script_id")) != "b14_large_image_bbox_sidecar_smoke":
        blockers.append("unexpected_or_missing_l72_script_id")
    if execution.get("gpu_required") is not True:
        blockers.append("execution_gpu_required_not_true")
    if int(execution.get("gpu_count") or 0) < 1:
        blockers.append("execution_gpu_count_missing")

    target_workplace = front.get("target_workplace") if isinstance(front.get("target_workplace"), dict) else {}
    for key in ("profile", "host", "remote_workdir", "remote_root"):
        if not _normalize(target_workplace.get(key)):
            blockers.append(f"target_workplace_missing_{key}")

    depends_on = front.get("depends_on") if isinstance(front.get("depends_on"), list) else []
    required_deps = {
        "b14_l70_large_image_coordinate_protocol_audit",
        "b14_l71_bbox_sidecar_large_image_cli_integration",
    }
    if not required_deps.issubset({str(item) for item in depends_on}):
        blockers.append("l72_missing_required_dependencies")

    if task_text:
        output_path.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(task_text, encoding="utf-8")

    next_step_readiness = _coerce_optional_next_step_readiness(
        admission_rehearsal.get("next_step_readiness"),
        admission_rehearsal.get("scheduler_context", {}).get("next_step_readiness")
        if isinstance(admission_rehearsal.get("scheduler_context"), dict)
        else None,
    )

    packet = {
        "schema_version": L72_ADMISSION_PACKET_SCHEMA_VERSION,
        "status": "packet_ready" if not blockers else "blocked",
        "packet_ready": not blockers,
        "task_id": task_id,
        "source_task_path": str(source_path),
        "task_draft_path": str(draft_path) if task_text else "",
        "admission_rehearsal_status": admission_rehearsal.get("status"),
        "admission_stageable": admission_rehearsal.get("admission_stageable") is True,
        "blockers": sorted(set(blockers)),
        "scheduler_instruction": (
            "review_or_stage_l72_task_without_dispatch"
            if not blockers
            else "keep_l72_task_admission_blocked"
        ),
        "validated_handoff": {
            "mission_id": admission_rehearsal.get("mission_id"),
            "source_task_id": admission_rehearsal.get("source_task_id"),
            "downstream_task_id": downstream_task_id,
            "required_evidence": admission_rehearsal.get("required_evidence", {}),
        },
        "task_contract": {
            "worker": front.get("worker") if front else "",
            "resource_hint": front.get("resource_hint") if front else "",
            "resource_lane": front.get("resource_lane") if front else "",
            "execution_stage": front.get("execution_stage") if front else "",
            "script_id": execution.get("script_id") if execution else "",
            "target_workplace": target_workplace,
            "depends_on": depends_on,
            "claim_level": front.get("claim_level") if front else "",
            "formal_claim_allowed": front.get("formal_claim_allowed") if front else None,
        },
        "claim_limits": [
            "task_admission_packet_only",
            "no_scheduler_dispatch",
            "no_active_inbox_write",
            "no_registry_mutation",
            "no_remote_execution",
            "claim_upgrade_via_human_only",
        ],
        "side_effects": {
            "source_edit": False,
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "remote_execution": False,
            "api_call": False,
            "pending_state_mutation": False,
        },
    }
    if next_step_readiness:
        packet["validated_handoff"]["next_step_readiness"] = next_step_readiness
        packet["next_step_readiness"] = next_step_readiness
    _write_json(packet_path, packet)
    return packet


def dry_run_l72_task_draft_scheduler_admission(
    *,
    admission_packet: Dict[str, Any],
    output_dir: Path | str,
) -> Dict[str, Any]:
    """Explain scheduler readiness for the L72 draft without making it visible."""

    output_path = Path(output_dir)
    report_path = output_path / "l72_task_draft_scheduler_dry_run.json"
    blockers: List[str] = []
    checks: List[Dict[str, Any]] = []

    task_draft_path = Path(_normalize(admission_packet.get("task_draft_path")))
    if admission_packet.get("packet_ready") is not True:
        blockers.append("admission_packet_not_ready")
    if _normalize(admission_packet.get("status")) != "packet_ready":
        blockers.append("admission_packet_status_not_ready")
    if not task_draft_path.exists():
        blockers.append("task_draft_missing")
        task_text = ""
    else:
        task_text = task_draft_path.read_text(encoding="utf-8-sig")

    front = parse_task_front_matter(task_text)
    if not front:
        blockers.append("task_draft_front_matter_missing")
        front = {}

    def add_check(check_id: str, passed: bool, **payload: Any) -> None:
        check = {"id": check_id, "status": "passed" if passed else "failed"}
        check.update(payload)
        checks.append(check)

    task_id = _normalize(front.get("task_id"))
    expected_task_id = _normalize(admission_packet.get("task_id"))
    add_check("task_id_matches_packet", bool(task_id and task_id == expected_task_id), task_id=task_id, expected=expected_task_id)
    if task_id != expected_task_id:
        blockers.append("task_id_mismatch")

    worker_ok = _normalize(front.get("worker")) == "materialized_execution"
    add_check("worker_materialized_execution", worker_ok, worker=front.get("worker", ""))
    if not worker_ok:
        blockers.append("worker_not_materialized_execution")

    resource_ok = _normalize(front.get("resource_hint")).lower() == "gpu"
    add_check("resource_hint_gpu", resource_ok, resource_hint=front.get("resource_hint", ""))
    if not resource_ok:
        blockers.append("resource_hint_not_gpu")

    execution = front.get("execution") if isinstance(front.get("execution"), dict) else {}
    script_ok = _normalize(execution.get("script_id")) == "b14_large_image_bbox_sidecar_smoke"
    add_check("script_id_allowlisted_for_l72", script_ok, script_id=execution.get("script_id", ""))
    if not script_ok:
        blockers.append("script_id_not_l72_smoke")

    gpu_ok = execution.get("gpu_required") is True and int(execution.get("gpu_count") or 0) >= 1
    add_check("execution_gpu_contract", gpu_ok, gpu_required=execution.get("gpu_required"), gpu_count=execution.get("gpu_count"))
    if not gpu_ok:
        blockers.append("gpu_contract_invalid")

    target_workplace = front.get("target_workplace") if isinstance(front.get("target_workplace"), dict) else {}
    workplace_ok = all(_normalize(target_workplace.get(key)) for key in ("profile", "host", "remote_workdir", "remote_root"))
    add_check("target_workplace_complete", workplace_ok, target_workplace=target_workplace)
    if not workplace_ok:
        blockers.append("target_workplace_incomplete")

    deps = {str(item) for item in front.get("depends_on", [])} if isinstance(front.get("depends_on"), list) else set()
    deps_ok = {
        "b14_l70_large_image_coordinate_protocol_audit",
        "b14_l71_bbox_sidecar_large_image_cli_integration",
    }.issubset(deps)
    add_check("required_dependencies_present", deps_ok, depends_on=sorted(deps))
    if not deps_ok:
        blockers.append("required_dependencies_missing")

    claim_ok = _normalize(front.get("claim_level")).lower() == "exploratory" and front.get("formal_claim_allowed") is False
    add_check("claim_boundary_exploratory_only", claim_ok, claim_level=front.get("claim_level"), formal_claim_allowed=front.get("formal_claim_allowed"))
    if not claim_ok:
        blockers.append("claim_boundary_invalid")

    packet_claim_limits = set(str(item) for item in admission_packet.get("claim_limits", []) if str(item))
    active_write_blocked = "no_active_inbox_write" in packet_claim_limits
    dispatch_blocked = "no_scheduler_dispatch" in packet_claim_limits
    add_check("packet_keeps_no_active_inbox_write", active_write_blocked)
    add_check("packet_keeps_no_scheduler_dispatch", dispatch_blocked)
    if not active_write_blocked:
        blockers.append("packet_missing_no_active_inbox_write")
    if not dispatch_blocked:
        blockers.append("packet_missing_no_scheduler_dispatch")

    next_step_readiness = _coerce_optional_next_step_readiness(
        admission_packet.get("next_step_readiness"),
        admission_packet.get("validated_handoff", {}).get("next_step_readiness")
        if isinstance(admission_packet.get("validated_handoff"), dict)
        else None,
    )

    report = {
        "schema_version": L72_DRAFT_SCHEDULER_DRY_RUN_SCHEMA_VERSION,
        "status": "dry_run_ready" if not blockers else "blocked",
        "dry_run_ready": not blockers,
        "task_id": task_id,
        "task_draft_path": str(task_draft_path),
        "scheduler_instruction": (
            "draft_contract_can_be_staged_after_explicit_promotion_boundary"
            if not blockers
            else "keep_l72_draft_blocked"
        ),
        "blockers": sorted(set(blockers)),
        "checks": checks,
        "selector_preview": {
            "worker": front.get("worker", ""),
            "line": front.get("line", ""),
            "batch": front.get("batch", ""),
            "dag_root": front.get("dag_root", ""),
            "resource_hint": front.get("resource_hint", ""),
            "resource_lane": front.get("resource_lane", ""),
            "target_workplace": target_workplace,
        },
        "side_effects": {
            "source_edit": False,
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "remote_execution": False,
            "api_call": False,
            "pending_state_mutation": False,
        },
    }
    if next_step_readiness:
        report["next_step_readiness"] = next_step_readiness
    _write_json(report_path, report)
    return report


def stage_l72_task_draft_candidate(
    *,
    admission_packet: Dict[str, Any],
    draft_dry_run: Dict[str, Any],
    output_dir: Path | str,
    proposal_run_id: str = "b14_l72_task_draft_stage",
    actor: str = "three_agent_flow",
) -> Dict[str, Any]:
    """Bridge a ready L72 draft into proposal staging without activation.

    This uses the existing proposal staging/night-auto dry-run path. It creates
    reviewable local staging artifacts only; it does not write active inbox,
    expose scheduler-visible work, mutate registry state, or launch remote work.
    """

    output_path = Path(output_dir)
    report_path = output_path / "l72_staging_candidate_report.json"
    blockers: List[str] = []

    if admission_packet.get("packet_ready") is not True:
        blockers.append("admission_packet_not_ready")
    if _normalize(admission_packet.get("status")) != "packet_ready":
        blockers.append("admission_packet_status_not_ready")
    if draft_dry_run.get("dry_run_ready") is not True:
        blockers.append("draft_scheduler_dry_run_not_ready")
    if _normalize(draft_dry_run.get("status")) != "dry_run_ready":
        blockers.append("draft_scheduler_dry_run_status_not_ready")

    task_id = _normalize(admission_packet.get("task_id") or draft_dry_run.get("task_id"))
    if task_id != "b14_l72_large_image_bbox_sidecar_smoke":
        blockers.append("task_id_not_l72_sidecar_smoke")

    contract = admission_packet.get("task_contract") if isinstance(admission_packet.get("task_contract"), dict) else {}
    selector = draft_dry_run.get("selector_preview") if isinstance(draft_dry_run.get("selector_preview"), dict) else {}
    line = _normalize(selector.get("line") or "B14_spatial_grace_graph_utility")
    workplace_profile = _normalize(
        (contract.get("target_workplace") or {}).get("profile")
        if isinstance(contract.get("target_workplace"), dict)
        else ""
    ) or "EXAMPLE_WORKER_A_B14"
    source = admission_packet.get("validated_handoff") if isinstance(admission_packet.get("validated_handoff"), dict) else {}
    source_task_id = _normalize(source.get("source_task_id")) or "b14_l70_large_image_coordinate_protocol_audit"
    mission_id = _normalize(source.get("mission_id")) or "B14_night_research_v1"
    next_step_readiness = _coerce_optional_next_step_readiness(
        draft_dry_run.get("next_step_readiness"),
        source.get("next_step_readiness"),
        {
            "can_stage": source.get("can_stage"),
            "route": source.get("route"),
            "status": source.get("status"),
            "blockers": source.get("blockers"),
            "bounded_validation": source.get("bounded_validation_readiness"),
        },
    ) or {
        "schema_version": 1,
        "can_stage": True,
        "route": "scheduler_admission",
        "blockers": [],
    }

    if blockers:
        report = {
            "schema_version": L72_STAGING_CANDIDATE_SCHEMA_VERSION,
            "status": "blocked",
            "staging_candidate_ready": False,
            "task_id": task_id,
            "proposal_run_id": proposal_run_id,
            "blockers": sorted(set(blockers)),
            "staging_result": None,
            "side_effects": {
                "source_edit": False,
                "scheduler_dispatch": False,
                "active_inbox_write": False,
                "registry_mutation": False,
                "remote_execution": False,
                "api_call": False,
                "scheduler_visible_write": False,
            },
        }
        _write_json(report_path, report)
        return report

    candidate = {
        "schema_version": 1,
        "candidate_id": task_id,
        "source_council_id": "b14_l70_l73_three_agent_rehearsal",
        "source_task_id": source_task_id,
        "source_knowledge_pack_id": "b14_agent_handoff_20260521",
        "kind": "bounded_probe",
        "worker": "materialized_execution",
        "line": line,
        "workplace_profile": workplace_profile,
        "reason": (
            "L72 sidecar smoke is the next bounded probe after L70 evidence, "
            "validated L71 adapter evidence, and scheduler admission dry-run."
        ),
        "risk_flags": ["gpu_required", "exploratory_only", "requires_explicit_scheduler_admission"],
        "claim_limitations": [
            "Exploratory bounded smoke only.",
            "May test sidecar pathway and coordinate evidence plumbing.",
            "Must not claim final localization quality or submission readiness.",
        ],
        "claim_limits": [
            "mission_metadata_required",
            "exploratory_evidence_only",
            "scheduler_visibility_blocked",
            "formal_claim_blocked",
        ],
        "expected_outputs": [
            "large_image_smoke_metrics.csv",
            "bbox_predictions.jsonl",
            "bbox_evidence_payload.jsonl",
            "mainline_invariance_check.csv",
            "coordinate_roundtrip_check.csv",
            "failure_cases.csv",
            "l72_large_image_bbox_sidecar_smoke_report.md",
            "decision_summary.json",
            "action_requests.json",
        ],
        "forbidden_actions": [
            "direct_dispatch",
            "scheduler_submit",
            "registry_mutation",
            "source_edit",
            "remote_path_guess",
            "manual_file_promotion",
        ],
        "status": "proposal_candidate_created",
        "can_stage": True,
        "requires_human_approval": False,
        "continuation_lane": "night_auto",
        "mission_id": mission_id,
        "parent_decision_id": "b14_l70_l73_real_pkg:l72_task_draft_scheduler_dry_run",
        "source_decision_id": "b14_l70_l73_real_pkg:l72_stage_candidate",
        "mission_gate_pass": True,
        "next_step_readiness": next_step_readiness,
        "side_effects": {
            "dispatch": False,
            "promote": False,
            "registry_mutation": False,
            "remote_execution": False,
            "running_state": False,
            "subprocess": False,
            "scheduler_submit": False,
            "scheduler_dispatch": False,
            "remote_write": False,
            "source_edit": False,
            "write_inbox": False,
        },
    }
    staging_result = stage_proposal_candidate_dry_run(
        candidate,
        actor=actor,
        proposal_run_id=proposal_run_id,
    )
    staging_blocked = _normalize(staging_result.get("status")).lower() == "blocked"
    report = {
        "schema_version": L72_STAGING_CANDIDATE_SCHEMA_VERSION,
        "status": "staging_ready" if not staging_blocked else "blocked",
        "staging_candidate_ready": not staging_blocked,
        "task_id": task_id,
        "proposal_run_id": proposal_run_id,
        "candidate": candidate,
        "blockers": sorted(set(staging_result.get("reasons", []) if staging_blocked else [])),
        "staging_result": staging_result,
        "scheduler_instruction": (
            "night_auto_pending_artifact_created_but_scheduler_invisible"
            if not staging_blocked
            else "keep_l72_staging_blocked"
        ),
        "side_effects": {
            "source_edit": False,
            "scheduler_dispatch": False,
            "active_inbox_write": False,
            "registry_mutation": False,
            "remote_execution": False,
            "api_call": False,
            "scheduler_visible_write": False,
        },
    }
    _write_json(report_path, report)
    return report


def build_l72_manual_launch_readiness_from_stage(
    *,
    admission_packet: Dict[str, Any],
    draft_dry_run: Dict[str, Any],
    staging_candidate_report: Dict[str, Any],
    output_dir: Path | str,
    actor: str = "three_agent_flow",
    approve_remote_launch: bool = True,
    operator_confirmed: bool = False,
) -> Dict[str, Any]:
    """Create local launch readiness artifacts for L72 from a staged candidate.

    This reaches only the manual launch preflight/readiness boundary. It does
    not run SSH, create subprocesses, dispatch scheduler work, mutate registry
    state, or launch the remote task.
    """

    output_path = Path(output_dir)
    report_path = output_path / "l72_manual_launch_readiness_report.json"
    work_order_path = output_path / "l72_manual_launch_work_order.json"
    blockers: List[str] = []

    if admission_packet.get("packet_ready") is not True:
        blockers.append("admission_packet_not_ready")
    if draft_dry_run.get("dry_run_ready") is not True:
        blockers.append("draft_scheduler_dry_run_not_ready")
    if staging_candidate_report.get("staging_candidate_ready") is not True:
        blockers.append("staging_candidate_not_ready")

    staging_result = staging_candidate_report.get("staging_result") if isinstance(staging_candidate_report.get("staging_result"), dict) else {}
    pending_artifact = staging_result.get("night_auto_pending_artifact") if isinstance(staging_result.get("night_auto_pending_artifact"), dict) else {}
    work_order = staging_result.get("night_auto_work_order") if isinstance(staging_result.get("night_auto_work_order"), dict) else {}
    if _normalize(staging_result.get("status")) != "staged":
        blockers.append("proposal_staging_not_staged")
    if not pending_artifact:
        blockers.append("night_auto_pending_artifact_missing")
    if pending_artifact and pending_artifact.get("scheduler_visible") is not False:
        blockers.append("pending_artifact_scheduler_visible_not_false")

    task_id = _normalize(admission_packet.get("task_id") or draft_dry_run.get("task_id"))
    if task_id != "b14_l72_large_image_bbox_sidecar_smoke":
        blockers.append("task_id_not_l72_sidecar_smoke")

    task_contract = admission_packet.get("task_contract") if isinstance(admission_packet.get("task_contract"), dict) else {}
    target_workplace = task_contract.get("target_workplace") if isinstance(task_contract.get("target_workplace"), dict) else {}
    selector = draft_dry_run.get("selector_preview") if isinstance(draft_dry_run.get("selector_preview"), dict) else {}
    script_id = _normalize(task_contract.get("script_id"))
    if script_id != "b14_large_image_bbox_sidecar_smoke":
        blockers.append("script_id_not_l72_smoke")

    remote_workdir = _normalize(target_workplace.get("remote_workdir"))
    remote_root = remote_workdir or _normalize(target_workplace.get("remote_root"))
    launch_work_order = {
        "work_order_id": _normalize(work_order.get("work_order_id")) or "l72_manual_launch_work_order",
        "source_work_order_hash": _normalize(work_order.get("idempotency_key")),
        "dispatch_request_id": f"{task_id}:manual_launch_preflight",
        "task_id": task_id,
        "workplace_profile": _normalize(target_workplace.get("profile")) or "EXAMPLE_WORKER_A_B14",
        "host": _normalize(target_workplace.get("host")) or "EXAMPLE_WORKER_A",
        "remote_workdir": remote_workdir,
        "remote_root": remote_root,
        "path_authority": "task_contract_target_workplace",
        "worker": "materialized_execution",
        "line": _normalize(selector.get("line")) or "B14_spatial_grace_graph_utility",
        "batch": _normalize(selector.get("batch")) or "b14_l70_large_image_feasibility_batch",
        "dag_root": _normalize(selector.get("dag_root")) or "b14_l70_large_image_coordinate_protocol_audit",
        "prefix": "b14_l72_",
        "resource_hint": _normalize(selector.get("resource_hint")) or "gpu",
        "resource_lane": _normalize(selector.get("resource_lane")) or "gpu_run",
        "resource_fields": {
            "gpu_required": True,
            "gpu_count": 1,
            "script_id": script_id,
            "taskbook_remote_root": _normalize(target_workplace.get("remote_root")),
        },
        "execution_mode": "dry_run",
        "status": "ready_for_materialized_execution_handoff",
        "required_artifacts": [
            "l72_task_admission_packet.json",
            "l72_task_draft_scheduler_dry_run.json",
            "l72_staging_candidate_report.json",
        ],
        "capability_checks": [
            "l71_sidecar_adapter_validated",
            "l72_scheduler_dry_run_ready",
            "night_auto_pending_scheduler_invisible",
        ],
        "side_effects": {
            "ssh": False,
            "remote_write": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "running_state": False,
            "subprocess": False,
            "remote_execution": False,
        },
    }
    if not remote_workdir:
        blockers.append("remote_workdir_missing")
    if not remote_root:
        blockers.append("remote_root_missing")

    _write_json(work_order_path, launch_work_order)

    if blockers:
        report = {
            "schema_version": L72_MANUAL_LAUNCH_READINESS_SCHEMA_VERSION,
            "status": "blocked",
            "ready_for_manual_launch_preflight": False,
            "task_id": task_id,
            "blockers": sorted(set(blockers)),
            "launch_work_order_path": str(work_order_path),
            "launch_intent": None,
            "manual_launch_readiness": None,
            "side_effects": {
                "ssh": False,
                "remote_write": False,
                "scheduler_dispatch": False,
                "registry_mutation": False,
                "running_state": False,
                "subprocess": False,
                "remote_execution": False,
            },
        }
        _write_json(report_path, report)
        return report

    intent = launch_intent.create_launch_intent_dry_run(
        launch_work_order,
        actor=actor,
        approve_remote_launch=approve_remote_launch,
    )
    readiness = None
    if _normalize(intent.get("status")) == "ready_for_launch_preflight" and _normalize(intent.get("launch_intent_path")):
        readiness = manual_launch.write_manual_launch_readiness_report(
            intent["launch_intent_path"],
            actor=actor,
            operator_confirmed=operator_confirmed,
        )

    report_blockers = []
    if _normalize(intent.get("status")) != "ready_for_launch_preflight":
        report_blockers.extend(intent.get("reasons", []))
    if readiness is None:
        report_blockers.append("manual_launch_readiness_not_created")
    elif _normalize(readiness.get("status")) != "ready_for_human_approved_remote_launch":
        report_blockers.extend(readiness.get("reasons", []))

    report = {
        "schema_version": L72_MANUAL_LAUNCH_READINESS_SCHEMA_VERSION,
        "status": "ready_for_human_approved_remote_launch" if not report_blockers else "blocked",
        "ready_for_manual_launch_preflight": _normalize(intent.get("status")) == "ready_for_launch_preflight",
        "ready_for_human_approved_remote_launch": not report_blockers,
        "task_id": task_id,
        "blockers": sorted(set(_normalize(item) for item in report_blockers if _normalize(item))),
        "launch_work_order_path": str(work_order_path),
        "launch_intent_path": intent.get("launch_intent_path", ""),
        "manual_launch_readiness_report_path": readiness.get("report_path", "") if isinstance(readiness, dict) else "",
        "launch_intent": intent,
        "manual_launch_readiness": readiness,
        "next_required_approval": (
            "live_launch_guard_with_user_approval_phrase"
            if not report_blockers
            else "resolve_l72_manual_launch_readiness_blockers"
        ),
        "side_effects": {
            "ssh": False,
            "remote_write": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "running_state": False,
            "subprocess": False,
            "remote_execution": False,
        },
    }
    _write_json(report_path, report)
    return report


__all__ = [
    "assess_three_agent_autonomy",
    "assess_repair_to_scheduler_gate",
    "assess_repair_to_scheduler_gate_from_summary",
    "build_artifact_index_research_prerequisite_packet",
    "build_context_index_research_prerequisite_packet",
    "build_research_prerequisite_packet",
    "build_l72_task_admission_packet",
    "build_l72_manual_launch_readiness_from_stage",
    "dry_run_l72_task_draft_scheduler_admission",
    "rehearse_scheduler_admission_after_repair",
    "rehearse_three_agent_flow",
    "stage_l72_task_draft_candidate",
    "validate_workplace_patch_adapter",
]
