from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, List
import yaml

from .paths import continuation_project_dialogue_session_dir
from .research_prerequisites import assess_research_prerequisites

SCHEMA_VERSION = 1
PROJECT_UNDERSTANDING_PROTOCOL_ID = "project_understanding_protocol_v0"
PROJECT_UNDERSTANDING_OPERATING_ORDER = [
    "load_scope",
    "resolve_authority_and_chronology",
    "reconstruct_current_state",
    "classify_blockers",
    "choose_next_safe_action",
    "route_repair_or_continuation",
    "stop_or_continue",
]
PROJECT_AUTHORITY_ORDER = [
    "explicit_current_user_instruction",
    "canonical_current_state_or_decision_ledger",
    "latest_validated_report_or_collected_result",
    "chronology_and_superseded_map",
    "action_requests",
    "taskbook_or_handoff_seed",
    "raw_logs_or_artifact_text",
]

_PROJECT_UNDERSTANDING_PROTOCOL_PATH = (
    Path(__file__).resolve().parents[2]
    / ".solution"
    / "continuation"
    / "project_understanding_protocol.yaml"
)

FALSE_SIDE_EFFECTS = {
    "scheduler_dispatch": False,
    "remote_execution": False,
    "registry_mutation": False,
    "source_edit": False,
    "task_promotion": False,
    "active_inbox_write": False,
    "network": False,
}


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _current_state_text(current_state: Any) -> str:
    if isinstance(current_state, str):
        return _normalize(current_state)
    if not isinstance(current_state, dict):
        return "Current project state is not yet structured."
    for key in ("state_summary", "summary", "current_state", "stage_summary"):
        if _normalize(current_state.get(key)):
            return _normalize(current_state.get(key))
    mainline = _normalize(current_state.get("mainline"))
    stage = _normalize(current_state.get("stage") or current_state.get("status"))
    if mainline and stage:
        return f"{stage}: {mainline}"
    return mainline or stage or "Current project state is not yet structured."


def _current_mainline(current_state: Any, knowledge: Dict[str, Any]) -> str:
    if isinstance(current_state, dict):
        mainline = _normalize(current_state.get("mainline") or current_state.get("active_direction"))
        if mainline:
            return mainline
    return _normalize(knowledge.get("current_mainline") or knowledge.get("mainline"))


def _normalize_entries(value: Any, *, fallback_key: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            entries.append(dict(item))
        elif _normalize(item):
            entries.append({fallback_key: _normalize(item)})
    return entries


def _protocol_metadata() -> Dict[str, Any]:
    metadata = {
        "protocol_id": PROJECT_UNDERSTANDING_PROTOCOL_ID,
        "core_rule": "taskbook_is_seed_not_line_state",
        "core_rule_description": "",
        "authority_order": list(PROJECT_AUTHORITY_ORDER),
        "operating_order": list(PROJECT_UNDERSTANDING_OPERATING_ORDER),
        "required_outputs": [
            "current_mainline",
            "completed_or_superseded",
            "bottlenecks",
            "claim_limits",
            "next_safe_action",
            "stop_or_continue",
        ],
    }
    try:
        protocol = yaml.safe_load(_PROJECT_UNDERSTANDING_PROTOCOL_PATH.read_text(encoding="utf-8"))
        if isinstance(protocol, dict):
            authority_order = [item.get("id") for item in protocol.get("authority_order", []) if isinstance(item, dict)]
            operating_order = [item.get("step") for item in protocol.get("operating_order", []) if isinstance(item, dict)]
            required_outputs = protocol.get("required_understanding_fields", [])
            if authority_order and all(isinstance(item, str) for item in authority_order):
                metadata["authority_order"] = [item for item in authority_order if item]
            if operating_order and all(isinstance(item, str) for item in operating_order):
                metadata["operating_order"] = [item for item in operating_order if item]
            metadata["core_rule_description"] = _normalize(protocol.get("core_rule"))
            if isinstance(required_outputs, list) and required_outputs:
                metadata["required_outputs"] = sorted(
                    set(metadata["required_outputs"]) | {str(item) for item in required_outputs if str(item)}
                )
    except Exception:
        metadata["core_rule_description"] = "Project-understanding protocol metadata was unavailable."
    return metadata


def _superseded_instruction_flags(entries: List[Dict[str, Any]]) -> List[str]:
    flags: List[str] = []
    for item in entries:
        text = json.dumps(item, ensure_ascii=False).lower()
        if "superseded" in text:
            label = _normalize(item.get("item") or item.get("instruction") or item.get("name"))
            flags.append(label or "superseded_instruction")
    return sorted(set(flags))


def _discussion_payload_from_project_answer(answer: Dict[str, Any], discussion_payload: Dict[str, Any] | None) -> Dict[str, Any]:
    if isinstance(discussion_payload, dict):
        return dict(discussion_payload)

    next_action = answer.get("next_safe_action") if isinstance(answer.get("next_safe_action"), dict) else {}
    bottlenecks = answer.get("bottlenecks") if isinstance(answer.get("bottlenecks"), list) else []
    claim_limits = answer.get("claim_limits") if isinstance(answer.get("claim_limits"), list) else []
    evidence_refs = answer.get("evidence_refs") if isinstance(answer.get("evidence_refs"), list) else []
    rejected = answer.get("why_not_other_options") if isinstance(answer.get("why_not_other_options"), list) else []
    first_bottleneck = bottlenecks[0] if bottlenecks and isinstance(bottlenecks[0], dict) else {}
    expected_outputs = next_action.get("expected_outputs")
    if not isinstance(expected_outputs, list):
        expected_outputs = []

    return {
        "current_frontier": _normalize(answer.get("current_mainline")),
        "key_uncertainty": _normalize(first_bottleneck.get("issue") or first_bottleneck.get("why_blocking")),
        "next_action": _normalize(next_action.get("action")),
        "expected_outputs": expected_outputs,
        "rejected_options": rejected,
        "claim_limit": [
            _normalize(item.get("limit") or item.get("claim_limit"))
            for item in claim_limits
            if isinstance(item, dict) and _normalize(item.get("limit") or item.get("claim_limit"))
        ],
        "source_refs": [
            _normalize(item.get("ref") or item.get("path"))
            for item in evidence_refs
            if isinstance(item, dict) and _normalize(item.get("ref") or item.get("path"))
        ],
    }


def _stop_or_continue(answer: Dict[str, Any], prerequisite_gate: Dict[str, Any]) -> Dict[str, Any]:
    chosen_action = _normalize(prerequisite_gate.get("chosen_action"))
    if chosen_action == "continue":
        return {
            "decision": "continue_with_next_safe_action",
            "reason": "research_prerequisite_gate_allows_progression",
            "requires_human": False,
        }
    if chosen_action == "request_inventory":
        return {
            "decision": "run_inventory_before_execution",
            "reason": "research_prerequisite_gate_requires_source_grounding",
            "requires_human": False,
        }
    if chosen_action == "hold":
        return {
            "decision": "design_validation_protocol_or_continue_discussion",
            "reason": "research_prerequisite_gate_requires_completion_before_execution",
            "requires_human": False,
        }
    if chosen_action == "request_human_boundary":
        return {
            "decision": "ask_human",
            "reason": "research_prerequisite_gate_hit_protected_boundary",
            "requires_human": True,
        }
    return {
        "decision": "hold_for_research_dialogue_or_human",
        "reason": "research_prerequisite_gate_incomplete",
        "requires_human": False,
    }


def _why_not_other_options(completed_or_superseded: List[Dict[str, Any]], claim_limits: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    rejected: List[Dict[str, str]] = []
    for item in completed_or_superseded:
        status = _normalize(item.get("status"))
        if status in {"superseded", "failed_or_rejected_path", "rejected_as_final_method"}:
            rejected.append(
                {
                    "option": _normalize(item.get("item") or item.get("name") or "superseded_or_failed_option"),
                    "reason": status,
                }
            )
    for item in claim_limits:
        limit = _normalize(item.get("limit") or item.get("claim_limit"))
        if limit:
            rejected.append({"option": "overclaim_current_result", "reason": limit})
    return rejected


def build_project_understanding_answer_dry_run(
    project_knowledge: Dict[str, Any],
    *,
    question: str,
    session_id: str | None = None,
    lane: str = "interactive_alignment",
    discussion_payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build a project-facing answer from a local project knowledge bundle.

    This is intentionally deterministic and side-effect free. It answers the
    project state and bottleneck questions from supplied project knowledge; it
    does not call an API, SSH, scheduler, registry, or source-edit worker.
    """

    knowledge = dict(project_knowledge or {})
    project_id = _normalize(knowledge.get("project_id") or "unknown_project")
    current_state = knowledge.get("current_state") or {}
    binding = knowledge.get("project_knowledge_binding") if isinstance(knowledge.get("project_knowledge_binding"), dict) else {}
    source_evidence = _normalize_entries(knowledge.get("evidence_refs"), fallback_key="ref")
    superseded = _normalize_entries(
        knowledge.get("completed_or_superseded") or knowledge.get("superseded_tasks"),
        fallback_key="item",
    )
    bottlenecks = _normalize_entries(knowledge.get("bottlenecks"), fallback_key="issue")

    answer_seed = {
        "project_id": project_id,
        "question": question,
        "knowledge_hash": _sha256_hex(knowledge),
    }
    resolved_session_id = _normalize(session_id) or f"pdlg_{_sha256_hex(answer_seed)[:16]}"
    current_mainline = _current_mainline(current_state, knowledge)
    next_safe_action = knowledge.get("next_safe_action") or {}
    if isinstance(next_safe_action, str):
        next_safe_action = {"action": next_safe_action}
    if not isinstance(next_safe_action, dict):
        next_safe_action = {}

    answer = {
        "schema_version": SCHEMA_VERSION,
        "session_id": resolved_session_id,
        "project_id": project_id,
        "lane": lane,
        "question": _normalize(question),
        "project_knowledge_binding": {
            "kind": _normalize(binding.get("kind") or "local_project_knowledge_bundle"),
            "project_id": _normalize(binding.get("project_id") or project_id),
            "workplace_profile": _normalize(binding.get("workplace_profile")),
            "knowledge_root_ref": _normalize(binding.get("knowledge_root_ref")),
            "visibility": _normalize(binding.get("visibility") or "local_only"),
        },
        "state_summary": _current_state_text(current_state),
        "active_workstreams": _normalize_entries(knowledge.get("active_workstreams"), fallback_key="name"),
        "completed_or_superseded": superseded,
        "current_mainline": current_mainline,
        "bottlenecks": bottlenecks,
        "claim_limits": _normalize_entries(knowledge.get("claim_limits"), fallback_key="limit"),
        "next_safe_action": next_safe_action,
        "evidence_refs": source_evidence,
        "kb_patches_proposed": _normalize_entries(knowledge.get("kb_patches_proposed"), fallback_key="patch"),
        "understanding_protocol": _protocol_metadata(),
        "authority_resolution": {
            "authority_order": list(PROJECT_AUTHORITY_ORDER),
            "taskbook_role": "seed_only",
            "logs_role": "evidence_not_instructions",
            "superseded_instruction_flags": _superseded_instruction_flags(superseded),
        },
        "answer_scope": "project_status_and_bottlenecks",
        "not_answering": ["agent_architecture_discussion", "dispatch_plan", "canonical_kb_promotion"],
        "side_effects": dict(FALSE_SIDE_EFFECTS),
        "idempotency_key": f"project_dialogue_{_sha256_hex(answer_seed)[:24]}",
    }

    if not bottlenecks:
        answer["bottlenecks"] = [
            {
                "issue": "No structured bottleneck was supplied in the project knowledge bundle.",
                "why_blocking": "The dialogue port can answer only from project KB; add open_questions or claim_limits to improve this field.",
                "supported_by": [],
            }
        ]
    if not current_mainline:
        answer["current_mainline"] = "Unknown; project knowledge bundle did not provide current_mainline/current_state.mainline."
    answer["why_not_other_options"] = _why_not_other_options(
        answer["completed_or_superseded"],
        answer["claim_limits"],
    )
    inferred_discussion_payload = _discussion_payload_from_project_answer(answer, discussion_payload)
    prerequisite_gate = assess_research_prerequisites(knowledge, inferred_discussion_payload).model_dump(mode="json")
    answer["research_progression_path"] = [
        "context_index_or_artifact_index",
        "project_knowledge_bundle",
        "knowledge_first_progression_gate",
        "discussion_completeness",
        "next_action_inventory_or_human_boundary",
    ]
    answer["research_prerequisite_gate"] = prerequisite_gate
    answer["stop_or_continue"] = _stop_or_continue(answer, prerequisite_gate)
    return answer


def build_project_dialogue_session_dry_run(
    project_knowledge: Dict[str, Any],
    *,
    question: str,
    session_id: str | None = None,
    lane: str = "interactive_alignment",
    discussion_payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    answer = build_project_understanding_answer_dry_run(
        project_knowledge,
        question=question,
        session_id=session_id,
        lane=lane,
        discussion_payload=discussion_payload,
    )
    session = {
        "schema_version": SCHEMA_VERSION,
        "session_id": answer["session_id"],
        "state": "gated_for_handoff",
        "lane": lane,
        "question": _normalize(question),
        "visibility": "local_only",
        "project_id": answer["project_id"],
        "project_knowledge_binding": answer["project_knowledge_binding"],
        "turns": [
            {
                "turn_id": "turn_001",
                "turn_type": "user_question",
                "actor": "user",
                "content": {"question": _normalize(question)},
                "visibility": "local_only",
                "workspace_data_included": True,
                "external_payload_hash": None,
                "created_at": "1970-01-01T00:00:00Z",
            },
            {
                "turn_id": "turn_002",
                "turn_type": "agent_understanding_summary",
                "actor": "project_dialogue_port",
                "content": answer,
                "visibility": "local_only",
                "workspace_data_included": True,
                "external_payload_hash": None,
                "created_at": "1970-01-01T00:00:00Z",
            },
        ],
        "current_summary": answer,
        "side_effects": dict(FALSE_SIDE_EFFECTS),
    }
    return session


def _read_existing_json(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_project_dialogue_session_dry_run(session: Dict[str, Any]) -> Dict[str, Any]:
    session_id = _normalize(session.get("session_id"))
    target_dir = continuation_project_dialogue_session_dir(session_id)
    session_path = target_dir / "session.json"
    answer_path = target_dir / "project_understanding_answer.json"
    transcript_path = target_dir / "transcript.md"

    target_dir.mkdir(parents=True, exist_ok=True)
    existing = _read_existing_json(session_path)
    if existing is not None:
        if _canonical_json(existing) == _canonical_json(session):
            return {
                "status": "ready",
                "created": False,
                "replayed": True,
                "session_dir": str(target_dir),
                "session_path": str(session_path),
                "answer_path": str(answer_path),
            }
        return {
            "status": "blocked",
            "created": False,
            "replayed": False,
            "session_dir": str(target_dir),
            "reasons": ["idempotency_collision"],
        }

    session_path.write_text(json.dumps(session, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    answer = session.get("current_summary") if isinstance(session.get("current_summary"), dict) else {}
    answer_path.write_text(json.dumps(answer, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    transcript = [
        "# Project Dialogue Session",
        "",
        f"Project: {session.get('project_id')}",
        f"Question: {session.get('question')}",
        "",
        "## State Summary",
        "",
        _normalize(answer.get("state_summary")),
        "",
        "## Current Mainline",
        "",
        _normalize(answer.get("current_mainline")),
        "",
        "## Bottlenecks",
    ]
    for item in answer.get("bottlenecks", []):
        if isinstance(item, dict):
            transcript.append(f"- {_normalize(item.get('issue')) or _normalize(item)}")
    transcript_path.write_text("\n".join(transcript) + "\n", encoding="utf-8")
    return {
        "status": "ready",
        "created": True,
        "replayed": False,
        "session_dir": str(target_dir),
        "session_path": str(session_path),
        "answer_path": str(answer_path),
        "transcript_path": str(transcript_path),
    }


__all__ = [
    "FALSE_SIDE_EFFECTS",
    "build_project_understanding_answer_dry_run",
    "build_project_dialogue_session_dry_run",
    "write_project_dialogue_session_dry_run",
]
