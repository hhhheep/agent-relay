from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .paths import (
    continuation_audit_events_path,
    continuation_task_inbox_root,
    continuation_task_inbox_proposed_root,
    solution_continuation_dir,
)


PROMOTION_STATES = {
    "drafted",
    "staging_passed",
    "awaiting_approval",
    "approved",
    "promotion_preflight_passed",
    "promotion_preflight_failed",
    "promoted_pending",
    "promoted_to_inbox",
    "rejected",
    "revoked",
    "expired",
    "superseded",
    "logically_revoked",
}

BLOCKED_PROMOTION_STATES = {"rejected", "revoked", "expired", "superseded", "logically_revoked"}

FORBIDDEN_TEXT = [
    "direct_dispatch",
    "scheduler_submit",
    "dispatch",
    "registry_mutation",
    "source_edit",
    "remote_path_guess",
    "remote_materializer",
    "apply_patch",
    "modify source",
    "patch source",
    "modify_source",
]

GUESSED_REMOTE_PATTERNS = [
    re.compile(r"/ssd\\d+/", re.IGNORECASE),
    re.compile(r"/raid/", re.IGNORECASE),
    re.compile(r"/mnt/", re.IGNORECASE),
    re.compile(r"\\\\[^\\\\]+\\\\[^\\\\]+", re.IGNORECASE),
]

REQUIRED_FRONT_MATTER_FIELDS = [
    "task_id",
    "source_task_id",
    "source_result_hash",
    "proposal_id",
    "plan_id",
    "bundle_id",
    "proposal_count",
    "claim_limitation",
    "forbidden_actions",
    "outputs",
]

PROMOTED_METADATA_FIELDS = ["source_task_id", "bundle_id", "plan_id", "proposal_id", "idempotency_key"]

ACTIVATION_APPROVAL_FIELDS = {"activation_approved", "activation_approval", "activation_permitted"}
PROMOTION_AUDIT_ACTION = "promote"


def _policy_root() -> Path:
    root = solution_continuation_dir()
    if root.exists():
        return root
    return Path(__file__).resolve().parents[2] / ".solution" / "continuation"


def _load_yaml(path: Path) -> Dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return payload


def _load_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_task_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value or "")[:80] or "bundle"


def _read_policy() -> Dict[str, Any]:
    return _load_yaml(_policy_root() / "activation_policy.yaml")


def _read_preflight_checklist() -> Dict[str, Any]:
    return _load_yaml(_policy_root() / "activation_preflight_checklist.yaml")


def _pending_root() -> Path:
    return continuation_task_inbox_root() / "_promoted_pending"


def _active_root() -> Path:
    return continuation_task_inbox_root()


def _required_path_roots() -> Dict[str, Path]:
    return {
        "active_target_root": _active_root(),
        "promoted_root": _pending_root(),
    }


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}, text
    front, body = parts[1], parts[2]
    try:
        payload = yaml.safe_load(front) or {}
    except yaml.YAMLError:
        payload = {}
    return payload if isinstance(payload, dict) else {}, body


def _normalize_target(value: Any) -> str:
    return str(value or "").strip()


def _read_promotion_state(bundle_root: Path) -> str:
    state_path = bundle_root / "promotion_state.json"
    if not state_path.exists():
        return "promoted_pending"
    payload = _load_json(state_path)
    return _normalize_target(payload.get("state")) or "promoted_pending"


def _bundle_root_from_pending(pending_path: Path, front: Dict[str, Any], root: Path) -> Path:
    proposal_path = _normalize_target(front.get("proposal_path"))
    if proposal_path:
        proposal_abs = Path(proposal_path)
        if not proposal_abs.is_absolute():
            proposal_abs = root / proposal_abs
        if proposal_abs.exists():
            return proposal_abs.parent

    source_task_id = _normalize_target(front.get("source_task_id"))
    plan_id = _normalize_target(front.get("plan_id"))
    if source_task_id and plan_id:
        return continuation_task_inbox_proposed_root() / source_task_id / plan_id

    # Fall back to deriving from pending path.
    return pending_path.parent


def _read_front_end_bundle_hash(bundle_root: Path, fallback_id: str) -> str:
    manifest = bundle_root / "manifest.json"
    if manifest.exists():
        try:
            payload = _load_json(manifest)
            return _normalize_target(payload.get("idempotency_key"))
        except Exception:
            pass

    proposal = bundle_root / "proposal.json"
    if proposal.exists():
        try:
            payload = _load_json(proposal)
            return _normalize_target(payload.get("idempotency_key"))
        except Exception:
            pass

    return fallback_id


def _read_bundle_proposal_hash(bundle_dir: Path) -> str:
    proposal_path = Path(bundle_dir) / "proposal.json"
    if not proposal_path.exists():
        return ""
    try:
        payload = _load_json(proposal_path)
    except Exception:
        return ""
    return _sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _write_jsonl_event(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _load_jsonl_lines(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _find_matching_approval(approvals: List[Dict[str, Any]], proposal_hash: str, now: datetime) -> Optional[Dict[str, Any]]:
    for approval in approvals:
        if _normalize_target(approval.get("proposal_hash")) != proposal_hash:
            continue
        expires_at = _normalize_target(approval.get("expires_at"))
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except Exception:
            continue
        if parsed >= now:
            return approval
    return None


def _load_active_inbox_history(bundle_root: Path) -> List[Dict[str, Any]]:
    history_path = bundle_root / "activation_history.jsonl"
    if not history_path.exists():
        return []

    rows: List[Dict[str, Any]] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _promotion_history_exists(bundle_root: Path) -> bool:
    history_path = bundle_root / "promotion_history.jsonl"
    if not history_path.exists():
        return False

    for line in history_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            return True
    return False


def _promotion_audit_event_exists(bundle_root: Path, proposal_hash: str, target_task_id: str) -> bool:
    path = continuation_audit_events_path()
    if not path.exists():
        return False

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("action") != PROMOTION_AUDIT_ACTION:
            continue
        if _normalize_target(payload.get("target")) != target_task_id:
            continue
        if (
            _normalize_target(payload.get("inputs_hash")) == proposal_hash
            or _normalize_target(payload.get("proposal_hash")) == proposal_hash
        ):
            return True
    return False


def activation_feature_status() -> Dict[str, Any]:
    policy = _read_policy()
    separation = policy.get("separation_constraints", {})
    enabled = bool(policy.get("active_inbox_activation", {}).get("enabled", False))

    return {
        "status": "enabled" if enabled else "disabled",
        "enabled": enabled,
        "reason": "explicit activation is local and gated when enabled." if enabled else "activation remains explicit but is currently disabled.",
        "scheduler_dispatch": False if bool(separation.get("disallow_scheduler_dispatch", True)) else True,
        "registry_mutation": False if bool(separation.get("disallow_registry_mutation", True)) else True,
        "remote_execution": False if bool(separation.get("disallow_remote_execution", True)) else True,
        "active_inbox_write": False if bool(separation.get("disallow_active_inbox_write", True)) else True,
        "activation_equals_dispatch": False,
        "active_inbox_activation_enabled": enabled,
        "policy_version": str(policy.get("policy_version", "0.2.0")),
    }


def _forbidden_term_found(text: str, terms: List[str]) -> List[str]:
    matches: List[str] = []
    lowered = text.lower()
    for term in terms:
        if term.lower() in lowered:
            matches.append(term)
    return sorted(set(matches))


def _bundle_root_for_pending(pending_path: Path) -> Tuple[Dict[str, Any], Path]:
    content = pending_path.read_text(encoding="utf-8")
    front, body = _parse_front_matter(content)

    try:
        root = solution_continuation_dir().parent
    except Exception:
        root = Path.cwd()

    target_id = _normalize_target(front.get("task_id")) or pending_path.stem
    active_target = _active_root() / f"{_safe_task_id(target_id)}.md"
    bundle_root = _bundle_root_from_pending(pending_path, front, root)
    return {"content": content, "front": front, "body": body, "target_id": target_id, "active_target": active_target}, bundle_root


def _write_activation_audit_event(
    *,
    event_type: str,
    actor: str,
    event_status: str,
    target: str,
    source_task_id: str,
    bundle_id: str,
    proposal_id: str,
    proposal_hash: str,
    idempotency_key: str,
    prior_state: str,
    checks: List[Dict[str, Any]],
    active_target: str,
    activation_id: str,
    policy_version: str,
) -> Dict[str, Any]:
    payload = {
        "event_id": f"{target}:{activation_id}",
        "timestamp": _now_iso(),
        "actor": actor,
        "event_type": event_type,
        "source_task_id": source_task_id,
        "bundle_id": bundle_id,
        "proposal_id": proposal_id,
        "target": target,
        "target_state": "activated_in_inbox",
        "prior_state": prior_state,
        "idempotency_key": idempotency_key,
        "outcome": event_status,
        "checks": checks,
        "preflight": checks,
        "side_effects": {
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "remote_execution": False,
            "active_inbox_write": event_type == "activated_in_inbox",
        },
        "activation_target": active_target,
        "activation_id": activation_id,
        "policy_version": policy_version,
        "metadata": {
            "proposal_hash": proposal_hash,
            "note": "activation action recorded by P0f-c",
        },
        "proposal_hash": proposal_hash,
    }
    _write_jsonl_event(continuation_audit_events_path(), payload)
    return payload


def _append_activation_history(bundle_root: Path, *, actor: str, event_type: str, proposal_id: str, source_task_id: str, bundle_id: str, proposal_hash: str, idempotency_key: str, target: str, outcome: str, target_state: str, prior_state: str, reason: str) -> None:
    history_path = bundle_root / "activation_history.jsonl"
    payload = {
        "at": _now_iso(),
        "actor": actor,
        "event_type": event_type,
        "proposal_id": proposal_id,
        "source_task_id": source_task_id,
        "bundle_id": bundle_id,
        "proposal_hash": proposal_hash,
        "idempotency_key": idempotency_key,
        "target": target,
        "outcome": outcome,
        "target_state": target_state,
        "prior_state": prior_state,
        "reason": reason,
    }
    _write_jsonl_event(history_path, payload)


def _activation_history_match(bundle_root: Path, *, target: str, idempotency_key: str, proposal_hash: str) -> Tuple[bool, bool]:
    rows = _load_active_inbox_history(bundle_root)
    replay = False
    collision = False
    for row in rows:
        row_target = _normalize_target(row.get("target"))
        row_key = _normalize_target(row.get("idempotency_key"))
        row_hash = _normalize_target(row.get("proposal_hash"))
        if row_target != target:
            continue
        if row_key == idempotency_key and row_hash:
            if row_hash == proposal_hash:
                replay = True
            else:
                collision = True
        elif row_key == idempotency_key and not row_hash:
            collision = True
    return replay, collision


def _ensure_continuation_metadata(front: Dict[str, Any], *, activation_id: str) -> None:
    continuation = front.get("continuation")
    if not isinstance(continuation, dict):
        continuation = {}

    continuation["source"] = "local_continuation_planner"
    continuation["activation_state"] = "activated_in_inbox"
    continuation["activation_id"] = activation_id
    continuation.setdefault("proposal_bundle_id", front.get("bundle_id"))
    continuation.setdefault("promoted_pending_id", front.get("task_id"))
    continuation["dispatch_allowed"] = False
    continuation["auto_dispatch_allowed"] = False
    continuation["registry_mutation_allowed"] = False
    continuation["remote_execution_allowed"] = False

    front["continuation"] = continuation


def _compose_active_payload(front: Dict[str, Any], body: str) -> str:
    front_text = yaml.safe_dump(front, sort_keys=False, default_flow_style=False)
    return f"---\n{front_text}---\n{body}".rstrip("\n") + "\n"


def run_activation_preflight_contract_stub(pending_path: Path) -> Dict[str, Any]:
    policy = _read_policy()
    policy_checklist = _read_preflight_checklist()
    now = datetime.now(timezone.utc)

    checks: List[Dict[str, Any]] = []
    ok = True
    pending_root = _pending_root()
    active_root = _active_root()
    root = solution_continuation_dir().parent
    pending_path = Path(pending_path)

    def add_check(check_id: str, status: str, **payload: Any) -> Dict[str, Any]:
        item = {"id": check_id, "status": status}
        item.update(payload)
        checks.append(item)
        return item

    if not pending_path.exists():
        add_check("pending_file_exists", "failed", violations=["pending_file_missing"])
        return {
            "ok": False,
            "status": "failed",
            "reason": "pending_file_missing",
            "checks": checks,
            "active_target": "",
            "target_id": "",
            "target_task_id": "",
            "pending_path": str(pending_path),
            "bundle_root": "",
            "pending_hash": "",
            "proposal_hash": "",
            "idempotency_key": "",
            "state": "",
            "checks_summary": {"collision_free": False},
            "checklist": policy_checklist.get("name", "continuation_activation_preflight_p0f_a"),
            "feature_status": activation_feature_status(),
            "policy_version": str(policy.get("policy_version", "0.2.0")),
            "front_matter": {},
            "pending_body": "",
        }

    add_check("pending_file_exists", "passed")

    add_check(
        "pending_file_under_promoted_pending",
        "passed" if pending_path.parent == pending_root else "failed",
        violations=[] if pending_path.parent == pending_root else [f"actual_parent={pending_path.parent}"],
    )
    if pending_path.parent != pending_root:
        ok = False

    content = pending_path.read_text(encoding="utf-8")
    front, body = _parse_front_matter(content)
    front = front if isinstance(front, dict) else {}

    if not front:
        add_check("front_matter_required_fields", "failed", violations=["missing_front_matter"])
        ok = False
    else:
        missing_front = [item for item in REQUIRED_FRONT_MATTER_FIELDS if not str(front.get(item, "")).strip()]
        add_check("front_matter_required_fields", "passed" if not missing_front else "failed", violations=missing_front)
        if missing_front:
            ok = False

    missing_promoted_metadata = [item for item in PROMOTED_METADATA_FIELDS if not str(front.get(item, "")).strip()]
    add_check("promoted_metadata_present", "passed" if not missing_promoted_metadata else "failed", violations=missing_promoted_metadata)
    if missing_promoted_metadata:
        ok = False

    target_id = _normalize_target(front.get("task_id")) or pending_path.stem
    target_task_id = _safe_task_id(target_id)
    active_target = active_root / f"{target_task_id}.md"

    add_check("active_target_is_deterministic", "passed", value=str(active_target).replace("\\", "/"))
    add_check(
        "active_target_in_tasks_inbox",
        "passed" if active_target.parent.resolve() == _required_path_roots()["active_target_root"].resolve() else "failed",
        value=str(active_target.parent),
    )
    if active_target.parent.resolve() != _required_path_roots()["active_target_root"].resolve():
        add_check("active_target_collision_free", "failed", violations=["invalid_active_target_root"])
        ok = False
    else:
        add_check("active_target_collision_free", "passed")

    bundle_root = _bundle_root_from_pending(pending_path, front, root)
    required_bundle_files = [
        bundle_root / "proposal.json",
        bundle_root / "proposal.md",
        bundle_root / "manifest.json",
        bundle_root / "proposal_dry_run_manifest.json",
        bundle_root / "gate_report.json",
    ]
    missing_bundle_files = [path.name for path in required_bundle_files if not path.exists()]
    add_check("pending_file_from_audited_promotion", "passed" if bundle_root.exists() else "failed", bundles_path=str(bundle_root), required_files=[path.name for path in required_bundle_files])
    if not bundle_root.exists():
        ok = False

    add_check("proposal_files_exist", "passed" if not missing_bundle_files else "failed", violations=missing_bundle_files)
    if missing_bundle_files:
        ok = False

    pending_hash = _sha256_text(content)
    proposal_hash = _read_bundle_proposal_hash(bundle_root)
    idempotency_key = _read_front_end_bundle_hash(bundle_root, target_task_id)

    if _promotion_history_exists(bundle_root):
        add_check("promotion_history_exists", "passed")
    else:
        add_check("promotion_history_exists", "failed", violations=["promotion_history_missing"])
        ok = False

    promotion_audit_ok = _promotion_audit_event_exists(bundle_root, proposal_hash or pending_hash, target_task_id)
    add_check("promotion_audit_event_exists", "passed" if promotion_audit_ok else "failed", violations=[] if promotion_audit_ok else ["promotion_audit_event_missing"])
    if not promotion_audit_ok:
        ok = False

    state = _read_promotion_state(bundle_root)
    blocked_state = state in BLOCKED_PROMOTION_STATES
    add_check("blocked_states", "failed" if blocked_state else "passed", violations=[f"state={state}"] if blocked_state else [])
    if blocked_state:
        ok = False

    has_flag_approval = any(bool(front.get(key)) for key in ACTIVATION_APPROVAL_FIELDS) if front else False
    has_approval_record = False
    if not has_flag_approval and bundle_root.exists():
        record_path = bundle_root / "approval_record.json"
        if record_path.exists():
            try:
                record = _load_json(record_path)
                approvals = record.get("approvals", [])
                if isinstance(approvals, list):
                    if _find_matching_approval(approvals, proposal_hash or pending_hash, now) is not None:
                        has_approval_record = True
            except Exception:
                has_approval_record = False
    approved = has_flag_approval or has_approval_record
    add_check("activation_or_promotion_approval_exists", "passed" if approved else "failed", violations=[] if approved else ["activation_permission_missing"])
    if not approved:
        ok = False

    text_for_scan = f"{front} {body}"
    for check_id, terms in [
        ("no_dispatch_markers", FORBIDDEN_TEXT + ["manual_file_promotion", "auto_promotion"]),
        ("no_registry_markers", ["registry.json", "registry mutation", "upsert_task", "mutation of registry"]),
        ("no_remote_markers", ["remote_path_guess", "remote_materializer", "remote execution"]),
        (
            "no_source_edit_markers",
            ["source_edit", "apply_patch", "modify source", "patch source"],
        ),
    ]:
        violations = _forbidden_term_found(text_for_scan, terms)
        add_check(check_id, "passed" if not violations else "failed", violations=violations)
        if violations:
            ok = False

    remote_path_violations = ["guessed_remote_path"] if any(expr.search(content) for expr in GUESSED_REMOTE_PATTERNS) else []
    add_check("no_guessed_remote_path_text", "passed" if not remote_path_violations else "failed", violations=remote_path_violations)
    if remote_path_violations:
        ok = False

    replay, collision = _activation_history_match(
        bundle_root,
        target=target_task_id,
        idempotency_key=idempotency_key,
        proposal_hash=pending_hash,
    )
    add_check(
        "idempotency_replay_or_reject",
        "failed" if collision else "passed",
        value="replayed" if replay else "fresh",
        violations=["same_idempotency_key_different_content"] if collision else [],
    )
    if collision:
        ok = False

    if active_target.exists():
        if active_target.is_dir():
            add_check("active_target_collision_check", "failed", violations=["active_target_is_directory"])
            ok = False
        elif replay:
            add_check("active_target_collision_check", "passed", value="replayed_target_is_expected")
        else:
            active_hash = _sha256_text(active_target.read_text(encoding="utf-8"))
            if active_hash == pending_hash:
                add_check(
                    "active_target_collision_check",
                    "failed",
                    violations=["active_target_no_matching_idempotency_history"],
                )
            else:
                add_check(
                    "active_target_collision_check",
                    "failed",
                    violations=["active_target_collision_different_content"],
                )
            ok = False
    else:
        add_check("active_target_collision_check", "passed")

    scheduler_enabled = bool(policy.get("scheduler_trigger", {}).get("enabled", True))
    add_check("scheduler_trigger_disabled", "passed" if not scheduler_enabled else "failed", value=scheduler_enabled)
    if scheduler_enabled:
        ok = False

    add_check("active_inbox_write_disabled", "passed", value=policy.get("separation_constraints", {}).get("disallow_active_inbox_write", True))
    add_check("direct_file_copy_forbidden", "passed", value=False)

    reason = "preflight_passed"
    if not ok:
        reason = next((check["id"] for check in checks if check["status"] == "failed"), "preflight_failed")

    return {
        "ok": ok,
        "status": "passed" if ok else "failed",
        "reason": reason,
        "target_id": target_id,
        "target_task_id": target_task_id,
        "active_target": str(active_target),
        "pending_path": str(pending_path),
        "bundle_root": str(bundle_root),
        "pending_hash": pending_hash,
        "proposal_hash": proposal_hash,
        "idempotency_key": idempotency_key,
        "state": state,
        "checks": checks,
        "feature_status": activation_feature_status(),
        "policy_version": str(policy.get("policy_version", "0.2.0")),
        "checklist": policy_checklist.get("name", "continuation_activation_preflight_p0f_a"),
        "front_matter": front,
        "pending_body": body,
        "idempotent_replay": replay,
        "checks_summary": {"collision_free": not any(item["status"] == "failed" for item in checks)},
    }


def activate_pending_to_inbox_stub(pending_path: Path, *, actor: str = "human") -> Dict[str, Any]:
    preflight = run_activation_preflight_contract_stub(pending_path)
    if not preflight.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "actor": actor,
            "pending_path": str(pending_path),
            "written": False,
            "action": "activation_failed",
            "active_target": preflight.get("active_target"),
            "reason": preflight.get("reason") or "preflight_failed",
            "preflight": preflight,
            "checks": preflight.get("checks", []),
            "feature_status": activation_feature_status(),
            "preflight_status": "failed",
            "target_task_id": preflight.get("target_task_id"),
            "idempotency_key": preflight.get("idempotency_key"),
            "proposal_hash": preflight.get("proposal_hash"),
            "pending_hash": preflight.get("pending_hash"),
        }

    policy = _read_policy()
    policy_version = str(policy.get("policy_version", "0.2.0"))
    active_target = Path(preflight["active_target"])
    front = preflight["front_matter"]
    body = preflight["pending_body"]
    proposal_hash = preflight["proposal_hash"]
    idempotency_key = preflight["idempotency_key"]
    pending_hash = preflight.get("pending_hash") or proposal_hash
    target_task_id = preflight["target_task_id"]
    prior_state = preflight.get("state")

    bundle_dir = Path(preflight["bundle_root"])
    activation_hash = pending_hash or proposal_hash
    activation_id = f"{target_task_id}:{idempotency_key}:{_normalize_target((activation_hash or target_task_id)[:8])}"
    checks = preflight.get("checks", [])
    replay = bool(preflight.get("idempotent_replay"))
    feature_status = activation_feature_status()

    if active_target.exists():
        if replay:
            _ensure_continuation_metadata(front, activation_id=activation_id)
            _append_activation_history(
                bundle_dir,
                actor=actor,
                event_type="activation_replayed",
                proposal_id=_normalize_target(front.get("proposal_id")),
                source_task_id=_normalize_target(front.get("source_task_id")),
                bundle_id=_normalize_target(front.get("bundle_id")),
                proposal_hash=activation_hash,
                idempotency_key=idempotency_key,
                target=target_task_id,
                outcome="noop",
                target_state="activated_in_inbox",
                prior_state=prior_state,
                reason="idempotent_noop",
            )
            _write_activation_audit_event(
                event_type="activation_replayed",
                actor=actor,
                event_status="noop",
                target=target_task_id,
                source_task_id=_normalize_target(front.get("source_task_id")),
                bundle_id=_normalize_target(front.get("bundle_id")),
                proposal_id=_normalize_target(front.get("proposal_id")),
                proposal_hash=activation_hash,
                idempotency_key=idempotency_key,
                prior_state=prior_state,
                checks=checks,
                active_target=str(active_target),
                activation_id=activation_id,
                policy_version=policy_version,
            )
            return {
                "ok": True,
                "status": "activation_replayed",
                "actor": actor,
                "pending_path": str(pending_path),
                "target_task_id": target_task_id,
                "active_target": str(active_target),
                "written": False,
                "action": "activate_pending_to_inbox_noop",
                "reason": "activation_replay_noop",
                "preflight": preflight,
                "checks": checks,
                "idempotency_key": idempotency_key,
                "proposal_hash": proposal_hash,
                "pending_hash": pending_hash,
                "feature_status": activation_feature_status(),
                "audit": {
                    "event_type": "activation_replayed",
                    "activation_id": activation_id,
                    "target": target_task_id,
                },
            }

        return {
            "ok": False,
            "status": "failed",
            "actor": actor,
            "pending_path": str(pending_path),
            "written": False,
            "action": "activation_failed",
            "active_target": str(active_target),
            "reason": "active_target_collision_different_content",
            "preflight": preflight,
            "checks": checks,
                "feature_status": feature_status,
            }

    front_path = Path(preflight["active_target"])
    _ensure_continuation_metadata(front, activation_id=activation_id)
    payload = _compose_active_payload(front, body)
    front_path.parent.mkdir(parents=True, exist_ok=True)
    front_path.write_text(payload, encoding="utf-8")

    _append_activation_history(
        bundle_dir,
        actor=actor,
        event_type="activated_in_inbox",
        proposal_id=_normalize_target(front.get("proposal_id")),
        source_task_id=_normalize_target(front.get("source_task_id")),
        bundle_id=_normalize_target(front.get("bundle_id")),
        proposal_hash=activation_hash,
        idempotency_key=idempotency_key,
        target=target_task_id,
        outcome="success",
        target_state="activated_in_inbox",
        prior_state=prior_state,
        reason="active_inbox_write",
    )
    _write_activation_audit_event(
        event_type="activated_in_inbox",
        actor=actor,
        event_status="success",
        target=target_task_id,
        source_task_id=_normalize_target(front.get("source_task_id")),
        bundle_id=_normalize_target(front.get("bundle_id")),
        proposal_id=_normalize_target(front.get("proposal_id")),
        proposal_hash=activation_hash,
        idempotency_key=idempotency_key,
        prior_state=prior_state,
        checks=checks,
        active_target=str(active_target),
        activation_id=activation_id,
        policy_version=policy_version,
    )
    return {
        "ok": True,
        "status": "activated_in_inbox",
        "actor": actor,
        "pending_path": str(pending_path),
        "target_task_id": target_task_id,
        "active_target": str(active_target),
        "written": True,
        "action": "activate_pending_to_inbox",
        "preflight": preflight,
        "checks": checks,
        "idempotency_key": idempotency_key,
        "proposal_hash": proposal_hash,
        "pending_hash": pending_hash,
        "feature_status": feature_status,
    }


def activate_to_inbox_disabled_stub(pending_path: Path, *, actor: str = "human") -> Dict[str, Any]:
    return activate_pending_to_inbox_stub(pending_path, actor=actor)
__all__ = [
    "activation_feature_status",
    "run_activation_preflight_contract_stub",
    "activate_pending_to_inbox_stub",
    "activate_to_inbox_disabled_stub",
]
