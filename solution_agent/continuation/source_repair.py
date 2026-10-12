from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from .paths import continuation_source_repair_plan_dir
import solution_agent.orchestrator.paths as orchestrator_paths
from solution_agent.tools.patch_tools import build_patch_from_intent


SOURCE_REPAIR_SCHEMA_VERSION = 1
PATCH_INTENT_SCHEMA_VERSION = 1
SOURCE_REPAIR_PLANNER_VERSION = "0.2.0"

SOURCE_REPAIR_KINDS = {
    "codex_debug_request",
    "missing_capability",
    "needs_bounded_smoke_runner",
    "request_codex_review",
    "source_code_required",
}

SOURCE_REPAIR_TEXT_MARKERS = (
    "codex",
    "source code",
    "source repair",
    "source edit",
    "cli flag",
    "module",
    "runner",
    "script",
    "utils/",
    ".py",
    "--",
)

NON_SOURCE_MISSING_KINDS = {
    "missing_artifact",
    "missing_cache",
    "missing_dataset",
    "missing_input",
    "needs_user_decision",
}

DEFAULT_SIDE_EFFECTS: Dict[str, bool] = {
    "source_edit": False,
    "inbox_write": False,
    "scheduler_dispatch": False,
    "registry_mutation": False,
    "remote_execution": False,
    "subprocess": False,
    "api_call": False,
}

SELF_REPAIR_PATCH_INTENT_OPERATION = "insert_after"
PATCH_INTENT_FILENAME = "patch_intent.json"
PATCH_DIFF_FILENAME = "patch.diff"
PATCH_QUALITY_REPORT_FILENAME = "patch_quality_report.json"
REPAIR_DESIGN_PACKET_FILENAME = "repair_design_packet.json"
REPAIR_DESIGN_VALIDATION_FILENAME = "validation_report.json"
CLAIM_BOUNDARY_REPORT_FILENAME = "claim_boundary_report.json"
RERUN_INTENT_FILENAME = "rerun_intent.json"
SAFE_DETERMINISTIC_PREFIXES = (
    ".solution/continuation/",
    ".solution/knowledge/_proposed/",
    "tasks/inbox/_proposed/",
    "tests/golden/",
    "tests/night_researcher/",
    "docs/",
    "solution_agent/templates/",
    "solution_agent/schemas/",
)
SAFE_BOUNDED_FILES = (
    "solution_agent/execution_port/script_registry.yaml",
    "B14_spatial_grace_graph_utility/utils/b14_bbox_sidecar.py",
)
SAFE_BOUNDED_PREFIXES = (
    ".solution/policies/",
    "experiments/b14_sandbox/",
    "scripts/b14_probe/",
    "B14_spatial_grace_graph_utility/scripts/b14_probe/",
)
FORBIDDEN_REPAIR_PREFIXES = (
    "solution_agent/orchestrator/",
    "solution_agent/execution_port/runner",
    "solution_agent/remote_runner",
    "scripts/train",
    "scripts/eval",
    "datasets/",
    "outputs/",
    "checkpoints/",
    ".env",
    ".git/",
)

SELF_REPAIR_REQUIRE_UPSTREAM_TASK = {
    "sandbox_cli_flag_integration",
    "sandbox_probe_script_missing",
}


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    safe = safe.replace("..", "_").strip("._")
    return safe or "unknown"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None


def _read_text(path: Path, *, limit: int = 20000) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="ignore")[:limit]
    except FileNotFoundError:
        return ""


def _artifact_dir(result_dir: Path | str) -> Path:
    root = Path(result_dir)
    if (root / "action_requests.json").exists() or (root / "decision_summary.json").exists():
        return root
    if (root / "artifacts").exists():
        return root / "artifacts"
    return root


def _parse_taskbook_front_matter(text: str) -> Dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    front = text[3:end].strip()
    payload: Dict[str, Any] = {}
    current_key = ""
    for raw_line in front.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not raw_line.startswith((" ", "\t")) and ":" in line:
            key, value = line.split(":", 1)
            current_key = key.strip()
            payload[current_key] = value.strip().strip("'\"")
        elif current_key and line.lstrip().startswith("-"):
            payload.setdefault(current_key, [])
            if not isinstance(payload[current_key], list):
                payload[current_key] = []
            payload[current_key].append(line.lstrip()[1:].strip().strip("'\""))
    return payload


def _normalize_action_requests(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        value = raw.get("action_requests", raw.get("requests", raw))
        if isinstance(value, list):
            raw_items = value
        else:
            raw_items = [value]
    elif isinstance(raw, list):
        raw_items = raw
    else:
        raw_items = [{"kind": "unknown", "message": str(raw)}]

    def _normalize_action_item(item: Any, *, index: int) -> Dict[str, Any]:
        if isinstance(item, dict):
            payload = dict(item)
        else:
            payload = {"message": str(item)}
        payload.setdefault("kind", "unknown")
        payload["kind"] = _normalize(payload.get("kind")) or "unknown"
        payload["message"] = (
            _normalize(payload.get("message"))
            or _normalize(payload.get("reason"))
            or _normalize(payload.get("details"))
            or ""
        )
        payload["missing_or_needed"] = (
            _normalize(payload.get("missing_or_needed"))
            or payload["message"]
        )
        payload["why_needed"] = (
            _normalize(payload.get("why_needed"))
            or _normalize(payload.get("requested_action"))
            or _normalize(payload.get("reason"))
            or _normalize(payload.get("details"))
            or ""
        )
        payload["index"] = index
        return payload

    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        normalized.append(_normalize_action_item(item, index=index))
    return normalized


def _action_text(action: Dict[str, Any]) -> str:
    pieces = []
    for key in ("kind", "message", "requested_action", "missing_or_needed", "reason", "details"):
        value = action.get(key)
        if value and str(value) not in pieces:
            pieces.append(str(value))
    return "\n".join(pieces)


def _is_source_repair_action(action: Dict[str, Any]) -> bool:
    kind = _normalize(action.get("kind")).lower()
    text = _action_text(action).lower()
    if kind in NON_SOURCE_MISSING_KINDS and not any(marker in text for marker in SOURCE_REPAIR_TEXT_MARKERS):
        return False
    if kind in SOURCE_REPAIR_KINDS:
        return True
    return any(marker in text for marker in SOURCE_REPAIR_TEXT_MARKERS)


def _extract_patch_scope(text: str) -> Dict[str, List[str]]:
    # Also recognize common glue/config/schema/template extensions used by bounded
    # repairs that live alongside deterministic code paths.
    file_pattern = re.compile(
        r"(?<![\w.-])([A-Za-z0-9_./\\-]+\.(?:py|ya?ml|json|md|j2|toml|cfg|ini))(?![\w.-])"
    )
    flag_pattern = re.compile(r"(?<!\w)(--[A-Za-z0-9][A-Za-z0-9_-]*)(?![\w-])")
    files = sorted({_normalize(match.group(1)).replace("\\", "/") for match in file_pattern.finditer(text) if match.group(1)})
    flags = sorted({_normalize(match.group(1)) for match in flag_pattern.finditer(text) if match.group(1)})
    modules = sorted({item for item in files if "/" in item or item.startswith("utils") or item.startswith("scripts")})
    return {
        "files_or_modules": files or modules,
        "cli_flags": flags,
    }


def _infer_repair_intent_route(files: List[str], action_kind_hint: str) -> str:
    normalized = [item.replace("\\", "/") for item in files if item]
    if not normalized:
        if "closure" in action_kind_hint:
            return "deterministic_self"
        if "policy" in action_kind_hint or "registry" in action_kind_hint:
            return "bounded_self_repair"
        return "codex"
    if any(path.startswith(FORBIDDEN_REPAIR_PREFIXES) for path in normalized):
        return "codex"
    if any(path in SAFE_BOUNDED_FILES or path.startswith(SAFE_BOUNDED_PREFIXES) for path in normalized):
        return "bounded_self_repair"
    if all(any(path.startswith(prefix) for prefix in SAFE_DETERMINISTIC_PREFIXES) for path in normalized):
        return "deterministic_self"
    allowed_prefixes = SAFE_DETERMINISTIC_PREFIXES + SAFE_BOUNDED_PREFIXES
    if all(any(path.startswith(prefix) for prefix in allowed_prefixes) for path in normalized):
        return "bounded_self_repair"
    return "codex"


def _infer_repair_type_from_scope(files: List[str], flags: List[str], text: str) -> str:
    normalized = [item.replace("\\", "/") for item in files if item]
    lower_text = text.lower()
    if any(path in SAFE_BOUNDED_FILES for path in normalized):
        return "sandbox_thin_wrapper_missing"
    if any(path.startswith("scripts/b14_probe/") or path.startswith("B14_spatial_grace_graph_utility/scripts/b14_probe/") for path in normalized):
        return "sandbox_probe_script_missing"
    if flags and (
        "report_only" in lower_text
        or "sidecar" in lower_text
        or "default behavior" in lower_text
        or "mainline" in lower_text
    ):
        return "sandbox_cli_flag_integration"
    if any("script_registry.yaml" in path for path in normalized):
        return "registry_contract_update"
    return "source_behavior_change"


def _build_patch_intent(plan: Dict[str, Any]) -> Dict[str, Any] | None:
    files = [
        item.replace("\\", "/")
        for item in (plan.get("proposed_patch_scope", {}).get("files_or_modules", []) or [])
        if item
    ]
    if not files:
        return None
    actions = plan.get("codex_task_intent", {}).get("action_requests", [])
    action_kind_hint = " ".join(_normalize(action.get("kind")) for action in actions).lower()
    route = _infer_repair_intent_route(files, action_kind_hint)
    if route not in {"deterministic_self", "bounded_self_repair"}:
        return None

    target_file = files[0]
    operation = "create_file" if _repair_prefers_create_file(plan, target_file) else SELF_REPAIR_PATCH_INTENT_OPERATION
    text_source = " ".join(_action_text(action) for action in actions if _normalize(action.get("message")))
    anchor_text = _normalize(text_source) or _normalize(target_file)
    anchor_text = anchor_text if len(anchor_text) <= 200 else anchor_text[:200]
    if not anchor_text:
        anchor_text = "repair placeholder"
    route_tier = "P0" if route == "deterministic_self" else "P1"
    intent_id = f"patch_intent_{_safe_id(plan.get('source_task_id'))}_{_safe_id(target_file)[:10]}_{_safe_id(route)}"
    return {
        "schema_version": PATCH_INTENT_SCHEMA_VERSION,
        "intent_id": intent_id,
        "tier": route_tier,
        "target_file": target_file,
        "operation": operation,
        "anchor": {
            "type": "literal",
            "text": anchor_text,
            "occurrence": "first",
        },
        "new_text": _default_new_text_for_repair(plan, target_file, route),
        "reason": f"{route} repair for source task {plan.get('source_task_id')}",
        "expected_validation": {
            "must_apply_cleanly": True,
            "must_change_only": [target_file],
            "required_tests": [],
        },
        "risk": {
            "source_code_behavior_change": False,
            "multi_file": False,
            "requires_codex": False,
        },
        "executor_route_hint": route,
    }


def _repair_prefers_create_file(plan: Dict[str, Any], target_file: str) -> bool:
    repair_type = _normalize(plan.get("repair_type"))
    if repair_type in {"sandbox_probe_script_missing", "sandbox_thin_wrapper_missing"}:
        return True
    lowered = target_file.lower()
    return lowered.startswith(("scripts/b14_probe/", "experiments/b14_sandbox/"))


def _default_new_text_for_repair(plan: Dict[str, Any], target_file: str, route: str) -> str:
    repair_type = _normalize(plan.get("repair_type"))
    if repair_type == "sandbox_thin_wrapper_missing" and target_file.endswith("b14_bbox_sidecar.py"):
        return "\n".join(
            [
                '"""Report-only B14 bbox sidecar helpers.',
                "",
                "This module is intentionally conservative: it can format candidate",
                "bbox payloads for diagnostics, but it must not mutate B14 mainline",
                "mask/evidence/token-fill outputs.",
                '"""',
                "",
                "from __future__ import annotations",
                "",
                "from typing import Any, Dict, Iterable, List",
                "",
                "",
                "def decode_p2_bbox_candidates(",
                "    image_id: str,",
                "    p2_proposals: Iterable[Dict[str, Any]] | None,",
                "    local_retoken_outputs: Dict[str, Any] | None = None,",
                "    clear_error_map: Dict[str, Any] | None = None,",
                "    g_src_map: Dict[str, Any] | None = None,",
                "    score_threshold: float = 0.2,",
                "    top_m: int = 5,",
                "    nms: bool = True,",
                "    nms_iou: float = 0.5,",
                ") -> List[Dict[str, Any]]:",
                '    """Return claim-limited report-only bbox candidates."""',
                "    del local_retoken_outputs, clear_error_map, g_src_map, nms, nms_iou",
                "    candidates: List[Dict[str, Any]] = []",
                "    for index, proposal in enumerate(p2_proposals or []):",
                "        try:",
                "            score = float(proposal.get('score', proposal.get('p_valid', 0.0)))",
                "        except (TypeError, ValueError):",
                "            score = 0.0",
                "        if score < score_threshold:",
                "            continue",
                "        bbox = proposal.get('bbox_xyxy_original') or proposal.get('bbox_xyxy')",
                "        candidates.append(",
                "            {",
                "                'image_id': image_id,",
                "                'bbox_xyxy_original': bbox,",
                "                'bbox_xyxy_local32': proposal.get('bbox_xyxy_local32'),",
                "                'score': score,",
                "                'p_valid': proposal.get('p_valid', score),",
                "                'q_iou': proposal.get('q_iou'),",
                "                'scale_bucket': proposal.get('scale_bucket', 'unknown'),",
                "                'crop_id': proposal.get('crop_id', f'p2_{index}'),",
                "                'source': proposal.get('source', 'p2_report_only_sidecar'),",
                "                'evidence_summary': proposal.get('evidence_summary', {}),",
                "                'local32_to_original_mapping': bool(proposal.get('local32_to_original_mapping')),",
                "            }",
                "        )",
                "    candidates.sort(key=lambda item: item.get('score', 0.0), reverse=True)",
                "    return candidates[: max(0, int(top_m))]",
                "",
            ],
        )
    return f"### repair placeholder for {route}"


def _infer_rerun_intent(source_task_id: str, decision: Dict[str, Any], actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    next_action = _normalize(decision.get("next_action"))
    downstream = _normalize(decision.get("downstream_task_id") or decision.get("task_id"))
    if not downstream and source_task_id:
        downstream = source_task_id
    text = "\n".join([next_action, *[_action_text(action) for action in actions]]).lower()
    upstream = ""
    if "l71" in text or "complete l71" in text:
        upstream = "b14_l71_bbox_sidecar_large_image_cli_integration"
    return {
        "after_repair": "rerun_downstream_smoke_or_metric_task",
        "downstream_task_id": downstream,
        "upstream_source_repair_task_id": upstream,
        "next_action_hint": next_action,
    }


def build_repair_design_packet(source_repair_plan: Dict[str, Any]) -> Dict[str, Any]:
    """Build an evidence-gated repair design packet from a source-repair plan.

    The packet is a problem-decomposition artifact. It explains how the agent
    should decide whether to continue locally, compare alternatives, or stop for
    human judgment before any source patch is applied.
    """

    plan = dict(source_repair_plan or {})
    source_task_id = _normalize(plan.get("source_task_id"))
    repair_type = _normalize(plan.get("repair_type"))
    trigger_kind = _normalize(plan.get("trigger_kind"))
    scope = plan.get("proposed_patch_scope") if isinstance(plan.get("proposed_patch_scope"), dict) else {}
    files = [_normalize(item) for item in scope.get("files_or_modules", []) if _normalize(item)]
    flags = [_normalize(item) for item in scope.get("cli_flags", []) if _normalize(item)]
    actions = plan.get("codex_task_intent", {}).get("action_requests", [])
    action_text = "\n".join(_action_text(action) for action in actions if isinstance(action, dict))
    missing_or_needed = _normalize(action_text) or "source repair capability gap"
    rerun = plan.get("rerun_intent") if isinstance(plan.get("rerun_intent"), dict) else {}

    if repair_type == "sandbox_cli_flag_integration" and "--large-image-mode" in flags:
        problem_class = "capability_gap"
        preferred_option = "dedicated_allowlisted_smoke_runner"
        alternatives = [
            {
                "id": "dedicated_allowlisted_smoke_runner",
                "description": "Add or use a bounded report-only smoke runner that accepts large-image mode configuration without changing B14 default inference behavior.",
                "evidence_value": "proves L72 can run a metric-producing or capability-complete smoke path",
                "risk": "low_to_medium",
                "can_continue_autonomously": True,
            },
            {
                "id": "report_only_entrypoint_flags",
                "description": "Add optional report-only flags such as --large-image-mode to the existing B14 entrypoint while preserving disabled-by-default behavior.",
                "evidence_value": "tests the real CLI integration path",
                "risk": "medium",
                "can_continue_autonomously": False,
                "requires_human_if": "default behavior or core runtime behavior would change",
            },
            {
                "id": "sidecar_helper_only",
                "description": "Create or validate only the sidecar helper module, then stop before claiming L72 readiness.",
                "evidence_value": "proves helper import/schema but not full L72 smoke readiness",
                "risk": "low",
                "can_continue_autonomously": True,
            },
        ]
    else:
        problem_class = "source_repair_gap" if files or flags else "evidence_gap"
        preferred_option = "bounded_repair_then_validation"
        alternatives = [
            {
                "id": "bounded_repair_then_validation",
                "description": "Apply the smallest bounded repair in an allowlisted target and validate it with existing tests.",
                "evidence_value": "confirms whether the missing capability was the blocker",
                "risk": "medium",
                "can_continue_autonomously": bool(files),
            },
            {
                "id": "more_evidence_first",
                "description": "Run or inspect a bounded inventory/probe before deciding the repair target.",
                "evidence_value": "reduces path or target ambiguity",
                "risk": "low",
                "can_continue_autonomously": True,
            },
        ]

    design = {
        "schema_version": 1,
        "artifact_type": "repair_design_packet",
        "planner_version": SOURCE_REPAIR_PLANNER_VERSION,
        "design_id": f"rdp_{_safe_id(source_task_id)}_{_sha256_hex(plan)[:12]}",
        "source_repair_plan_id": _normalize(plan.get("plan_id")),
        "source_task_id": source_task_id,
        "trigger_kind": trigger_kind,
        "repair_type": repair_type,
        "problem_statement": {
            "classification": problem_class,
            "missing_or_needed": missing_or_needed,
            "current_claim_limit": "no_metric_claim" if "l72" in source_task_id.lower() else "claim_limited_until_validated",
            "not_a_model_quality_failure": trigger_kind == "missing_capability",
        },
        "candidate_options": alternatives,
        "fair_comparison_protocol": {
            "baseline": "current L72 partial result with no bbox metric claim",
            "compare_options_on": [
                "can_produce_required_L72_evidence",
                "preserves_default_B14_mainline_behavior",
                "bounded_blast_radius",
                "has_local_validation_tests",
                "does_not_upgrade_smoke_or_proxy_to_formal_claim",
            ],
            "confounders_to_control": [
                "no default mask/token-fill behavior change",
                "no formal bbox success claim from smoke evidence",
                "no remote dispatch during design validation",
            ],
        },
        "minimal_bounded_experiment": {
            "preferred_option": preferred_option,
            "required_outputs": [
                "validation_report.json",
                "claim_boundary_report.json",
                "rerun_intent.json",
            ],
            "required_checks": [
                "sidecar_module_import_or_runner_help",
                "large_image_mode_config_or_flag_detected",
                "report_only_default_behavior",
                "no_formal_claim",
                "rerun_target_is_b14_l72_large_image_bbox_sidecar_smoke",
            ],
        },
        "decision_rule": {
            "continue_autonomously_if": [
                "chosen_option_is_report_only_or_allowlisted_smoke_runner",
                "validation_evidence_exists",
                "default_mainline_behavior_preserved",
                "next_step_is_bounded_rerun_intent_not_dispatch",
            ],
            "ask_human_if": [
                "default_mainline_behavior_change_needed",
                "formal_bbox_claim_upgrade_needed",
                "high_cost_remote_or_gpu_execution_needed",
                "evidence_conflict_without_authority",
                "research_direction_choice_required",
            ],
            "must_not_do": [
                "dispatch_remote_work",
                "auto_promote_to_active_inbox",
                "mutate_raw_dataset",
                "relax_safety_gate",
                "claim_formal_bbox_success_from_smoke",
            ],
        },
        "rerun_intent": {
            "downstream_task_id": _normalize(rerun.get("downstream_task_id")),
            "upstream_source_repair_task_id": _normalize(rerun.get("upstream_source_repair_task_id")),
            "after_repair": _normalize(rerun.get("after_repair")),
            "allowed_only_after_validation": True,
        },
        "side_effects": dict(DEFAULT_SIDE_EFFECTS),
    }
    return design


def write_repair_design_packet(
    source_repair_plan: Dict[str, Any],
    *,
    output_dir: Path | str | None = None,
) -> Dict[str, Any]:
    design = build_repair_design_packet(source_repair_plan)
    if output_dir is None:
        plan_id = _normalize(source_repair_plan.get("plan_id")) or "unknown_source_repair_plan"
        target_dir = continuation_source_repair_plan_dir(plan_id)
    else:
        target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / REPAIR_DESIGN_PACKET_FILENAME
    serialized = json.dumps(design, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if target_path.exists():
        existing = target_path.read_text(encoding="utf-8")
        if existing == serialized:
            return {"status": "replayed", "path": str(target_path), "design": design, "side_effects": dict(DEFAULT_SIDE_EFFECTS)}
        return {
            "status": "blocked",
            "reason": "repair_design_packet_collision",
            "path": str(target_path),
            "side_effects": dict(DEFAULT_SIDE_EFFECTS),
        }
    target_path.write_text(serialized, encoding="utf-8")
    return {"status": "written", "path": str(target_path), "design": design, "side_effects": dict(DEFAULT_SIDE_EFFECTS)}


def _design_check(checks: List[Dict[str, Any]], check_id: str, passed: bool, **details: Any) -> None:
    payload = {"id": check_id, "passed": bool(passed)}
    payload.update(details)
    checks.append(payload)


def build_repair_design_validation_report(
    repair_design_packet: Dict[str, Any],
    *,
    repo_root: Path | str | None = None,
) -> Dict[str, Any]:
    """Validate whether a repair design packet has enough local evidence to rerun.

    This is intentionally static and local. It does not import project code,
    execute a runner, write inbox tasks, dispatch a scheduler, or call SSH.
    """

    design = dict(repair_design_packet or {})
    root = Path(repo_root) if repo_root is not None else orchestrator_paths.root()
    source_task_id = _normalize(design.get("source_task_id"))
    minimal = design.get("minimal_bounded_experiment") if isinstance(design.get("minimal_bounded_experiment"), dict) else {}
    preferred_option = _normalize(minimal.get("preferred_option"))
    required_checks = [_normalize(item) for item in minimal.get("required_checks", []) if _normalize(item)]
    rerun = design.get("rerun_intent") if isinstance(design.get("rerun_intent"), dict) else {}

    runner_path = root / "scripts" / "reports" / "run_b14_large_image_bbox_sidecar_smoke.py"
    sidecar_path = root / "B14_spatial_grace_graph_utility" / "utils" / "b14_bbox_sidecar.py"
    runner_text = _read_text(runner_path, limit=60000)

    checks: List[Dict[str, Any]] = []
    blockers: List[str] = []

    def add_required(check_id: str, passed: bool, blocker: str, **details: Any) -> None:
        _design_check(checks, check_id, passed, required=True, **details)
        if not passed:
            blockers.append(blocker)

    runner_exists = runner_path.exists()
    add_required(
        "allowlisted_smoke_runner_exists",
        runner_exists,
        "allowlisted_smoke_runner_missing",
        path=str(runner_path),
    )

    sidecar_exists = sidecar_path.exists()
    add_required(
        "sidecar_module_exists",
        sidecar_exists,
        "sidecar_helper_missing_local",
        path=str(sidecar_path),
    )

    parser_flag_pattern = re.compile(r"add_argument\s*\(\s*[\"']--large-image-mode[\"']")
    large_image_flag_supported = bool(runner_text and parser_flag_pattern.search(runner_text))
    add_required(
        "large_image_mode_config_or_flag_detected",
        large_image_flag_supported,
        "large_image_mode_config_missing",
        detection="argparse_add_argument",
    )

    report_only = all(marker in runner_text for marker in ("claim_limit", "no_metric_claim")) and "fabricate" in runner_text.lower()
    add_required(
        "report_only_default_behavior",
        report_only,
        "report_only_boundary_missing",
    )

    no_formal_claim = (
        "no_metric_claim" in runner_text
        and "formal bbox" not in runner_text.lower().replace("no formal bbox", "")
        and "claim formal bbox success" not in _canonical_json(design).lower()
    )
    add_required(
        "no_formal_claim",
        no_formal_claim,
        "formal_claim_boundary_missing",
    )

    rerun_target = _normalize(rerun.get("downstream_task_id"))
    target_ok = rerun_target == "b14_l72_large_image_bbox_sidecar_smoke"
    add_required(
        "rerun_target_is_b14_l72_large_image_bbox_sidecar_smoke",
        target_ok,
        "rerun_target_invalid",
        downstream_task_id=rerun_target,
    )

    preferred_ok = preferred_option in {"dedicated_allowlisted_smoke_runner", "sidecar_helper_only"}
    add_required(
        "preferred_option_is_bounded",
        preferred_ok,
        "preferred_option_not_bounded",
        preferred_option=preferred_option,
    )

    side_effects = design.get("side_effects") if isinstance(design.get("side_effects"), dict) else {}
    forbidden_side_effects = [
        key
        for key in ("remote_execution", "scheduler_dispatch", "registry_mutation", "inbox_write", "api_call")
        if side_effects.get(key) is True
    ]
    add_required(
        "design_has_no_execution_side_effects",
        not forbidden_side_effects,
        "design_side_effect_boundary_failed",
        forbidden_side_effects=forbidden_side_effects,
    )

    status = "passed" if not blockers else "blocked"
    validation_id = f"rdv_{_safe_id(source_task_id)}_{_sha256_hex({'design': design, 'checks': checks})[:12]}"
    return {
        "schema_version": 1,
        "artifact_type": "repair_design_validation_report",
        "validation_id": validation_id,
        "design_id": _normalize(design.get("design_id")),
        "source_task_id": source_task_id,
        "status": status,
        "can_continue_autonomously": status == "passed",
        "can_emit_rerun_intent": status == "passed",
        "preferred_option": preferred_option,
        "required_checks": required_checks,
        "checks": checks,
        "blockers": sorted(set(blockers)),
        "runner_path": str(runner_path),
        "sidecar_path": str(sidecar_path),
        "side_effects": dict(DEFAULT_SIDE_EFFECTS),
        "next_action": (
            "emit_claim_limited_rerun_intent"
            if status == "passed"
            else "repair_or_stage_allowlisted_smoke_runner_before_rerun_intent"
        ),
    }


def build_claim_boundary_report(repair_design_packet: Dict[str, Any], validation_report: Dict[str, Any]) -> Dict[str, Any]:
    design = dict(repair_design_packet or {})
    validation = dict(validation_report or {})
    return {
        "schema_version": 1,
        "artifact_type": "claim_boundary_report",
        "design_id": _normalize(design.get("design_id")),
        "validation_id": _normalize(validation.get("validation_id")),
        "source_task_id": _normalize(design.get("source_task_id")),
        "formal_claim_allowed": False,
        "allowed_claims": [
            "local validation checks passed or blocked",
            "runner/helper capability evidence only",
            "rerun intent readiness if validation passed",
        ],
        "forbidden_claims": [
            "large-image IoU improvement",
            "formal bbox quality success",
            "submission readiness",
            "B14 mainline method improvement",
        ],
        "claim_limit": "no_metric_claim_until_bounded_rerun_evidence",
        "validation_status": _normalize(validation.get("status")),
        "side_effects": dict(DEFAULT_SIDE_EFFECTS),
    }


def build_rerun_intent_from_repair_design_validation(
    repair_design_packet: Dict[str, Any],
    validation_report: Dict[str, Any],
) -> Dict[str, Any] | None:
    if _normalize(validation_report.get("status")) != "passed":
        return None
    design = dict(repair_design_packet or {})
    rerun = design.get("rerun_intent") if isinstance(design.get("rerun_intent"), dict) else {}
    intent = {
        "schema_version": 1,
        "artifact_type": "rerun_intent",
        "intent_id": f"rerun_{_safe_id(design.get('source_task_id'))}_{_sha256_hex({'design': design, 'validation': validation_report})[:12]}",
        "source_task_id": _normalize(design.get("source_task_id")),
        "downstream_task_id": _normalize(rerun.get("downstream_task_id")),
        "upstream_source_repair_task_id": _normalize(rerun.get("upstream_source_repair_task_id")),
        "after_repair": _normalize(rerun.get("after_repair")) or "rerun_downstream_smoke_or_metric_task",
        "allowed_to_dispatch": False,
        "requires_staging_gate": True,
        "requires_explicit_remote_approval": True,
        "claim_limit": "rerun verifies repaired capability only; no formal bbox or IoU claim",
        "validation_id": _normalize(validation_report.get("validation_id")),
        "forbidden": [
            "scheduler_dispatch",
            "remote_execution_without_approval",
            "formal_bbox_claim",
            "active_inbox_write",
        ],
        "side_effects": dict(DEFAULT_SIDE_EFFECTS),
    }
    return intent


def write_repair_design_validation(
    repair_design_packet: Dict[str, Any],
    *,
    output_dir: Path | str | None = None,
    repo_root: Path | str | None = None,
) -> Dict[str, Any]:
    validation = build_repair_design_validation_report(repair_design_packet, repo_root=repo_root)
    claim = build_claim_boundary_report(repair_design_packet, validation)
    rerun_intent = build_rerun_intent_from_repair_design_validation(repair_design_packet, validation)

    if output_dir is None:
        design_id = _normalize(repair_design_packet.get("source_repair_plan_id")) or _normalize(repair_design_packet.get("design_id")) or "unknown_repair_design"
        target_dir = continuation_source_repair_plan_dir(design_id)
    else:
        target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    validation_path = target_dir / REPAIR_DESIGN_VALIDATION_FILENAME
    claim_path = target_dir / CLAIM_BOUNDARY_REPORT_FILENAME
    rerun_path = target_dir / RERUN_INTENT_FILENAME
    validation_payload = json.dumps(validation, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    claim_payload = json.dumps(claim, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    previous_validation = validation_path.read_text(encoding="utf-8") if validation_path.exists() else ""
    validation_path.write_text(validation_payload, encoding="utf-8")
    claim_path.write_text(claim_payload, encoding="utf-8")
    rerun_intent_path = None
    if rerun_intent is not None:
        rerun_path.write_text(json.dumps(rerun_intent, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        rerun_intent_path = str(rerun_path)
    elif rerun_path.exists():
        rerun_path.unlink()

    return {
        "status": "replayed" if previous_validation == validation_payload else "written",
        "validation_status": validation["status"],
        "validation_path": str(validation_path),
        "claim_boundary_path": str(claim_path),
        "rerun_intent_path": rerun_intent_path,
        "validation": validation,
        "claim_boundary": claim,
        "rerun_intent": rerun_intent,
        "side_effects": dict(DEFAULT_SIDE_EFFECTS),
    }


def _blocked_by(decision: Dict[str, Any], extra_blockers: List[str] | None) -> List[str]:
    blockers = list(extra_blockers or [])
    if decision.get("codex_remote_ready") is False:
        blockers.append("remote_codex_unavailable")
    if decision.get("workdir_is_git_worktree") is False:
        blockers.append("remote_workdir_not_git_worktree")
    return sorted({_normalize(item) for item in blockers if _normalize(item)})


def _add_rerun_contract_validation(plan: Dict[str, Any]) -> None:
    if not plan.get("needs_source_repair"):
        return
    repair_type = _normalize(plan.get("repair_type"))
    rerun_intent = plan.get("rerun_intent")
    if not isinstance(rerun_intent, dict):
        plan.setdefault("blocked_by", []).append("rerun_intent_missing")
        plan["recommended_worker"] = "codex"
        return

    blockers = set(plan.get("blocked_by", []))
    if not _normalize(rerun_intent.get("downstream_task_id")):
        blockers.add("rerun_intent_missing_downstream_task_id")
    if repair_type in SELF_REPAIR_REQUIRE_UPSTREAM_TASK and not _normalize(rerun_intent.get("upstream_source_repair_task_id")):
        blockers.add("rerun_intent_missing_upstream_source_repair_task_id")
    if blockers:
        plan["blocked_by"] = sorted(blockers)
        plan["recommended_worker"] = "codex"


def build_source_repair_plan(
    result_dir: Path | str,
    *,
    source_task_id: str | None = None,
    downstream_task_id: str | None = None,
    extra_blockers: List[str] | None = None,
) -> Dict[str, Any]:
    """Build a local, side-effect-free Codex source repair plan from result artifacts."""
    artifacts = _artifact_dir(result_dir)
    raw_actions = _read_json(artifacts / "action_requests.json")
    decision = _read_json(artifacts / "decision_summary.json")
    if not isinstance(decision, dict):
        decision = {}
    if raw_actions is None:
        inline_actions = decision.get("action_requests")
        if inline_actions is None:
            inline_actions = decision.get("action_request")
        if inline_actions is None:
            inline_actions = decision.get("requests")
        if inline_actions is None and isinstance(decision.get("interpretation"), dict):
            inline_actions = decision["interpretation"].get("action_requests")
        if inline_actions is not None:
            raw_actions = inline_actions
    actions = _normalize_action_requests(raw_actions)
    task_id = _normalize(source_task_id) or _normalize(decision.get("task_id")) or _safe_id(artifacts.parent.name)

    source_actions = [action for action in actions if _is_source_repair_action(action)]
    combined_text = "\n".join(
        [
            json.dumps(decision, ensure_ascii=False, sort_keys=True),
            *[_action_text(action) for action in source_actions],
            _read_text(artifacts / "artifact_summary.md"),
            _read_text(artifacts / "worker_notes.md"),
        ],
    )
    patch_scope = _extract_patch_scope(combined_text)
    needs_source_repair = bool(source_actions)
    trigger_kind = _normalize(source_actions[0].get("kind")) if source_actions else "none"
    if downstream_task_id:
        decision["downstream_task_id"] = downstream_task_id

    basis_payload = {
        "planner_version": SOURCE_REPAIR_PLANNER_VERSION,
        "source_task_id": task_id,
        "decision": decision,
        "source_actions": source_actions,
        "patch_scope": patch_scope,
    }
    basis_hash = _sha256_hex(basis_payload)
    plan_id = f"srp_{_safe_id(task_id)}_{basis_hash[:12]}"

    required_context = [
        str(artifacts / "action_requests.json"),
        str(artifacts / "decision_summary.json"),
    ]
    for optional in ("artifact_summary.md", "worker_notes.md"):
        path = artifacts / optional
        if path.exists():
            required_context.append(str(path))

    status = "needs_source_repair" if needs_source_repair else "no_source_repair_needed"
    inferred_repair_type = _infer_repair_type_from_scope(
        patch_scope.get("files_or_modules", []),
        patch_scope.get("cli_flags", []),
        combined_text,
    )
    plan = {
        "schema_version": SOURCE_REPAIR_SCHEMA_VERSION,
        "planner_version": SOURCE_REPAIR_PLANNER_VERSION,
        "plan_id": plan_id,
        "idempotency_key": f"source_repair:{basis_hash[:24]}",
        "status": status,
        "needs_source_repair": needs_source_repair,
        "source_task_id": task_id,
        "trigger_kind": trigger_kind,
        "repair_type": inferred_repair_type if needs_source_repair else "",
        "recommended_worker": "codex" if needs_source_repair else "",
        "codex_task_intent": {
            "goal": "repair_missing_source_capability_then_rerun_bounded_smoke",
            "summary": "Prepare a bounded source repair request from collected action_requests; this plan does not edit source code.",
            "action_requests": source_actions,
        },
        "required_context": required_context,
        "proposed_patch_scope": patch_scope,
        "blocked_by": _blocked_by(decision, extra_blockers),
        "rerun_intent": _infer_rerun_intent(task_id, decision, source_actions),
        "patch_intent": None,
        "safety": {
            **DEFAULT_SIDE_EFFECTS,
            "no_source_edit_performed": True,
            "no_dispatch_performed": True,
            "no_inbox_write": True,
        },
    }
    plan["patch_intent"] = _build_patch_intent(plan)
    if isinstance(plan["patch_intent"], dict):
        route_hint = _normalize(plan["patch_intent"].get("executor_route_hint"))
        if route_hint in {"deterministic_self", "bounded_self_repair"}:
            plan["recommended_worker"] = route_hint
    _add_rerun_contract_validation(plan)
    return plan


def build_source_repair_plan_from_taskbook(
    task_path: Path | str,
    *,
    downstream_task_id: str | None = None,
    extra_blockers: List[str] | None = None,
) -> Dict[str, Any]:
    """Build a source-repair plan directly from a task book.

    This is for cases where the task book itself describes a missing capability
    or report-only glue step. It lets the agent plan a bounded repair before
    waiting for another worker to fail.
    """

    path = Path(task_path)
    text = _read_text(path, limit=50000)
    meta = _parse_taskbook_front_matter(text)
    task_id = _normalize(meta.get("task_id")) or _safe_id(path.stem)
    patch_scope = _extract_patch_scope(text)
    repair_type = _infer_repair_type_from_scope(
        patch_scope.get("files_or_modules", []),
        patch_scope.get("cli_flags", []),
        text,
    )
    basis_payload = {
        "planner_version": SOURCE_REPAIR_PLANNER_VERSION,
        "source_task_id": task_id,
        "task_path": str(path),
        "task_meta": meta,
        "patch_scope": patch_scope,
        "repair_type": repair_type,
    }
    basis_hash = _sha256_hex(basis_payload)
    plan_id = f"srp_{_safe_id(task_id)}_{basis_hash[:12]}"
    action = {
        "kind": _normalize(meta.get("intent")) or "source_code_required",
        "message": (
            "Taskbook requests bounded source/glue integration. "
            "Use the proposed patch scope and constraints from the task body."
        ),
        "requested_action": _normalize(meta.get("title")) or "bounded source repair",
        "source": str(path),
        "index": 0,
    }
    if downstream_task_id:
        downstream = downstream_task_id
    else:
        downstream = _normalize(meta.get("task_id"))
    plan = {
        "schema_version": SOURCE_REPAIR_SCHEMA_VERSION,
        "planner_version": SOURCE_REPAIR_PLANNER_VERSION,
        "plan_id": plan_id,
        "idempotency_key": f"source_repair_taskbook:{basis_hash[:24]}",
        "status": "needs_source_repair",
        "needs_source_repair": True,
        "source_task_id": task_id,
        "trigger_kind": _normalize(meta.get("intent")) or "source_code_required",
        "repair_type": repair_type,
        "recommended_worker": "codex",
        "codex_task_intent": {
            "goal": "repair_missing_source_capability_then_rerun_bounded_smoke",
            "summary": "Prepare a bounded source repair request from a taskbook; this plan does not edit source code.",
            "action_requests": [action],
        },
        "required_context": [str(path)],
        "proposed_patch_scope": patch_scope,
        "blocked_by": sorted({_normalize(item) for item in (extra_blockers or []) if _normalize(item)}),
        "rerun_intent": {
            "after_repair": "rerun_downstream_smoke_or_metric_task",
            "downstream_task_id": downstream,
            "upstream_source_repair_task_id": task_id,
            "next_action_hint": "stage bounded repair, validate report-only behavior, then rerun dependent smoke",
        },
        "patch_intent": None,
        "safety": {
            **DEFAULT_SIDE_EFFECTS,
            "no_source_edit_performed": True,
            "no_dispatch_performed": True,
            "no_inbox_write": True,
        },
    }
    plan["patch_intent"] = _build_patch_intent(plan)
    if isinstance(plan["patch_intent"], dict):
        route_hint = _normalize(plan["patch_intent"].get("executor_route_hint"))
        if route_hint in {"deterministic_self", "bounded_self_repair"}:
            plan["recommended_worker"] = route_hint
    _add_rerun_contract_validation(plan)
    return plan


def build_source_repair_plan_from_action_request(
    action_request_path: Path | str,
    *,
    downstream_task_id: str | None = None,
    extra_blockers: List[str] | None = None,
) -> Dict[str, Any]:
    """Build a source-repair plan from a collected pending action request.

    This bridges the action_request queue back into continuation planning.  The
    function is intentionally side-effect free: it reads the pending request and
    its referenced result artifacts, then returns the same source-repair plan
    shape produced by ``build_source_repair_plan``.
    """

    path = Path(action_request_path)
    request = _read_json(path)
    if not isinstance(request, dict):
        request = {}
    payload_raw = request.get("payload")
    payload_is_corrupt = payload_raw is not None and not isinstance(payload_raw, dict)
    payload = payload_raw if isinstance(payload_raw, dict) else {}
    local_blockers = list(extra_blockers or [])
    if payload_is_corrupt:
        local_blockers.append("pending_action_request_payload_invalid")
    source_result_path = _normalize(payload.get("source_result_path"))
    task_id = _normalize(request.get("task_id")) or _normalize(payload.get("source_task_id"))
    source_run_id = _normalize(payload.get("source_run_id"))

    if source_result_path:
        # Carry local blockers (including payload-corrupt markers and caller-specified
        # constraints) into the artifact-based plan construction so downstream
        # preflight/reporting stays consistent.
        plan = build_source_repair_plan(
            source_result_path,
            source_task_id=task_id or None,
            downstream_task_id=downstream_task_id,
            extra_blockers=local_blockers,
        )
    else:
        action = {
            "kind": _normalize(payload.get("kind")) or _normalize(request.get("action_type")) or "unknown",
            "message": (
                _normalize(payload.get("message"))
                or _normalize(payload.get("missing_or_needed"))
                or _normalize(request.get("reason"))
            ),
            "requested_action": (
                _normalize(payload.get("why_needed"))
                or _normalize(payload.get("suggested_next_step"))
                or _normalize(request.get("reason"))
                or _normalize(payload.get("reason"))
            ),
            "source_action_request_path": str(path),
            "index": 0,
        }
        action = _normalize_action_requests(action)[0]
        action["source_action_request_path"] = str(path)
        basis_payload = {
            "planner_version": SOURCE_REPAIR_PLANNER_VERSION,
            "source_task_id": task_id,
            "action_request_path": str(path),
            "action": action,
        }
        basis_hash = _sha256_hex(basis_payload)
        patch_scope = _extract_patch_scope(_action_text(action))
        needs_source_repair = _is_source_repair_action(action)
        repair_type = _infer_repair_type_from_scope(
            patch_scope.get("files_or_modules", []),
            patch_scope.get("cli_flags", []),
            _action_text(action),
        )
        plan = {
            "schema_version": SOURCE_REPAIR_SCHEMA_VERSION,
            "planner_version": SOURCE_REPAIR_PLANNER_VERSION,
            "plan_id": f"srp_{_safe_id(task_id)}_{basis_hash[:12]}",
            "idempotency_key": f"source_repair_action_request:{basis_hash[:24]}",
            "status": "needs_source_repair" if needs_source_repair else "no_source_repair_needed",
            "needs_source_repair": needs_source_repair,
            "source_task_id": task_id,
            "trigger_kind": action["kind"],
            "repair_type": repair_type if needs_source_repair else "",
            "recommended_worker": "codex" if needs_source_repair else "",
            "codex_task_intent": {
                "goal": "repair_missing_source_capability_then_rerun_bounded_smoke",
                "summary": "Prepare a bounded source repair request from a pending action_request; this plan does not edit source code.",
                "action_requests": [action] if needs_source_repair else [],
            },
            "required_context": [str(path)],
            "proposed_patch_scope": patch_scope,
            "blocked_by": sorted({_normalize(item) for item in local_blockers if _normalize(item)}),
            "rerun_intent": _infer_rerun_intent(task_id, {}, [action] if needs_source_repair else []),
            "patch_intent": None,
            "safety": {
                **DEFAULT_SIDE_EFFECTS,
                "no_source_edit_performed": True,
                "no_dispatch_performed": True,
                "no_inbox_write": True,
            },
        }
        plan["patch_intent"] = _build_patch_intent(plan)
        route_hint = ""
        if isinstance(plan["patch_intent"], dict):
            route_hint = _normalize(plan["patch_intent"].get("executor_route_hint"))
            if route_hint in {"deterministic_self", "bounded_self_repair"}:
                plan["recommended_worker"] = route_hint
    if payload_is_corrupt and plan.get("needs_source_repair"):
        blockers = set(plan.get("blocked_by", []))
        blockers.add("pending_action_request_payload_invalid")
        plan["blocked_by"] = sorted(blockers)
        plan["recommended_worker"] = "codex"
    _add_rerun_contract_validation(plan)

    required_context = list(plan.get("required_context", []))
    if str(path) not in required_context:
        required_context.append(str(path))
    plan["required_context"] = required_context
    plan["source_action_request"] = {
        "action_id": _normalize(request.get("action_id")),
        "path": str(path),
        "source_run_id": source_run_id,
        "source_result_path": source_result_path,
        "status": _normalize(request.get("status")),
        "lifecycle_status": _normalize(request.get("lifecycle_status")),
    }
    return plan


def render_codex_patch_request(plan: Dict[str, Any]) -> str:
    actions = plan.get("codex_task_intent", {}).get("action_requests", [])
    action_lines = []
    for action in actions:
        action_lines.append(f"- `{action.get('kind', 'unknown')}`: {_normalize(action.get('message'))}")
        requested = _normalize(action.get("requested_action"))
        if requested:
            action_lines.append(f"  requested_action: {requested}")
    if not action_lines:
        action_lines.append("- No source-repair action request detected.")

    files = plan.get("proposed_patch_scope", {}).get("files_or_modules", [])
    flags = plan.get("proposed_patch_scope", {}).get("cli_flags", [])
    scope_lines = [*(f"- file/module: `{item}`" for item in files), *(f"- CLI flag: `{item}`" for item in flags)]
    if not scope_lines:
        scope_lines.append("- Patch scope could not be inferred; inspect required context first.")
    patch_intent = plan.get("patch_intent")
    anchor_text = ""
    if isinstance(patch_intent, dict):
        anchor = patch_intent.get("anchor")
        if isinstance(anchor, dict):
            anchor_text = _normalize(anchor.get("text", ""))
    patch_lines = [
        f"- route_hint: `{patch_intent.get('executor_route_hint', '')}`" if isinstance(patch_intent, dict) else "- no route-specific patch intent generated"
    ]
    if isinstance(patch_intent, dict):
        patch_lines.extend(
            [
                f"- intent_id: `{patch_intent.get('intent_id', '')}`",
                f"- tier: `{patch_intent.get('tier', '')}`",
                f"- target_file: `{patch_intent.get('target_file', '')}`",
                f"- operation: `{patch_intent.get('operation', '')}`",
                f"- anchor: `{anchor_text}`",
            ],
        )

    rerun = plan.get("rerun_intent", {})
    return "\n".join(
        [
            "# Source Repair Request",
            "",
            f"- source_task_id: `{plan.get('source_task_id', '')}`",
            f"- trigger_kind: `{plan.get('trigger_kind', '')}`",
            f"- recommended_worker: `{plan.get('recommended_worker', '')}`",
            f"- status: `{plan.get('status', '')}`",
            "",
            "## Safety",
            "",
            "- This request is local, structured, and side-effect free.",
            "- Codex is an escalation lane, not the default executor.",
            "- It did not edit source code.",
            "- It did not write active inbox or `_proposed` tasks.",
            "- It did not dispatch scheduler work or launch remote execution.",
            "",
            "## Action Requests",
            "",
            *action_lines,
            "",
            "## Proposed Patch Scope",
            "",
            *scope_lines,
            "",
            "## Rerun Intent",
            "",
            f"- after_repair: `{rerun.get('after_repair', '')}`",
            f"- downstream_task_id: `{rerun.get('downstream_task_id', '')}`",
            f"- upstream_source_repair_task_id: `{rerun.get('upstream_source_repair_task_id', '')}`",
            "",
            "## Patch Intent",
            "",
            *patch_lines,
            "",
            "## Required Context",
            "",
            *[f"- `{item}`" for item in plan.get("required_context", [])],
            "",
        ],
    )


def write_source_repair_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    plan_id = _normalize(plan.get("plan_id")) or "unknown_source_repair_plan"
    target_dir = continuation_source_repair_plan_dir(plan_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    plan_path = target_dir / "source_repair_plan.json"
    request_path = target_dir / "codex_patch_request.md"
    patch_intent_path = target_dir / PATCH_INTENT_FILENAME
    patch_intent = plan.get("patch_intent")
    patch_intent_payload = json.dumps(patch_intent, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n" if patch_intent is not None else None
    expected_plan_has_patch_intent = patch_intent is not None

    serialized = json.dumps(plan, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if plan_path.exists():
        existing = plan_path.read_text(encoding="utf-8")
        if existing == serialized and request_path.exists():
            return {
                "status": "replayed",
                "plan_id": plan_id,
                "plan_dir": str(target_dir),
                "plan_path": str(plan_path),
                "codex_patch_request_path": str(request_path),
                "patch_intent_path": str(patch_intent_path) if expected_plan_has_patch_intent else None,
                "side_effects": dict(DEFAULT_SIDE_EFFECTS),
            }
        if existing != serialized:
            return {
                "status": "blocked",
                "reason": "source_repair_plan_collision",
                "plan_id": plan_id,
                "plan_dir": str(target_dir),
                "plan_path": str(plan_path),
                "side_effects": dict(DEFAULT_SIDE_EFFECTS),
            }

    plan_path.write_text(serialized, encoding="utf-8")
    request_path.write_text(render_codex_patch_request(plan), encoding="utf-8")
    if expected_plan_has_patch_intent and patch_intent is not None:
        patch_intent_path.write_text(patch_intent_payload, encoding="utf-8")
    return {
        "status": "written",
        "plan_id": plan_id,
        "plan_dir": str(target_dir),
        "plan_path": str(plan_path),
        "codex_patch_request_path": str(request_path),
        "patch_intent_path": str(patch_intent_path) if expected_plan_has_patch_intent else None,
        "side_effects": dict(DEFAULT_SIDE_EFFECTS),
    }


def materialize_source_repair_patch(
    plan: Dict[str, Any],
    *,
    output_dir: Path | str,
    repo_root: Path | str | None = None,
) -> Dict[str, Any]:
    """Render a source-repair patch artifact without applying it."""

    patch_intent = plan.get("patch_intent")
    if not isinstance(patch_intent, dict):
        return {
            "status": "blocked",
            "reason": "patch_intent_missing",
            "side_effects": dict(DEFAULT_SIDE_EFFECTS),
        }
    target_file = _normalize(patch_intent.get("target_file"))
    write_scope = [target_file] if target_file else []
    if not write_scope:
        return {
            "status": "blocked",
            "reason": "target_file_missing",
            "side_effects": dict(DEFAULT_SIDE_EFFECTS),
        }
    root = Path(repo_root) if repo_root is not None else orchestrator_paths.root()
    patch_result = build_patch_from_intent(root, patch_intent, write_scope=write_scope)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    quality_path = output_path / PATCH_QUALITY_REPORT_FILENAME
    patch_path = output_path / PATCH_DIFF_FILENAME
    quality_payload = {
        "schema_version": 1,
        "plan_id": plan.get("plan_id"),
        "source_task_id": plan.get("source_task_id"),
        "repair_type": plan.get("repair_type"),
        "ok": patch_result.ok,
        "errors": patch_result.errors,
        "quality_report": patch_result.quality_report,
        "target_file": target_file,
        "side_effects": dict(DEFAULT_SIDE_EFFECTS),
    }
    quality_path.write_text(json.dumps(quality_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if patch_result.patch_text:
        patch_path.write_text(patch_result.patch_text, encoding="utf-8")
    return {
        "status": "written" if patch_result.ok else "blocked",
        "patch_path": str(patch_path) if patch_result.patch_text else None,
        "quality_report_path": str(quality_path),
        "ok": patch_result.ok,
        "errors": patch_result.errors,
        "side_effects": dict(DEFAULT_SIDE_EFFECTS),
    }


__all__ = [
    "build_claim_boundary_report",
    "build_repair_design_packet",
    "build_repair_design_validation_report",
    "build_rerun_intent_from_repair_design_validation",
    "build_source_repair_plan",
    "build_source_repair_plan_from_action_request",
    "build_source_repair_plan_from_taskbook",
    "materialize_source_repair_patch",
    "render_codex_patch_request",
    "write_repair_design_packet",
    "write_repair_design_validation",
    "write_source_repair_plan",
]
