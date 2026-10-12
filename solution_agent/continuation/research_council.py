from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import yaml

from . import proposal_staging
from .paths import (
    continuation_research_council_flow_path,
    continuation_research_council_run_path,
    continuation_research_council_summary_path,
    solution_continuation_dir,
)

RESEARCH_COUNCIL_SCHEMA_VERSION = 1

DEFAULT_FLOW_ID = "research_council_v0"
DEFAULT_RUN_ID_PREFIX = "research_council_run"

ALLOWED_STAGE_TYPES = {
    "deterministic_retrieval",
    "role",
    "review",
    "synthesis",
    "deterministic_validator",
}

ALLOWED_REVIEW_DECISIONS = {"pass", "fail"}

ALLOWED_OUTPUTS = {
    "case_memory_excerpt",
    "chief_plan",
    "evidence_review",
    "skeptic_review",
    "risk_review",
    "synthesis",
    "final_gate_report",
    "council_decision",
}

FORBIDDEN_ACTIONS = {
    "write_inbox",
    "promote",
    "dispatch",
    "scheduler_submit",
    "registry_mutation",
    "remote_execution",
    "source_edit",
    "path_guessing",
    "credential_request",
    "auto_promotion",
    "active_inbox_write",
}

FORBIDDEN_TEXT_MARKERS = {
    "write_inbox",
    "promote",
    "direct_dispatch",
    "dispatch",
    "scheduler_submit",
    "registry_mutation",
    "remote_execution",
    "source_edit",
    "path_guess",
    "credential_request",
    "api_key",
    "secret",
    "password",
    "token",
    "auto_promote",
}

PATH_GUESSING_PATTERNS = (
    re.compile(r"^[A-Za-z]:[\\/]", re.IGNORECASE),
    re.compile(r"^\\\\"),
    re.compile(r"^/"),
    re.compile(r"\.\."),
    re.compile(r"(?i)/tmp"),
    re.compile(r"(?i)/ssd"),
    re.compile(r"(?i)/mnt"),
    re.compile(r"(?i)/raid"),
    re.compile(r"(?i)/home/"),
)

SIDE_EFFECTS_NONE = {
    "write_inbox": False,
    "promote": False,
    "dispatch": False,
    "scheduler_submit": False,
    "registry_mutation": False,
    "remote_execution": False,
    "source_edit": False,
    "path_guessing": False,
    "credential_request": False,
    "subprocess": False,
    "network": False,
}

RESEARCH_COUNCIL_PROVIDER_ALLOWLIST = {
    "deepseek-v4-pro": {"provider": "deepseek", "model": "v4-pro", "enabled": True},
    "qwen-flash": {"provider": "qwen", "model": "flash", "enabled": True},
    "qwen3-coder-next": {"provider": "qwen3", "model": "coder-next", "enabled": True},
    "deepseek-v4-flash": {"provider": "deepseek", "model": "v4-flash", "enabled": True},
}

RESEARCH_COUNCIL_DEFAULT_PROVIDER_ROUTING = {
    "chief_planner": "deepseek-v4-pro",
    "evidence_grounder": "qwen-flash",
    "skeptic_reviewer": "deepseek-v4-pro",
    "risk_policy_review": "deepseek-v4-pro",
    "chief_synthesizer": "deepseek-v4-pro",
}

RESEARCH_COUNCIL_PROVIDER_STAGE_TYPES = {"role", "review", "synthesis"}

PROMPT_CONTRACT_CACHE: Dict[str, Dict[str, Any]] = {}


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_lower(value: Any) -> str:
    return _normalize(value).lower()


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _read_payload(payload: dict | Path | str) -> Dict[str, Any]:
    if isinstance(payload, (Path, str)):
        loaded = json.loads(Path(payload).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError("payload must be a JSON object")
        return loaded
    if isinstance(payload, dict):
        return dict(payload)
    raise TypeError("payload must be a dict, Path, or string path")


def _coerce_next_step_readiness(from_payload: Dict[str, Any] | Any) -> Dict[str, Any] | None:
    readiness = from_payload.get("next_step_readiness") if isinstance(from_payload, dict) else None
    return dict(readiness) if isinstance(readiness, dict) else None


def _read_existing_json(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _extract_json_object(raw: str) -> str:
    text = _normalize(raw)
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    candidate = (fenced.group(1) if fenced else text).strip()
    if candidate.startswith("{") and candidate.endswith("}"):
        return candidate
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        return candidate[start : end + 1]
    return ""


def _policy_root() -> Path:
    configured = solution_continuation_dir()
    fallback = Path(__file__).resolve().parents[2] / ".solution" / "continuation"
    if configured.exists():
        return configured
    if fallback.exists():
        return fallback
    return configured


def _policy_file(filename: str) -> Path:
    configured = _policy_root() / filename
    if configured.exists():
        return configured
    fallback = Path(__file__).resolve().parents[2] / ".solution" / "continuation" / filename
    if fallback.exists():
        return fallback
    return configured


def _load_yaml(path: Path) -> Dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return loaded


def _parse_prompt_front_matter(prompt_path: Path) -> Dict[str, Any]:
    if not prompt_path.exists():
        return {}
    text = prompt_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    if text.count("---") < 2:
        return {}
    _, front, _ = text.split("---", 2)
    loaded = yaml.safe_load(front)
    return loaded if isinstance(loaded, dict) else {}


def _prompt_contract_for_key(key: str) -> Dict[str, Any]:
    if not key:
        return {}
    normalized = _normalize_lower(key)
    if normalized in PROMPT_CONTRACT_CACHE:
        return PROMPT_CONTRACT_CACHE[normalized]

    candidate_path = (
        _policy_root()
        / "prompts"
        / "research_council"
        / f"{normalized}.prompt.md"
    )
    payload = _parse_prompt_front_matter(candidate_path)
    PROMPT_CONTRACT_CACHE[normalized] = payload
    return payload


def _prompt_contract_for_stage(stage: Dict[str, Any]) -> Dict[str, Any]:
    role = _normalize_lower(stage.get("role"))
    stage_id = _normalize_lower(stage.get("id"))
    contract = _prompt_contract_for_key(role)
    if contract:
        return contract
    return _prompt_contract_for_key(stage_id)


def _provider_model_from_label(label: str) -> Tuple[str, str] | None:
    normalized = _normalize(label)
    if "-" not in normalized:
        return None
    provider, model = normalized.split("-", 1)
    if not provider or not model:
        return None
    return provider, model


def _provider_label_allowed(label: str) -> bool:
    parts = _provider_model_from_label(label)
    if not parts:
        return False
    entry = RESEARCH_COUNCIL_PROVIDER_ALLOWLIST.get(_normalize_lower(label))
    if not isinstance(entry, dict):
        return False
    if not _normalize_lower(entry.get("enabled")) in {"", "true", "1", "yes", "on"}:
        return False
    provider, model = parts
    return (
        _normalize_lower(entry.get("provider")) == _normalize_lower(provider)
        and _normalize_lower(entry.get("model")) == _normalize_lower(model)
    )


def _resolve_provider_model(
    stage: Dict[str, Any],
    provider_routing: Dict[str, Any] | None = None,
) -> Tuple[str, str, str] | Tuple[None, None, str]:
    stage_id = _normalize_lower(stage.get("id"))
    role = _normalize_lower(stage.get("role"))
    routing = _normalize_lower(provider_routing.get(stage_id) if isinstance(provider_routing, dict) else "")
    if not routing:
        routing = _normalize_lower(provider_routing.get(role) if isinstance(provider_routing, dict) else "")
    if not routing:
        routing = _normalize_lower(
            RESEARCH_COUNCIL_DEFAULT_PROVIDER_ROUTING.get(stage_id)
            or RESEARCH_COUNCIL_DEFAULT_PROVIDER_ROUTING.get(role)
            or "deepseek-v4-pro"
        )

    parts: Tuple[str, str] | None = None
    if isinstance(provider_routing, dict):
        maybe_entry = provider_routing.get(stage_id) or provider_routing.get(role)
        if isinstance(maybe_entry, dict):
            provider = _normalize_lower(maybe_entry.get("provider"))
            model = _normalize_lower(maybe_entry.get("model"))
            if provider and model:
                routing = f"{provider}-{model}"
                parts = (provider, model)
        elif isinstance(maybe_entry, str):
            routing = _normalize_lower(maybe_entry)

    if not parts:
        parts = _provider_model_from_label(routing)
    if parts is None:
        return (None, None, routing)
    provider, model = parts
    if _provider_label_allowed(f"{provider}-{model}"):
        return (provider, model, f"{provider}-{model}")
    return (None, None, routing)


def load_research_council_flow_contract() -> Dict[str, Any]:
    return _load_yaml(_policy_file("research_council_flow.yaml"))


def load_research_council_policy() -> Dict[str, Any]:
    return _load_yaml(_policy_file("research_council_policy.yaml"))


def _payload_for_replay(payload: Dict[str, Any]) -> Dict[str, Any]:
    candidate = dict(payload)
    for key in ("created", "replayed", "flow_path", "run_path", "summary_path", "write_status"):
        candidate.pop(key, None)
    return candidate


def _is_same_payload(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return _canonical_json(_payload_for_replay(left)) == _canonical_json(_payload_for_replay(right))


def _looks_like_path_guessing(text: str) -> bool:
    normalized = _normalize(text)
    return any(pattern.search(normalized) for pattern in PATH_GUESSING_PATTERNS)


def _forbidden_findings(value: Any) -> list[str]:
    if value is None or isinstance(value, (bool, int, float)):
        return []
    findings: list[str] = []
    if isinstance(value, str):
        normalized = _normalize_lower(value)
        for marker in FORBIDDEN_TEXT_MARKERS:
            if marker in normalized:
                findings.append(f"forbidden_text:{marker}")
        if _looks_like_path_guessing(value):
            findings.append("path_guessing_detected")
        return findings
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = _normalize_lower(key)
            if normalized_key in FORBIDDEN_ACTIONS:
                findings.append(f"forbidden_field:{normalized_key}")
            findings.extend(_forbidden_findings(item))
        return findings
    if isinstance(value, list):
        for item in value:
            findings.extend(_forbidden_findings(item))
    return findings


def parse_research_council_review_decision(raw: str) -> Dict[str, Any]:
    """Parse a reviewer reply into a strict pass/fail decision contract."""
    raw_json = _extract_json_object(raw)
    if not raw_json:
        return {"ok": False, "error": "review_response_missing_json"}
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return {"ok": False, "error": "review_response_invalid_json"}
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "review_response_must_be_object"}

    decision = _normalize_lower(parsed.get("decision"))
    if decision not in ALLOWED_REVIEW_DECISIONS:
        return {"ok": False, "error": "review_decision_must_be_pass_or_fail"}
    reason = _normalize(parsed.get("reason"))
    if not reason:
        return {"ok": False, "error": "review_reason_required"}
    failed_criteria = parsed.get("failed_criteria", parsed.get("failedCriteria"))
    if not isinstance(failed_criteria, list) or not all(isinstance(item, str) for item in failed_criteria):
        return {"ok": False, "error": "review_failed_criteria_must_be_string_array"}
    next_round_instruction = _normalize(
        parsed.get("next_round_instruction", parsed.get("nextRoundInstruction"))
    )
    if decision == "fail" and not next_round_instruction:
        return {"ok": False, "error": "review_next_round_instruction_required_on_fail"}
    if decision == "pass" and next_round_instruction:
        return {"ok": False, "error": "review_next_round_instruction_must_be_empty_on_pass"}

    return {
        "ok": True,
        "decision": {
            "decision": decision,
            "reason": reason,
            "failed_criteria": failed_criteria,
            "next_round_instruction": next_round_instruction,
            "raw_json": raw_json,
        },
    }


def validate_research_council_provider_response(
    response: Any,
    *,
    stage_id: str | None = None,
    stage_type: str | None = None,
) -> Dict[str, Any]:
    """Validate mock provider response payload before using it in the run."""
    stage = _normalize_lower(stage_id)
    stage_type_norm = _normalize_lower(stage_type)
    reasons: list[str] = []
    review_decision: Dict[str, Any] | None = None
    response_payload = response

    if response_payload is None:
        reasons.append("provider_response_missing")
        return {
            "status": "rejected",
            "stage_id": stage,
            "stage_type": stage_type_norm,
            "reasons": reasons,
        }

    if isinstance(response_payload, str):
        response_payload = {"content": response_payload}

    if not isinstance(response_payload, dict):
        reasons.append("provider_response_not_mapping")
        response_payload = {}

    if stage_type_norm == "review":
        review_input = response_payload.get("raw") or response_payload.get("content") or response_payload.get("text")
        if review_input is None and {"decision", "reason", "failed_criteria", "next_round_instruction"} <= response_payload.keys():
            review_input = json.dumps(
                {
                    "decision": response_payload.get("decision"),
                    "reason": response_payload.get("reason"),
                    "failedCriteria": response_payload.get("failed_criteria", response_payload.get("failedCriteria", [])),
                    "nextRoundInstruction": response_payload.get("next_round_instruction", response_payload.get("nextRoundInstruction", "")),
                }
            )
        if review_input is None:
            reasons.append("review_response_missing_json")
        else:
            parsed = parse_research_council_review_decision(str(review_input))
            if not parsed["ok"]:
                reasons.append(f"review_{parsed['error']}")
            else:
                review_decision = parsed["decision"]

    for reason in _forbidden_findings(response_payload):
        if reason not in reasons:
            reasons.append(reason)

    if reasons:
        status = "rejected"
    else:
        status = "valid"

    return {
        "status": status,
        "stage_id": stage,
        "stage_type": stage_type_norm,
        "reasons": sorted(set(reasons)),
        "provider_response": response_payload,
        **({"review_decision": review_decision} if review_decision else {}),
    }


def _stage_ids(flow: Dict[str, Any]) -> set[str]:
    return {_normalize(stage.get("id")) for stage in flow.get("stages", []) if isinstance(stage, dict)}


def _reachable_stage_ids(stage_ids: set[str], edges: list[dict[str, str]]) -> set[str]:
    dependency_edges = [edge for edge in edges if edge.get("branch") != "fail"]
    targets = {edge["to"] for edge in dependency_edges}
    roots = sorted(stage_ids - targets)
    if not roots and stage_ids:
        roots = [sorted(stage_ids)[0]]
    reachable = set(roots)
    frontier = list(roots)
    while frontier:
        source = frontier.pop(0)
        for edge in dependency_edges:
            if edge["from"] != source or edge["to"] in reachable:
                continue
            reachable.add(edge["to"])
            frontier.append(edge["to"])
    return reachable


def _stage_by_id(flow: Dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _normalize(stage.get("id")): stage
        for stage in flow.get("stages", [])
        if isinstance(stage, dict) and _normalize(stage.get("id"))
    }


def _review_stages(flow: Dict[str, Any]) -> list[dict[str, Any]]:
    return [
        stage
        for stage in flow.get("stages", [])
        if isinstance(stage, dict) and _normalize(stage.get("type")) == "review"
    ]


def _first_stage_by_type(flow: Dict[str, Any], stage_type: str) -> dict[str, Any] | None:
    for stage in flow.get("stages", []):
        if isinstance(stage, dict) and _normalize(stage.get("type")) == stage_type:
            return stage
    return None


def _decision_for_round(decisions: dict[str, Any], stage_id: str, round_index: int) -> str:
    configured = decisions.get(stage_id, "pass")
    if isinstance(configured, (list, tuple)):
        value = configured[min(round_index, len(configured) - 1)] if configured else "pass"
    else:
        value = configured
    decision = _normalize_lower(value)
    return decision if decision in ALLOWED_REVIEW_DECISIONS else "fail"


def _append_stage_run(
    *,
    stage_runs: list[dict[str, Any]],
    transcript: list[dict[str, Any]],
    stage: dict[str, Any],
    execution_index: int,
    round_index: int,
    status: str = "completed",
    content: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    stage_id = _normalize(stage.get("id"))
    stage_type = _normalize(stage.get("type"))
    output_ref = _normalize(stage.get("output")) or f"{stage_id}.yaml"
    stage_run = {
        "stage_id": stage_id,
        "stage_execution_index": execution_index,
        "round_index": round_index,
        "stage_type": stage_type,
        "status": status,
        "output_ref": output_ref,
    }
    if extra:
        stage_run.update(extra)
    stage_runs.append(stage_run)
    transcript.append(
        {
            "stage_id": stage_id,
            "round_index": round_index,
            "role": _normalize(stage.get("role")) or stage_type,
            "content": content or f"deterministic output for {stage_id}",
            **(extra or {}),
        }
    )


def validate_research_council_flow(flow: dict | Path | str) -> Dict[str, Any]:
    payload = _read_payload(flow)
    reasons: list[str] = []

    if payload.get("schema_version") != RESEARCH_COUNCIL_SCHEMA_VERSION:
        reasons.append("schema_version_mismatch")
    if _normalize(payload.get("mode")) != "advisory_only":
        reasons.append("mode_must_be_advisory_only")

    stages = payload.get("stages")
    if not isinstance(stages, list) or not stages:
        reasons.append("stages_required")
        stages = []

    seen_stage_ids: set[str] = set()
    review_stage_ids: set[str] = set()
    for stage in stages:
        if not isinstance(stage, dict):
            reasons.append("stage_must_be_object")
            continue
        stage_id = _normalize(stage.get("id"))
        stage_type = _normalize(stage.get("type"))
        if not stage_id:
            reasons.append("stage_id_required")
        elif stage_id in seen_stage_ids:
            reasons.append(f"duplicate_stage_id:{stage_id}")
        seen_stage_ids.add(stage_id)
        if stage_type not in ALLOWED_STAGE_TYPES:
            reasons.append(f"unsupported_stage_type:{stage_type or '<missing>'}")
        if stage_type == "review":
            review_stage_ids.add(stage_id)
            review = stage.get("review")
            if not isinstance(review, dict):
                reasons.append(f"review_config_required:{stage_id}")
            else:
                if not _normalize(review.get("pass_output")):
                    reasons.append(f"review_pass_output_required:{stage_id}")
                if not _normalize(review.get("fail_output")):
                    reasons.append(f"review_fail_output_required:{stage_id}")
                max_attempts = review.get("max_attempts")
                if not isinstance(max_attempts, int) or max_attempts < 1:
                    reasons.append(f"review_max_attempts_invalid:{stage_id}")

    edges = payload.get("edges")
    if not isinstance(edges, list) or not edges:
        reasons.append("edges_required")
        edges = []

    normalized_edges: list[dict[str, str]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            reasons.append("edge_must_be_object")
            continue
        source = _normalize(edge.get("from") or edge.get("sourceStageId"))
        target = _normalize(edge.get("to") or edge.get("targetStageId"))
        branch = _normalize(edge.get("branch") or edge.get("sourcePort"))
        if source and target:
            normalized_edges.append({"from": source, "to": target, "branch": branch})
        if source not in seen_stage_ids:
            reasons.append(f"edge_unknown_source:{source or '<missing>'}")
        if target not in seen_stage_ids:
            reasons.append(f"edge_unknown_target:{target or '<missing>'}")
        if source and target and source == target:
            reasons.append(f"edge_self_loop:{source}")
        if branch and branch not in ALLOWED_REVIEW_DECISIONS:
            reasons.append(f"edge_unsupported_branch:{branch}")
        if branch and source not in review_stage_ids:
            reasons.append(f"edge_branch_from_non_review:{source}")

    for review_stage_id in sorted(review_stage_ids):
        outgoing_branches = {
            edge["branch"]
            for edge in normalized_edges
            if edge["from"] == review_stage_id and edge["branch"] in ALLOWED_REVIEW_DECISIONS
        }
        if "pass" not in outgoing_branches:
            reasons.append(f"review_pass_branch_required:{review_stage_id}")
        if "fail" not in outgoing_branches:
            reasons.append(f"review_fail_branch_required:{review_stage_id}")

    valid_edge_refs = [
        edge for edge in normalized_edges
        if edge["from"] in seen_stage_ids and edge["to"] in seen_stage_ids and edge["from"] != edge["to"]
    ]
    if seen_stage_ids:
        dependency_targets = {edge["to"] for edge in valid_edge_refs if edge.get("branch") != "fail"}
        roots = sorted(seen_stage_ids - dependency_targets)
        expected_root = _normalize(stages[0].get("id")) if stages and isinstance(stages[0], dict) else ""
        unexpected_roots = [root for root in roots if root != expected_root]
        if len(roots) != 1 or unexpected_roots:
            reasons.append(f"unexpected_root_stages:{','.join(roots) or '<none>'}")
        unreachable = seen_stage_ids - _reachable_stage_ids(seen_stage_ids, valid_edge_refs)
        if unreachable:
            reasons.append(f"unreachable_stages:{','.join(sorted(unreachable))}")

    loop_policy = payload.get("loop_policy")
    if not isinstance(loop_policy, dict):
        reasons.append("loop_policy_required")
    else:
        max_rounds = loop_policy.get("max_rounds")
        if not isinstance(max_rounds, int) or max_rounds < 1 or max_rounds > 10:
            reasons.append("loop_policy_max_rounds_invalid")
        max_node_executions = loop_policy.get("max_node_executions")
        if not isinstance(max_node_executions, int) or max_node_executions < 1 or max_node_executions > 200:
            reasons.append("loop_policy_max_node_executions_invalid")

    forbidden_actions = set(payload.get("forbidden_actions") or [])
    missing_forbidden = FORBIDDEN_ACTIONS - forbidden_actions
    if missing_forbidden:
        reasons.append(f"forbidden_actions_missing:{','.join(sorted(missing_forbidden))}")

    return {
        "status": "valid" if not reasons else "rejected",
        "reasons": reasons,
        "stage_count": len(stages),
        "edge_count": len(edges),
        "review_stage_count": len(review_stage_ids),
        "advisory_only": _normalize(payload.get("mode")) == "advisory_only",
    }


def build_research_council_provider_invocations_dry_run(
    flow: dict | Path | str,
    discussion_pack: dict | None = None,
    *,
    provider_routing: Dict[str, Any] | None = None,
    run_id: str | None = None,
) -> Dict[str, Any]:
    council_flow = _read_payload(flow)
    validation = validate_research_council_flow(council_flow)
    if validation["status"] != "valid":
        return {
            "run_id": run_id or f"{DEFAULT_RUN_ID_PREFIX}_provider_blocked",
            "flow_id": _normalize(council_flow.get("flow_id")) or DEFAULT_FLOW_ID,
            "status": "blocked",
            "network_enabled": False,
            "api_call_executed": False,
            "invocations": [],
            "provider_count": 0,
            "reasons": validation["reasons"],
        }

    pack = discussion_pack or {}
    source_task_id = _normalize(pack.get("source_task_id")) or "unknown_source_task"
    flow_hash = _sha256_hex(council_flow)
    pack_hash = _sha256_hex(pack)
    actual_run_id = run_id or f"{DEFAULT_RUN_ID_PREFIX}_provider_{flow_hash[:10]}_{pack_hash[:10]}"

    invocations: list[dict[str, Any]] = []
    reasons: list[str] = []
    for stage in council_flow.get("stages", []):
        if not isinstance(stage, dict):
            continue
        stage_id = _normalize_lower(stage.get("id"))
        role = _normalize_lower(stage.get("role"))
        stage_type = _normalize_lower(stage.get("type"))
        if stage_type not in RESEARCH_COUNCIL_PROVIDER_STAGE_TYPES:
            continue
        provider, model, provider_model = _resolve_provider_model(stage, provider_routing=provider_routing or {})
        if provider is None or model is None:
            reasons.append(f"provider_not_allowlisted:{provider_model or stage_id}")
            continue

        contract = _prompt_contract_for_stage(stage)
        invocation = {
            "run_id": actual_run_id,
            "stage_id": stage_id,
            "role": role or stage_type,
            "stage_type": stage_type,
            "provider": provider,
            "model": model,
            "provider_model": provider_model,
            "prompt_contract_ref": _normalize(contract.get("prompt_id")) or f"research_council_{stage_id}",
            "trusted_inputs": list(contract.get("trusted_inputs") or [f"research_council_flow.yaml", "discussion_pack"]),
            "untrusted_inputs": list(contract.get("untrusted_inputs") or [f"discussion_pack_{source_task_id}", "evidence_refs"]),
            "forbidden_actions": sorted(FORBIDDEN_ACTIONS),
            "network_enabled": False,
            "api_call_executed": False,
        }
        invocations.append(invocation)

    if reasons:
        return {
            "run_id": actual_run_id,
            "flow_id": _normalize(council_flow.get("flow_id")) or DEFAULT_FLOW_ID,
            "source_task_id": source_task_id,
            "status": "blocked",
            "network_enabled": False,
            "api_call_executed": False,
            "invocations": invocations,
            "provider_count": len(invocations),
            "reason_count": len(reasons),
            "reasons": sorted(set(reasons)),
        }

    return {
        "run_id": actual_run_id,
        "flow_id": _normalize(council_flow.get("flow_id")) or DEFAULT_FLOW_ID,
        "source_task_id": source_task_id,
        "status": "valid",
        "network_enabled": False,
        "api_call_executed": False,
        "invocations": invocations,
        "provider_count": len(invocations),
        "reasons": [],
    }


def build_mock_research_council_run(
    flow: dict | Path | str,
    discussion_pack: dict | None = None,
    *,
    run_id: str | None = None,
    reviewer_decisions: dict[str, str] | None = None,
) -> Dict[str, Any]:
    council_flow = _read_payload(flow)
    validation = validate_research_council_flow(council_flow)
    if validation["status"] != "valid":
        return {
            "schema_version": RESEARCH_COUNCIL_SCHEMA_VERSION,
            "run_id": run_id or f"{DEFAULT_RUN_ID_PREFIX}_blocked",
            "flow_id": _normalize(council_flow.get("flow_id")) or DEFAULT_FLOW_ID,
            "status": "blocked",
            "reasons": validation["reasons"],
            "side_effects": dict(SIDE_EFFECTS_NONE),
            "network_enabled": False,
            "api_call_executed": False,
        }

    pack = discussion_pack or {}
    source_task_id = _normalize(pack.get("source_task_id")) or "unknown_source_task"
    flow_hash = _sha256_hex(council_flow)
    pack_hash = _sha256_hex(pack)
    actual_run_id = run_id or f"{DEFAULT_RUN_ID_PREFIX}_{flow_hash[:10]}_{pack_hash[:10]}"
    decisions = reviewer_decisions or {}

    stage_runs: list[dict[str, Any]] = []
    transcript: list[dict[str, Any]] = []
    failed_reviews: list[dict[str, Any]] = []

    for index, stage in enumerate(council_flow["stages"]):
        stage_id = _normalize(stage.get("id"))
        stage_type = _normalize(stage.get("type"))
        output_ref = _normalize(stage.get("output")) or f"{stage_id}.yaml"
        stage_run = {
            "stage_id": stage_id,
            "stage_index": index,
            "stage_type": stage_type,
            "status": "completed",
            "output_ref": output_ref,
        }
        message = {
            "stage_id": stage_id,
            "role": _normalize(stage.get("role")) or stage_type,
            "content": f"mock output for {stage_id}",
            "evidence_refs": pack.get("evidence_refs", []),
        }
        if stage_type == "review":
            decision = _normalize_lower(decisions.get(stage_id, "pass"))
            if decision not in ALLOWED_REVIEW_DECISIONS:
                decision = "fail"
            review_result = {
                "decision": decision,
                "reason": "mock review passed" if decision == "pass" else "mock review requested revision",
                "failed_criteria": [] if decision == "pass" else ["insufficient_grounding"],
                "next_round_instruction": "" if decision == "pass" else "Revise with explicit evidence references and narrower claim limits.",
            }
            stage_run["review_result"] = review_result
            message["review_result"] = review_result
            if decision == "fail":
                failed_reviews.append({"stage_id": stage_id, **review_result})
        transcript.append(message)
        stage_runs.append(stage_run)

    final_status = "needs_revision" if failed_reviews else "valid"
    final_decision = {
        "status": final_status,
        "source_task_id": source_task_id,
        "recommended_action": "revise" if failed_reviews else "stage_proposal_candidate",
        "next_step_candidates": []
        if failed_reviews
        else [
            {
                "kind": "bounded_probe",
                "reason": "Mock council found a safe bounded continuation candidate.",
                "claim_limit": "Advisory mock output only; no execution or promotion is implied.",
            }
        ],
        "failed_reviews": failed_reviews,
    }
    readiness_from_pack = _coerce_next_step_readiness(pack)
    if readiness_from_pack is not None:
        final_decision["next_step_readiness"] = readiness_from_pack

    return {
        "schema_version": RESEARCH_COUNCIL_SCHEMA_VERSION,
        "run_id": actual_run_id,
        "flow_id": _normalize(council_flow.get("flow_id")) or DEFAULT_FLOW_ID,
        "source_task_id": source_task_id,
        "status": final_status,
        "flow_hash": flow_hash,
        "discussion_pack_hash": pack_hash,
        "idempotency_key": _sha256_hex(
            {
                "run_id": actual_run_id,
                "flow_hash": flow_hash,
                "discussion_pack_hash": pack_hash,
                "reviewer_decisions": decisions,
            }
        ),
        "network_enabled": False,
        "api_call_executed": False,
        "side_effects": dict(SIDE_EFFECTS_NONE),
        "max_node_executions": council_flow["loop_policy"]["max_node_executions"],
        "stage_runs": stage_runs,
        "transcript": transcript,
        "final_decision": final_decision,
        "allowed_outputs": sorted(ALLOWED_OUTPUTS),
        "forbidden_actions": sorted(FORBIDDEN_ACTIONS),
    }


def execute_research_council_flow_with_mock_providers_dry_run(
    flow: dict | Path | str,
    discussion_pack: dict | None = None,
    *,
    mock_responses: dict[str, Any] | None = None,
    provider_routing: Dict[str, Any] | None = None,
    run_id: str | None = None,
) -> Dict[str, Any]:
    """Run the council flow using mocked provider payloads only."""
    council_flow = _read_payload(flow)
    flow_validation = validate_research_council_flow(council_flow)
    pack = discussion_pack or {}
    source_task_id = _normalize(pack.get("source_task_id")) or "unknown_source_task"
    flow_hash = _sha256_hex(council_flow)
    pack_hash = _sha256_hex(pack)
    max_rounds = int(council_flow["loop_policy"]["max_rounds"])
    max_node_executions = int(council_flow["loop_policy"]["max_node_executions"])
    generated_run_id = run_id or f"{DEFAULT_RUN_ID_PREFIX}_provider_{flow_hash[:10]}_{pack_hash[:10]}"

    def _build_failed_run(
        *,
        status: str,
        stop_reason: str,
        reasons: list[str] | None = None,
        stage_runs_payload: list[dict[str, Any]] | None = None,
        transcript_payload: list[dict[str, Any]] | None = None,
        provider_invocations: list[dict[str, Any]] | None = None,
        final_decision: dict[str, Any] | None = None,
        max_nodes: int | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": RESEARCH_COUNCIL_SCHEMA_VERSION,
            "run_id": generated_run_id,
            "flow_id": _normalize(council_flow.get("flow_id")) or DEFAULT_FLOW_ID,
            "source_task_id": source_task_id,
            "status": status,
            "stop_reason": stop_reason,
            "flow_hash": flow_hash,
            "discussion_pack_hash": pack_hash,
            "idempotency_key": _sha256_hex(
                {
                    "run_id": generated_run_id,
                    "flow_hash": flow_hash,
                    "discussion_pack_hash": pack_hash,
                    "status": status,
                    "stop_reason": stop_reason,
                }
            ),
            "network_enabled": False,
            "api_call_executed": False,
            "side_effects": dict(SIDE_EFFECTS_NONE),
            "max_node_executions": max_node_executions if max_nodes is None else max_nodes,
            "provider_invocations": provider_invocations or [],
            "stage_runs": stage_runs_payload or [],
            "transcript": transcript_payload or [],
            "final_decision": final_decision
            or {
                "status": status,
                "source_task_id": source_task_id,
                "recommended_action": "no_safe_continuation",
                "next_step_candidates": [],
                "failed_reviews": [],
                "stop_reason": stop_reason,
            },
            "allowed_outputs": sorted(ALLOWED_OUTPUTS),
            "forbidden_actions": sorted(FORBIDDEN_ACTIONS),
            "reasons": reasons or [],
        }

    if flow_validation["status"] != "valid":
        return _build_failed_run(
            status="blocked",
            stop_reason="flow_invalid",
            reasons=flow_validation["reasons"],
            final_decision={
                "status": "blocked",
                "source_task_id": source_task_id,
                "recommended_action": "no_safe_continuation",
                "next_step_candidates": [],
                "failed_reviews": [],
                "stop_reason": "flow_invalid",
            },
            max_nodes=0,
        )

    invocations_payload = build_research_council_provider_invocations_dry_run(
        council_flow,
        pack,
        provider_routing=provider_routing,
        run_id=generated_run_id,
    )
    if invocations_payload.get("status") != "valid":
        return _build_failed_run(
            status="blocked",
            stop_reason="provider_invocation_invalid",
            reasons=invocations_payload.get("reasons", []),
            provider_invocations=invocations_payload.get("invocations", []),
            final_decision={
                "status": "blocked",
                "source_task_id": source_task_id,
                "recommended_action": "no_safe_continuation",
                "next_step_candidates": [],
                "failed_reviews": [],
                "stop_reason": "provider_invocation_invalid",
            },
        )

    generated_run_id = _normalize(invocations_payload.get("run_id")) or generated_run_id
    invocation_by_stage = {
        _normalize_lower(invocation.get("stage_id")): invocation
        for invocation in invocations_payload.get("invocations", [])
        if isinstance(invocation, dict)
    }
    responses = mock_responses if isinstance(mock_responses, dict) else {}
    stages_by_id = _stage_by_id(council_flow)
    planner_stage = stages_by_id.get("chief_planner") or _first_stage_by_type(council_flow, "role")
    retrieval_stage = _first_stage_by_type(council_flow, "deterministic_retrieval")
    synthesis_stage = _first_stage_by_type(council_flow, "synthesis")
    final_gate_stage = _first_stage_by_type(council_flow, "deterministic_validator")
    review_stages = _review_stages(council_flow)

    def _value_for_round(raw: Any, round_index: int) -> Any:
        if isinstance(raw, (list, tuple)):
            if not raw:
                return None
            return raw[min(round_index, len(raw) - 1)]
        return raw

    def _response_for_stage(
        stage_id: str,
        *,
        stage_role: str | None = None,
        round_index: int = 0,
    ) -> Any:
        stage_key = _normalize_lower(stage_id)
        role_key = _normalize_lower(stage_role)
        if stage_key in responses:
            return _value_for_round(responses.get(stage_key), round_index)
        if role_key in responses:
            return _value_for_round(responses.get(role_key), round_index)
        return _value_for_round(responses.get("default"), round_index)

    def _is_final_gate_allowed(payload: Any) -> bool:
        if isinstance(payload, dict):
            status_hint = _normalize_lower(payload.get("status") or payload.get("decision"))
            return not status_hint or status_hint in {"pass", "approved", "valid", "ok", "pass_review"}
        if isinstance(payload, str):
            return _normalize_lower(payload) not in {"reject", "fail", "blocked"}
        return True

    def _append_mock_stage_run(
        stage: dict[str, Any],
        *,
        round_index: int,
        status: str = "completed",
        branch_taken: str | None = None,
        content: str | None = None,
        extra: Dict[str, Any] | None = None,
    ) -> None:
        nonlocal execution_index
        stage_id = _normalize(stage.get("id"))
        stage_type = _normalize(stage.get("type"))
        invocation = invocation_by_stage.get(_normalize_lower(stage_id), {})
        run_extra = {
            "provider": _normalize(invocation.get("provider")),
            "model": _normalize(invocation.get("model")),
            "provider_model": _normalize(invocation.get("provider_model")),
            "role": _normalize(stage.get("role")) or stage_type,
            "stage_type": stage_type,
        }
        if branch_taken is not None:
            run_extra["branch_taken"] = branch_taken
        if extra:
            run_extra.update(extra)

        _append_stage_run(
            stage_runs=stage_runs,
            transcript=transcript,
            stage=stage,
            execution_index=execution_index,
            round_index=round_index,
            status=status,
            content=content or f"mock output for {stage_id}",
            extra=run_extra,
        )
        execution_index += 1

    def _can_execute_next() -> bool:
        return execution_index < max_node_executions

    stage_runs: list[dict[str, Any]] = []
    transcript: list[dict[str, Any]] = []
    failed_reviews: list[dict[str, Any]] = []
    failed_reason: list[str] = []
    execution_index = 0
    stop_reason = ""
    blocked = False
    completed_successfully = False
    needs_revision = False

    if retrieval_stage and _can_execute_next():
        retrieval_response = _response_for_stage(_normalize(retrieval_stage.get("id")), round_index=0)
        retrieval_validation = validate_research_council_provider_response(
            retrieval_response,
            stage_id=_normalize(retrieval_stage.get("id")),
            stage_type=_normalize(retrieval_stage.get("type")),
        )
        if retrieval_validation["status"] != "valid":
            blocked = True
            stop_reason = "provider_response_invalid"
            failed_reason.extend(retrieval_validation.get("reasons", []))
        else:
            _append_mock_stage_run(
                retrieval_stage,
                round_index=0,
                branch_taken="next",
                content="mock retrieval output from provider",
                extra={"provider_response_validation": retrieval_validation},
            )

    for round_index in range(max_rounds):
        if blocked:
            break
        if not _can_execute_next():
            if not stop_reason:
                stop_reason = "max_node_executions_reached"
            break
        if not planner_stage:
            blocked = True
            stop_reason = "chief_planner_stage_missing"
            break

        planner_response = _response_for_stage(
            _normalize(planner_stage.get("id")),
            stage_role=_normalize(planner_stage.get("role")),
            round_index=round_index,
        )
        planner_validation = validate_research_council_provider_response(
            planner_response,
            stage_id=_normalize(planner_stage.get("id")),
            stage_type=_normalize(planner_stage.get("type")),
        )
        if planner_validation["status"] != "valid":
            blocked = True
            stop_reason = "provider_response_invalid"
            failed_reason.extend(planner_validation.get("reasons", []))
            break
        _append_mock_stage_run(
            planner_stage,
            round_index=round_index,
            branch_taken="next",
            content="mock chief planner output",
            extra={"provider_response_validation": planner_validation},
        )

        round_failures: list[dict[str, Any]] = []
        for review_stage in review_stages:
            if not _can_execute_next():
                if not stop_reason:
                    stop_reason = "max_node_executions_reached"
                break
            review_stage_id = _normalize(review_stage.get("id"))
            review_response = _response_for_stage(
                review_stage_id,
                stage_role=_normalize(review_stage.get("role")),
                round_index=round_index,
            )
            review_validation = validate_research_council_provider_response(
                review_response,
                stage_id=review_stage_id,
                stage_type=_normalize(review_stage.get("type")),
            )
            review_payload = review_validation.get("review_decision") or {}
            if review_validation["status"] != "valid" or not review_payload:
                needs_revision = True
                decision = "fail"
                failure = {
                    "stage_id": review_stage_id,
                    "decision": decision,
                    "reason": _normalize(review_validation.get("provider_response", "")),
                    "failed_criteria": ["provider_response_invalid"],
                    "next_round_instruction": "Revision required because provider response failed validation.",
                    "response_rejected": True,
                }
                failed_reviews.append(failure)
                round_failures.append(failure)
                failed_reason.extend(review_validation.get("reasons", []))
                _append_mock_stage_run(
                    review_stage,
                    round_index=round_index,
                    branch_taken="fail",
                    content="mock review invalid",
                    extra={"review_result": review_validation, "provider_response_validation": review_validation},
                )
                continue

            decision = _normalize_lower(review_payload.get("decision"))
            review_result = {
                "stage_id": review_stage_id,
                "decision": decision,
                "reason": _normalize(review_payload.get("reason")),
                "failed_criteria": review_payload.get("failed_criteria", []),
                "next_round_instruction": _normalize(review_payload.get("next_round_instruction")),
            }
            _append_mock_stage_run(
                review_stage,
                round_index=round_index,
                branch_taken=decision,
                content=f"mock review {decision} for {review_stage_id}",
                extra={"review_result": review_result, "provider_response_validation": review_validation},
            )
            if decision == "fail":
                needs_revision = True
                round_failures.append(review_result)
                failed_reviews.append(review_result)

        if round_failures:
            if round_index + 1 >= max_rounds:
                stop_reason = "max_rounds_reached"
                break
            continue

        if synthesis_stage and _can_execute_next():
            synthesis_response = _response_for_stage(
                _normalize(synthesis_stage.get("id")),
                stage_role=_normalize(synthesis_stage.get("role")),
                round_index=round_index,
            )
            synthesis_validation = validate_research_council_provider_response(
                synthesis_response,
                stage_id=_normalize(synthesis_stage.get("id")),
                stage_type=_normalize(synthesis_stage.get("type")),
            )
            if synthesis_validation["status"] != "valid":
                blocked = True
                stop_reason = "provider_response_invalid"
                failed_reason.extend(synthesis_validation.get("reasons", []))
                break
            _append_mock_stage_run(
                synthesis_stage,
                round_index=round_index,
                branch_taken="next",
                content="mock synthesis output",
                extra={"provider_response_validation": synthesis_validation},
            )

        if final_gate_stage and _can_execute_next():
            final_gate_response = _response_for_stage(
                _normalize(final_gate_stage.get("id")),
                stage_role=_normalize(final_gate_stage.get("role")),
                round_index=round_index,
            )
            final_gate_validation = validate_research_council_provider_response(
                final_gate_response,
                stage_id=_normalize(final_gate_stage.get("id")),
                stage_type=_normalize(final_gate_stage.get("type")),
            )
            if final_gate_validation["status"] != "valid":
                blocked = True
                stop_reason = "provider_response_invalid"
                failed_reason.extend(final_gate_validation.get("reasons", []))
                break

            if not _is_final_gate_allowed(final_gate_validation.get("provider_response")):
                blocked = True
                stop_reason = "final_gate_rejected"
                failed_reason.append("final_gate_rejected")
                break

            _append_mock_stage_run(
                final_gate_stage,
                round_index=round_index,
                branch_taken="pass",
                content="mock final gate output",
                extra={"provider_response_validation": final_gate_validation},
            )
            completed_successfully = True
            break

        if not _can_execute_next():
            stop_reason = "max_node_executions_reached"
            break

    if not stop_reason and not completed_successfully and not blocked and not needs_revision:
        stop_reason = "no_safe_continuation"
    if not stop_reason and blocked:
        stop_reason = "provider_response_blocked"

    if blocked:
        final_status = "blocked"
        recommended_action = "no_safe_continuation"
    elif completed_successfully:
        final_status = "valid"
        recommended_action = "stage_proposal_candidate"
    else:
        final_status = "needs_revision"
        recommended_action = "revise" if needs_revision else "no_safe_continuation"

    next_step_candidates: list[dict[str, Any]] = []
    if completed_successfully:
        next_step_candidates = [
            {
                "candidate_id": f"{source_task_id}_research_council_bounded_probe_mock",
                "kind": "bounded_probe",
                "source_council_id": generated_run_id,
                "source_task_id": source_task_id,
                "worker": _normalize(pack.get("worker")) or "small_task_worker",
                "line": _normalize(pack.get("line")) or _normalize(pack.get("line_filter")) or "unknown",
                "workplace_profile": _normalize(pack.get("workplace_profile")) or "unknown",
                "reason": "Mock provider council output passed all review stages and final gate.",
                "risk_flags": [],
                "claim_limitations": ["Mock provider responses are advisory-only and must not be used for execution."],
                "expected_outputs": ["bounded_probe_report.md", "action_requests.json"],
                "status": "proposal_candidate_created",
                "can_stage": True,
                "requires_human_approval": False,
            }
        ]

    return {
        "schema_version": RESEARCH_COUNCIL_SCHEMA_VERSION,
        "run_id": generated_run_id,
        "flow_id": _normalize(council_flow.get("flow_id")) or DEFAULT_FLOW_ID,
        "source_task_id": source_task_id,
        "status": final_status,
        "stop_reason": stop_reason,
        "flow_hash": flow_hash,
        "discussion_pack_hash": pack_hash,
        "idempotency_key": _sha256_hex(
            {
                "run_id": generated_run_id,
                "flow_hash": flow_hash,
                "discussion_pack_hash": pack_hash,
                "status": final_status,
                "mode": "mock_provider_flow_runner",
                "stop_reason": stop_reason,
            }
        ),
        "provider_invocations": invocations_payload.get("invocations", []),
        "network_enabled": False,
        "api_call_executed": False,
        "side_effects": dict(SIDE_EFFECTS_NONE),
        "max_node_executions": max_node_executions,
        "stage_runs": stage_runs,
        "transcript": transcript,
        "final_decision": {
            "status": final_status,
            "source_task_id": source_task_id,
            "recommended_action": recommended_action,
            "next_step_candidates": next_step_candidates,
            "failed_reviews": failed_reviews,
            "stop_reason": stop_reason,
        },
        "allowed_outputs": sorted(ALLOWED_OUTPUTS),
        "forbidden_actions": sorted(FORBIDDEN_ACTIONS),
        "reasons": sorted(set(failed_reason)),
    }

def execute_research_council_flow_dry_run(
    flow: dict | Path | str,
    discussion_pack: dict | None = None,
    *,
    run_id: str | None = None,
    reviewer_decisions: dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Run the fixed council flow locally with deterministic outputs.

    This does not call models, tools, subprocesses, the scheduler, or the
    network. It hardens orchestration semantics before any real provider is
    allowed: review failures carry next-round instructions back to the chief
    planner, and loop limits fail closed.
    """
    council_flow = _read_payload(flow)
    validation = validate_research_council_flow(council_flow)
    if validation["status"] != "valid":
        return {
            "schema_version": RESEARCH_COUNCIL_SCHEMA_VERSION,
            "run_id": run_id or f"{DEFAULT_RUN_ID_PREFIX}_blocked",
            "flow_id": _normalize(council_flow.get("flow_id")) or DEFAULT_FLOW_ID,
            "status": "blocked",
            "reasons": validation["reasons"],
            "side_effects": dict(SIDE_EFFECTS_NONE),
            "network_enabled": False,
            "api_call_executed": False,
        }

    pack = discussion_pack or {}
    decisions = reviewer_decisions or {}
    source_task_id = _normalize(pack.get("source_task_id")) or "unknown_source_task"
    flow_hash = _sha256_hex(council_flow)
    pack_hash = _sha256_hex(pack)
    actual_run_id = run_id or f"{DEFAULT_RUN_ID_PREFIX}_exec_{flow_hash[:10]}_{pack_hash[:10]}"
    max_rounds = int(council_flow["loop_policy"]["max_rounds"])
    max_node_executions = int(council_flow["loop_policy"]["max_node_executions"])

    stages_by_id = _stage_by_id(council_flow)
    retrieval_stage = _first_stage_by_type(council_flow, "deterministic_retrieval")
    planner_stage = stages_by_id.get("chief_planner") or _first_stage_by_type(council_flow, "role")
    synthesis_stage = _first_stage_by_type(council_flow, "synthesis")
    final_gate_stage = _first_stage_by_type(council_flow, "deterministic_validator")
    review_stages = _review_stages(council_flow)

    stage_runs: list[dict[str, Any]] = []
    transcript: list[dict[str, Any]] = []
    failed_reviews: list[dict[str, Any]] = []
    revision_instructions: list[str] = []
    execution_index = 0
    stop_reason = ""

    def can_execute_next() -> bool:
        return execution_index < max_node_executions

    if retrieval_stage and can_execute_next():
        _append_stage_run(
            stage_runs=stage_runs,
            transcript=transcript,
            stage=retrieval_stage,
            execution_index=execution_index,
            round_index=0,
            content="deterministic case-memory excerpt built from discussion pack",
            extra={"branch_taken": "next"},
        )
        execution_index += 1

    completed_successfully = False
    for round_index in range(max_rounds):
        if not can_execute_next():
            stop_reason = "max_node_executions_reached"
            break
        if not planner_stage:
            stop_reason = "chief_planner_stage_missing"
            break

        _append_stage_run(
            stage_runs=stage_runs,
            transcript=transcript,
            stage=planner_stage,
            execution_index=execution_index,
            round_index=round_index,
            content=(
                "deterministic chief plan"
                if not revision_instructions
                else "deterministic chief plan revised from reviewer instruction"
            ),
            extra={"branch_taken": "next", "revision_instructions": list(revision_instructions)},
        )
        execution_index += 1

        round_failures: list[dict[str, Any]] = []
        revision_instructions = []
        for review_stage in review_stages:
            if not can_execute_next():
                stop_reason = "max_node_executions_reached"
                break
            stage_id = _normalize(review_stage.get("id"))
            decision = _decision_for_round(decisions, stage_id, round_index)
            review_result = {
                "decision": decision,
                "reason": "deterministic review passed"
                if decision == "pass"
                else "deterministic review requested revision",
                "failed_criteria": [] if decision == "pass" else ["insufficient_grounding"],
                "next_round_instruction": ""
                if decision == "pass"
                else f"Round {round_index + 1}: revise after {stage_id} failure with explicit evidence and narrower claim limits.",
            }
            _append_stage_run(
                stage_runs=stage_runs,
                transcript=transcript,
                stage=review_stage,
                execution_index=execution_index,
                round_index=round_index,
                content=f"deterministic review {decision} for {stage_id}",
                extra={
                    "branch_taken": decision,
                    "review_result": review_result,
                    "next_stage": "chief_planner" if decision == "fail" else "chief_synthesizer",
                },
            )
            execution_index += 1
            if decision == "fail":
                failure = {"stage_id": stage_id, **review_result}
                round_failures.append(failure)
                failed_reviews.append(failure)
                revision_instructions.append(review_result["next_round_instruction"])

        if stop_reason:
            break
        if round_failures:
            if round_index + 1 >= max_rounds:
                stop_reason = "max_rounds_reached"
                break
            continue

        if synthesis_stage and can_execute_next():
            _append_stage_run(
                stage_runs=stage_runs,
                transcript=transcript,
                stage=synthesis_stage,
                execution_index=execution_index,
                round_index=round_index,
                content="deterministic synthesis accepted review-passed chief plan",
                extra={"branch_taken": "next"},
            )
            execution_index += 1
        if final_gate_stage and can_execute_next():
            _append_stage_run(
                stage_runs=stage_runs,
                transcript=transcript,
                stage=final_gate_stage,
                execution_index=execution_index,
                round_index=round_index,
                content="deterministic final gate passed advisory-only candidate",
                extra={"branch_taken": "pass"},
            )
            execution_index += 1
        completed_successfully = True
        break

    if not stop_reason and not completed_successfully:
        stop_reason = "no_safe_continuation"

    final_status = "valid" if completed_successfully else "needs_revision"
    recommended_action = "stage_proposal_candidate" if completed_successfully else "no_safe_continuation"
    next_step_candidates = []
    if completed_successfully:
        next_step_candidates = [
            {
                "candidate_id": f"{_normalize(source_task_id) or 'unknown_task'}_research_council_bounded_probe",
                "kind": "bounded_probe",
                "source_council_id": actual_run_id,
                "source_task_id": source_task_id,
                "worker": _normalize(pack.get("worker")) or "small_task_worker",
                "line": _normalize(pack.get("line")) or _normalize(pack.get("line_filter")) or "unknown",
                "workplace_profile": _normalize(pack.get("workplace_profile")) or "unknown",
                "reason": "Research Council deterministic runner found a bounded, advisory-only next step.",
                "risk_flags": [],
                "claim_limitations": ["Research Council output is advisory and must pass proposal staging before use."],
                "expected_outputs": ["bounded_probe_report.md", "action_requests.json"],
                "status": "proposal_candidate_created",
                "can_stage": True,
                "requires_human_approval": False,
            }
        ]

    final_decision = {
        "status": final_status,
        "source_task_id": source_task_id,
        "recommended_action": recommended_action,
        "next_step_candidates": next_step_candidates,
        "failed_reviews": failed_reviews,
        "stop_reason": stop_reason,
    }
    readiness_from_pack = _coerce_next_step_readiness(pack)
    if readiness_from_pack is not None:
        final_decision["next_step_readiness"] = readiness_from_pack

    return {
        "schema_version": RESEARCH_COUNCIL_SCHEMA_VERSION,
        "run_id": actual_run_id,
        "flow_id": _normalize(council_flow.get("flow_id")) or DEFAULT_FLOW_ID,
        "source_task_id": source_task_id,
        "status": final_status,
        "stop_reason": stop_reason,
        "flow_hash": flow_hash,
        "discussion_pack_hash": pack_hash,
        "idempotency_key": _sha256_hex(
            {
                "run_id": actual_run_id,
                "flow_hash": flow_hash,
                "discussion_pack_hash": pack_hash,
                "reviewer_decisions": decisions,
                "mode": "deterministic_flow_runner",
            }
        ),
        "network_enabled": False,
        "api_call_executed": False,
        "side_effects": dict(SIDE_EFFECTS_NONE),
        "max_node_executions": max_node_executions,
        "stage_runs": stage_runs,
        "transcript": transcript,
        "final_decision": final_decision,
        "allowed_outputs": sorted(ALLOWED_OUTPUTS),
        "forbidden_actions": sorted(FORBIDDEN_ACTIONS),
    }


def validate_research_council_run(run: dict | Path | str) -> Dict[str, Any]:
    payload = _read_payload(run)
    reasons: list[str] = []
    if payload.get("schema_version") != RESEARCH_COUNCIL_SCHEMA_VERSION:
        reasons.append("schema_version_mismatch")
    if payload.get("network_enabled") is not False:
        reasons.append("network_must_be_disabled")
    if payload.get("api_call_executed") is not False:
        reasons.append("api_call_must_not_execute")
    side_effects = payload.get("side_effects")
    if not isinstance(side_effects, dict):
        reasons.append("side_effects_required")
    else:
        for key, expected in SIDE_EFFECTS_NONE.items():
            if side_effects.get(key) is not expected:
                reasons.append(f"side_effect_not_allowed:{key}")
    if not isinstance(payload.get("stage_runs"), list):
        reasons.append("stage_runs_required")
    if not isinstance(payload.get("transcript"), list):
        reasons.append("transcript_required")
    final_decision = payload.get("final_decision")
    if not isinstance(final_decision, dict):
        reasons.append("final_decision_required")
    else:
        forbidden = _forbidden_findings(final_decision)
        if forbidden:
            reasons.extend(sorted(set(forbidden)))
    max_node_executions = payload.get("max_node_executions")
    stage_runs = payload.get("stage_runs") if isinstance(payload.get("stage_runs"), list) else []
    if isinstance(max_node_executions, int) and len(stage_runs) > max_node_executions:
        reasons.append("max_node_executions_exceeded")
    return {
        "status": "valid" if not reasons else "rejected",
        "reasons": reasons,
        "stage_run_count": len(stage_runs),
        "advisory_only": True,
    }


def summarize_research_council_run(run: dict | Path | str) -> Dict[str, Any]:
    payload = _read_payload(run)
    validation = validate_research_council_run(payload)
    final_decision = payload.get("final_decision") if isinstance(payload.get("final_decision"), dict) else {}
    return {
        "schema_version": RESEARCH_COUNCIL_SCHEMA_VERSION,
        "run_id": payload.get("run_id"),
        "flow_id": payload.get("flow_id"),
        "source_task_id": payload.get("source_task_id"),
        "status": payload.get("status"),
        "validation_status": validation["status"],
        "stage_run_count": validation["stage_run_count"],
        "transcript_message_count": len(payload.get("transcript", [])) if isinstance(payload.get("transcript"), list) else 0,
        "next_step_candidate_count": len(final_decision.get("next_step_candidates", []))
        if isinstance(final_decision.get("next_step_candidates"), list)
        else 0,
        "advisory_only": True,
        "side_effects": dict(SIDE_EFFECTS_NONE),
    }


def stage_research_council_decision_candidates_dry_run(
    run: dict | Path | str,
    *,
    actor: str = "research_council",
    proposal_run_id: str | None = None,
) -> Dict[str, Any]:
    payload = _read_payload(run)
    validation = validate_research_council_run(payload)
    if validation["status"] != "valid":
        return {
            "status": "rejected",
            "action": "invalid_research_council_run",
            "validation_status": validation["status"],
            "reasons": validation["reasons"],
            "candidate_count": 0,
            "staged_count": 0,
            "blocked_count": 0,
            "skipped_count": 0,
            "report_paths": [],
            "staging_results": [],
            "proposal_run_id": _normalize(proposal_run_id) or _normalize(payload.get("run_id")) or None,
        }

    final_decision = payload.get("final_decision") if isinstance(payload.get("final_decision"), dict) else {}
    recommended_action = _normalize_lower(final_decision.get("recommended_action"))
    next_step_candidates = final_decision.get("next_step_candidates")
    raw_candidates = next_step_candidates if isinstance(next_step_candidates, list) else []
    next_step_readiness = final_decision.get("next_step_readiness")
    resolved_proposal_run_id = _normalize(proposal_run_id) or _normalize(payload.get("run_id")) or "research_council_candidate_batch"

    if recommended_action != "stage_proposal_candidate":
        return {
            "status": "skipped",
            "action": f"recommendation_{recommended_action or 'missing'}",
            "validation_status": "valid",
            "reasons": [f"recommended_action_not_stageable:{recommended_action or 'missing'}"],
            "candidate_count": len(raw_candidates),
            "staged_count": 0,
            "blocked_count": 0,
            "skipped_count": len(raw_candidates),
            "report_paths": [],
            "staging_results": [],
            "proposal_run_id": resolved_proposal_run_id,
        }

    candidates = raw_candidates[:3]
    staging_results: list[dict[str, Any]] = []
    report_paths: list[str] = []
    blocked_count = 0
    staged_count = 0
    skipped_count = 0

    for candidate in candidates:
        if not isinstance(candidate, dict):
            skipped_count += 1
            continue

        staged_candidate = dict(candidate)
        if isinstance(next_step_readiness, dict):
            staged_candidate["next_step_readiness"] = dict(next_step_readiness)
        result = proposal_staging.stage_proposal_candidate_dry_run(
            staged_candidate,
            actor=_normalize(actor) or "research_council",
            proposal_run_id=resolved_proposal_run_id,
        )
        staging_results.append(result)
        if result.get("staging_report_path"):
            report_paths.append(str(result["staging_report_path"]))
        result_reasons = [str(item) for item in result.get("reasons", [])]
        is_blocked = result.get("status") == "blocked" or any(
            reason.startswith("forbidden_side_effect_true:")
            or reason.startswith("forbidden_field:")
            or reason.startswith("next_step_readiness_blocker:")
            or reason == "next_step_not_stageable_by_progression"
            or reason == "static_gate_failed"
            for reason in result_reasons
        )
        if is_blocked:
            blocked_count += 1
        elif result.get("staged"):
            staged_count += 1
        else:
            skipped_count += 1

    if blocked_count and blocked_count == len(candidates):
        status = "blocked"
    elif not candidates:
        status = "skipped"
    elif blocked_count:
        status = "partially_blocked"
    else:
        status = "accepted"

    return {
        "status": status,
        "action": "stage_research_council_decision_candidates",
        "validation_status": "valid",
        "reasons": [],
        "candidate_count": len(candidates),
        "staged_count": staged_count,
        "blocked_count": blocked_count,
        "skipped_count": skipped_count,
        "report_paths": report_paths,
        "staging_results": staging_results,
        "proposal_run_id": resolved_proposal_run_id,
    }


def write_research_council_run_artifacts_dry_run(run: dict | Path | str, flow: dict | Path | str) -> Dict[str, Any]:
    run_payload = _read_payload(run)
    flow_payload = _read_payload(flow)
    validation = validate_research_council_run(run_payload)
    if validation["status"] != "valid":
        return {
            "status": "rejected",
            "created": False,
            "replayed": False,
            "reasons": validation["reasons"],
        }

    run_id = _normalize(run_payload.get("run_id")) or "unknown_research_council_run"
    flow_path = continuation_research_council_flow_path(run_id)
    run_path = continuation_research_council_run_path(run_id)
    summary_path = continuation_research_council_summary_path(run_id)
    summary_payload = summarize_research_council_run(run_payload)

    paths_and_payloads = (
        (flow_path, flow_payload),
        (run_path, run_payload),
        (summary_path, summary_payload),
    )
    for path, payload in paths_and_payloads:
        existing = _read_existing_json(path)
        if existing is not None and not _is_same_payload(existing, payload):
            return {
                "status": "blocked",
                "created": False,
                "replayed": False,
                "reasons": [f"idempotency_collision:{path.name}"],
                "flow_path": str(flow_path),
                "run_path": str(run_path),
                "summary_path": str(summary_path),
            }

    created = False
    for path, payload in paths_and_payloads:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            created = True

    return {
        "status": "accepted",
        "created": created,
        "replayed": not created,
        "flow_path": str(flow_path),
        "run_path": str(run_path),
        "summary_path": str(summary_path),
    }
