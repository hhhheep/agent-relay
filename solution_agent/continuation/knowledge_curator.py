from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .paths import continuation_knowledge_update_proposal_dir

SCHEMA_VERSION = 1

ALLOWED_TARGETS = {
    "chronology",
    "decision_ledger",
    "superseded_map",
    "claim_limits",
    "artifact_lineage",
    "current_state",
    "open_questions",
    "next_safe_actions",
}

FALSE_SIDE_EFFECTS = {
    "canonical_kb_write": False,
    "active_inbox_write": False,
    "scheduler_dispatch": False,
    "registry_mutation": False,
    "remote_execution": False,
    "source_edit": False,
    "auto_promote": False,
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
    safe = _normalize(value).replace("..", "_").replace("/", "_").replace("\\", "_")
    return safe or "unknown"


def _source_evidence(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = payload.get("source_evidence") or payload.get("evidence") or []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    output: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        evidence_id = _normalize(item.get("evidence_id") or item.get("id") or f"evidence_{index + 1}")
        output.append(
            {
                "evidence_id": evidence_id,
                "evidence_type": _normalize(item.get("evidence_type") or item.get("type") or "unknown"),
                "summary": _normalize(item.get("summary") or item.get("text") or item.get("description")),
                "authority": _normalize(item.get("authority") or item.get("authority_level") or "unknown"),
            }
        )
    return output


def _evidence_ids(evidence: Iterable[Dict[str, Any]]) -> List[str]:
    return [item["evidence_id"] for item in evidence if _normalize(item.get("evidence_id"))]


def _patch(target: str, entries: List[Dict[str, Any]], evidence: List[Dict[str, Any]], claim_level: str) -> Dict[str, Any]:
    return {
        "target": target,
        "entries": entries,
        "source_evidence_ids": _evidence_ids(evidence),
        "claim_level": claim_level,
    }


def _build_patches(payload: Dict[str, Any], evidence: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    observations = payload.get("observations") if isinstance(payload.get("observations"), dict) else {}
    patches: Dict[str, Dict[str, Any]] = {}

    superseded = observations.get("superseded_instruction")
    if isinstance(superseded, dict):
        old_instruction = _normalize(superseded.get("old_instruction"))
        superseded_by = superseded.get("superseded_by") or _evidence_ids(evidence)
        if isinstance(superseded_by, str):
            superseded_by = [superseded_by]
        patches["superseded_map"] = _patch(
            "superseded_map",
            [
                {
                    "old_instruction": old_instruction,
                    "source": _normalize(superseded.get("source")),
                    "superseded_by": list(superseded_by),
                    "current_handling": _normalize(superseded.get("current_handling") or "historical_context_only"),
                    "invalid_use": superseded.get("invalid_use") or ["current_next_step", "scheduling_instruction"],
                }
            ],
            evidence,
            "current_evidence",
        )

    smoke = observations.get("smoke_result")
    if isinstance(smoke, dict):
        subject = _normalize(smoke.get("subject") or "bounded_smoke_result")
        patches["claim_limits"] = _patch(
            "claim_limits",
            [
                {
                    "subject": subject,
                    "can_claim": smoke.get("can_claim") or ["interface_or_pathway_works"],
                    "cannot_claim": smoke.get("cannot_claim")
                    or ["formal_quality", "submission_readiness", "final_method_success"],
                    "status": "smoke_only",
                }
            ],
            evidence,
            "interface_or_pathway_only",
        )
        patches["decision_ledger"] = _patch(
            "decision_ledger",
            [
                {
                    "decision_id": _safe_id(f"{subject}_smoke_only"),
                    "status": "active",
                    "decision": f"{subject} is smoke/interface evidence only.",
                    "claim_limit_ref": subject,
                }
            ],
            evidence,
            "interface_or_pathway_only",
        )

    failed_branch = observations.get("failed_branch")
    if isinstance(failed_branch, dict):
        branch = _normalize(failed_branch.get("branch") or failed_branch.get("name") or "unknown_branch")
        patches["decision_ledger"] = _patch(
            "decision_ledger",
            [
                {
                    "decision_id": _safe_id(f"{branch}_rejected_path"),
                    "status": "failed_or_rejected_path",
                    "decision": _normalize(failed_branch.get("reason") or f"{branch} should not be promoted."),
                    "restart_condition": _normalize(
                        failed_branch.get("restart_condition") or "requires new evidence, gate, and negative handling"
                    ),
                }
            ],
            evidence,
            "bounded_observation",
        )

    mainline = observations.get("current_mainline")
    if isinstance(mainline, dict):
        mainline_text = _normalize(mainline.get("mainline") or mainline.get("text"))
        patches["current_state"] = _patch(
            "current_state",
            [
                {
                    "mainline": mainline_text,
                    "status": _normalize(mainline.get("status") or "active_direction"),
                    "known_bottleneck": _normalize(mainline.get("known_bottleneck")),
                }
            ],
            evidence,
            "current_evidence",
        )
        next_action = _normalize(mainline.get("next_safe_action"))
        if next_action:
            patches["next_safe_actions"] = _patch(
                "next_safe_actions",
                [
                    {
                        "action": next_action,
                        "why_safe": _normalize(mainline.get("why_safe") or "bounded local evidence update"),
                        "forbidden_escalations": mainline.get("forbidden_escalations")
                        or ["full_training", "submission", "promotion_without_gate"],
                    }
                ],
                evidence,
                "current_evidence",
            )

    open_questions = observations.get("open_questions")
    if isinstance(open_questions, list) and open_questions:
        patches["open_questions"] = _patch(
            "open_questions",
            [{"question": _normalize(item)} for item in open_questions if _normalize(item)],
            evidence,
            "open_question",
        )

    return patches


def validate_knowledge_update_proposal(proposal: Dict[str, Any]) -> Dict[str, Any]:
    evidence = proposal.get("source_evidence") if isinstance(proposal.get("source_evidence"), list) else []
    patches = proposal.get("patches") if isinstance(proposal.get("patches"), dict) else {}
    reasons: List[str] = []
    checks: List[Dict[str, Any]] = []

    evidence_ok = bool(evidence) and all(
        _normalize(item.get("evidence_id")) and _normalize(item.get("summary")) and _normalize(item.get("authority"))
        for item in evidence
        if isinstance(item, dict)
    )
    checks.append({"id": "source_evidence_present", "status": "passed" if evidence_ok else "failed"})
    if not evidence_ok:
        reasons.append("missing_source_evidence")

    for target, patch in patches.items():
        target_ok = target in ALLOWED_TARGETS and patch.get("target") == target
        entries_ok = isinstance(patch.get("entries"), list) and bool(patch.get("entries"))
        claim_level = _normalize(patch.get("claim_level"))
        checks.append({"id": f"target_{target}", "status": "passed" if target_ok else "failed"})
        checks.append({"id": f"entries_{target}", "status": "passed" if entries_ok else "failed"})
        if not target_ok:
            reasons.append("target_section_not_allowed")
        if not entries_ok:
            reasons.append("empty_patch_entries")
        if target == "claim_limits":
            for entry in patch.get("entries", []):
                cannot_claim = entry.get("cannot_claim") if isinstance(entry, dict) else []
                if "final_method_success" not in cannot_claim:
                    reasons.append("smoke_or_proxy_missing_claim_limit")
        if claim_level in {"formal_quality", "final_method_success"}:
            reasons.append("unsupported_formal_claim")

    side_effects = proposal.get("side_effects") if isinstance(proposal.get("side_effects"), dict) else {}
    side_effects_ok = all(side_effects.get(key) is value for key, value in FALSE_SIDE_EFFECTS.items())
    checks.append({"id": "no_canonical_or_runtime_side_effects", "status": "passed" if side_effects_ok else "failed"})
    if not side_effects_ok:
        reasons.append("forbidden_side_effect")

    status = "ready" if not reasons and patches else "blocked"
    gate_report = {
        "schema_version": SCHEMA_VERSION,
        "status": "passed" if status == "ready" else "failed",
        "checks": checks,
        "reasons": sorted(set(reasons)),
    }
    return {**proposal, "status": status, "gate_report": gate_report, "reasons": sorted(set(reasons))}


def build_knowledge_update_proposal_dry_run(payload: Dict[str, Any]) -> Dict[str, Any]:
    source_kind = _normalize(payload.get("source_kind") or "manual_curator_input")
    source_id = _normalize(payload.get("source_id") or payload.get("session_id") or payload.get("task_id") or "unknown_source")
    evidence = _source_evidence(payload)
    patches = _build_patches(payload, evidence)
    seed = {
        "source_kind": source_kind,
        "source_id": source_id,
        "source_evidence": evidence,
        "patches": patches,
    }
    proposal_id = _normalize(payload.get("proposal_id")) or f"kbupd_{_sha256_hex(seed)[:16]}"
    proposal = {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "source_kind": source_kind,
        "source_id": source_id,
        "source_evidence": evidence,
        "patches": patches,
        "side_effects": dict(FALSE_SIDE_EFFECTS),
        "idempotency_key": f"kbupd_{_sha256_hex({'proposal_id': proposal_id, 'seed': seed})[:24]}",
    }
    return validate_knowledge_update_proposal(proposal)


def _read_existing_json(path: Path) -> Dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _same_payload(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    return _canonical_json(left) == _canonical_json(right)


def _write_json_yaml(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_knowledge_update_proposal_dry_run(proposal: Dict[str, Any]) -> Dict[str, Any]:
    validated = validate_knowledge_update_proposal(dict(proposal))
    proposal_id = _normalize(validated.get("proposal_id"))
    target_dir = continuation_knowledge_update_proposal_dir(proposal_id)
    manifest_path = target_dir / "kb_update_manifest.yaml"

    if validated["status"] != "ready":
        return {**validated, "created": False, "replayed": False, "proposal_dir": str(target_dir)}

    existing = _read_existing_json(manifest_path)
    if existing is not None:
        if _same_payload(existing, validated):
            return {**validated, "created": False, "replayed": True, "proposal_dir": str(target_dir)}
        return {
            **validated,
            "status": "blocked",
            "created": False,
            "replayed": False,
            "proposal_dir": str(target_dir),
            "reasons": sorted(set(list(validated.get("reasons", [])) + ["idempotency_collision"])),
        }

    _write_json_yaml(manifest_path, validated)
    _write_json_yaml(target_dir / "gate_report.yaml", validated["gate_report"])
    _write_json_yaml(target_dir / "source_evidence.yaml", {"source_evidence": validated["source_evidence"]})
    rationale = [
        "# Knowledge Update Proposal",
        "",
        f"Proposal: {proposal_id}",
        f"Status: {validated['status']}",
        "",
        "This is a reviewable KB patch proposal. It does not modify canonical knowledge.",
    ]
    (target_dir / "rationale.md").write_text("\n".join(rationale) + "\n", encoding="utf-8")

    patch_names = {
        "chronology": "chronology_patch.yaml",
        "decision_ledger": "decision_ledger_patch.yaml",
        "superseded_map": "superseded_map_patch.yaml",
        "claim_limits": "claim_limits_patch.yaml",
        "artifact_lineage": "artifact_lineage_patch.yaml",
        "current_state": "current_state_patch.yaml",
        "open_questions": "open_questions_patch.yaml",
        "next_safe_actions": "next_safe_actions_patch.yaml",
    }
    for target, patch in validated["patches"].items():
        _write_json_yaml(target_dir / patch_names[target], patch)

    return {**validated, "created": True, "replayed": False, "proposal_dir": str(target_dir)}


__all__ = [
    "ALLOWED_TARGETS",
    "FALSE_SIDE_EFFECTS",
    "build_knowledge_update_proposal_dry_run",
    "validate_knowledge_update_proposal",
    "write_knowledge_update_proposal_dry_run",
]
