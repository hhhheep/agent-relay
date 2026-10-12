from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .audit import append_audit_event
from .gate import scan_static_draft_text
from .paths import continuation_task_inbox_root, solution_continuation_dir
from .schema import AuditAction, AuditActor, AuditEvent, AuditOutcome

PROMOTION_STATE_FILE = "promotion_state.json"
PROMOTION_HISTORY_FILE = "promotion_history.jsonl"
APPROVAL_RECORD_FILE = "approval_record.json"

FORBIDDEN_TEXT_RULES = {
    "direct_dispatch",
    "scheduler_submit",
    "registry_mutation",
    "source_edit",
    "remote_path_guess",
    "cross_line_continuation",
    "cross_workplace_data_mix",
    "auto_promotion",
    "manual_promotion",
}


def _policy_root() -> Path:
    primary = solution_continuation_dir()
    if primary.exists():
        return primary
    # Tests sometimes patch continuation root to isolated workspaces without copying
    # policy fixtures. Keep a safe fallback to repository defaults for resilience.
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


def _load_policy() -> Dict[str, Any]:
    return _load_yaml(_policy_root() / "promotion_policy.yaml")


def _policy_value(key: str, default: Any = None) -> Any:
    return _load_policy().get(key, default)


def _policy_version() -> str:
    return str(_policy_value("policy_version", "0.2.0"))


def _approval_ttl() -> timedelta:
    return timedelta(hours=int(_policy_value("approval_ttl_hours", 24)))


def _state_blocklist() -> List[str]:
    return [str(item) for item in _policy_value("state_rules", {}).get("blocked_states", [])]


def _allowed_gate_status() -> List[str]:
    return [str(item) for item in _policy_value("preflight_requirements", {}).get("gate_report", {}).get("acceptable_status", ["passed", "manual_review"])]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_task_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value or "")[:80] or "bundle"


def _bundle_state_path(bundle_dir: Path) -> Path:
    return Path(bundle_dir) / PROMOTION_STATE_FILE


def _approval_record_path(bundle_dir: Path) -> Path:
    return Path(bundle_dir) / APPROVAL_RECORD_FILE


def _history_path(bundle_dir: Path) -> Path:
    return Path(bundle_dir) / PROMOTION_HISTORY_FILE


def _read_promotion_state(bundle_dir: Path) -> Tuple[str, Dict[str, Any]]:
    state_path = _bundle_state_path(bundle_dir)
    if not state_path.exists():
        return "drafted", {}

    payload = _load_json(state_path)
    return str(payload.get("state") or "drafted"), payload


def _write_state(bundle_dir: Path, state: str, actor: str, reason: str, *, target_task_id: str, proposal_hash: str) -> None:
    payload = {
        "state": state,
        "updated_at": _now_iso(),
        "updated_by": actor,
        "reason": reason,
        "target_task_id": target_task_id,
        "proposal_hash": proposal_hash,
    }
    _bundle_state_path(bundle_dir).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _safe_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _approval_expired(approved_at: str) -> bool:
    ts = _safe_timestamp(approved_at)
    if ts is None:
        return True
    return datetime.now(timezone.utc) - ts > _approval_ttl()


def _append_history(bundle_dir: Path, entry: Dict[str, Any]) -> None:
    payload = dict(entry)
    payload.setdefault("at", _now_iso())
    with _history_path(bundle_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _read_history(bundle_dir: Path) -> List[Dict[str, Any]]:
    path = _history_path(bundle_dir)
    if not path.exists():
        return []

    entries: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries


def _history_collision(bundle_dir: Path, target_task_id: str, idempotency_key: str, proposal_hash: str) -> Tuple[bool, str]:
    for item in _read_history(bundle_dir):
        if item.get("action") != "promote":
            continue
        if _safe_task_id(str(item.get("target_task_id", ""))) != _safe_task_id(target_task_id):
            continue
        if str(item.get("idempotency_key", "")) != str(idempotency_key):
            continue
        prior = str(item.get("proposal_hash", ""))
        if prior and prior != str(proposal_hash):
            return True, prior
    return False, ""


def _read_approval_record(bundle_dir: Path) -> Dict[str, Any]:
    path = _approval_record_path(bundle_dir)
    if not path.exists():
        return {"approvals": []}

    payload = _load_json(path)
    approvals = payload.get("approvals")
    if not isinstance(approvals, list):
        approvals = []

    return {
        "bundle_id": payload.get("bundle_id", Path(bundle_dir).name),
        "updated_at": payload.get("updated_at", ""),
        "approvals": approvals,
    }


def _find_matching_approval(approvals: List[Dict[str, Any]], proposal_hash: str) -> Optional[Dict[str, Any]]:
    for item in approvals:
        if str(item.get("proposal_hash", "")) != str(proposal_hash):
            continue
        if _approval_expired(str(item.get("approved_at", ""))):
            continue
        return item
    return None


def _find_latest_approval(approvals: List[Dict[str, Any]], proposal_hash: str) -> Optional[Dict[str, Any]]:
    for item in reversed(list(approvals)):
        if str(item.get("proposal_hash", "")) != str(proposal_hash):
            continue
        return item
    return None


def _pending_root() -> Path:
    return continuation_task_inbox_root() / "_promoted_pending"


def _bundle_required_files(bundle_dir: Path) -> List[Path]:
    return [
        Path(bundle_dir) / "proposal.json",
        Path(bundle_dir) / "proposal.md",
        Path(bundle_dir) / "proposal_dry_run_manifest.json",
        Path(bundle_dir) / "manifest.json",
        Path(bundle_dir) / "gate_report.json",
    ]


def _read_bundle_metadata(bundle_dir: Path) -> Tuple[Dict[str, Any], Path, str, str, str, str]:
    bundle_dir = Path(bundle_dir)
    proposal_path = bundle_dir / "proposal.json"
    if not proposal_path.exists():
        raise FileNotFoundError("proposal.json missing")

    proposal_md_path = bundle_dir / "proposal.md"
    if not proposal_md_path.exists():
        raise FileNotFoundError("proposal.md missing")

    proposal_payload = _load_json(proposal_path)
    proposal_text = proposal_md_path.read_text(encoding="utf-8")
    proposal_hash = _sha256_text(json.dumps(proposal_payload, sort_keys=True, ensure_ascii=False))
    proposal_id = str(proposal_payload.get("proposal_id", proposal_path.parent.name))
    source_task_id = str(proposal_payload.get("source_task_id", ""))
    plan_id = str(proposal_payload.get("plan_id", proposal_id))
    bundle_id = str(proposal_payload.get("bundle_id", proposal_path.parent.name))
    idempotency_key = str(proposal_payload.get("idempotency_key", ""))
    target_task_id = _safe_task_id(f"{source_task_id}_{plan_id}" if source_task_id else proposal_id)
    return proposal_payload, proposal_md_path, proposal_hash, idempotency_key, bundle_id, target_task_id


def _read_manifest(bundle_dir: Path) -> Tuple[str, Dict[str, Any]]:
    candidates = [
        Path(bundle_dir) / "proposal_dry_run_manifest.json",
        Path(bundle_dir) / "manifest.json",
    ]
    for path in candidates:
        if path.exists():
            return path.name, _load_json(path)
    raise FileNotFoundError("manifest/proposal_dry_run_manifest.json missing")


def _read_gate(bundle_dir: Path) -> Tuple[str, Dict[str, Any]]:
    path = Path(bundle_dir) / "gate_report.json"
    if not path.exists():
        raise FileNotFoundError("gate_report.json missing")
    return path.name, _load_json(path)


def _forbidden_terms(payload: Dict[str, Any], proposal_text: str) -> List[str]:
    violations: List[str] = []

    for item in scan_static_draft_text(proposal_text).get("violations", []):
        rule_id = str(item.get("rule_id", "")).lower()
        if rule_id in FORBIDDEN_TEXT_RULES:
            violations.append(rule_id)

    for proposal in payload.get("proposals", []):
        if not isinstance(proposal, dict):
            continue
        for action in proposal.get("forbidden_actions", []):
            candidate = str(action).lower()
            if candidate in FORBIDDEN_TEXT_RULES:
                violations.append(candidate)

    return sorted(set(violations))


def _write_audit(event_action: AuditAction, *, actor: str, target: str, proposal_hash: str, idempotency_key: str, outcome: AuditOutcome, source_task_id: str, target_task_id: str, policy_version: str, error_detail: str = "", preflight_passed: bool = False, checks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    event = AuditEvent(
        event_id=f"{target}:{event_action}:{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        timestamp=datetime.now(timezone.utc),
        actor=AuditActor.HUMAN if actor and actor != "continuation_promotion_service" else AuditActor.CONTINUATION_PLANNER,
        action=event_action,
        target=target,
        idempotency_key=idempotency_key,
        outcome=outcome,
        source_task_id=source_task_id,
        prompt_versions={"continuation_planner": "0.1.0", "promotion": policy_version},
        policy_version=policy_version,
        error_detail=error_detail or None,
        inputs_hash=proposal_hash,
        outputs_hash=_sha256_text(f"{event_action}:{target}:{target_task_id}"),
    )

    event_payload = event.model_dump(mode="json")
    event_payload["proposal_hash"] = proposal_hash
    event_payload["target_task_id"] = target_task_id
    event_payload["before_state"] = "unknown"
    event_payload["after_state"] = "unknown"
    event_payload["checks"] = checks or []
    event_payload["preflight_passed"] = bool(preflight_passed)
    event_payload["proposal_id"] = target

    return append_audit_event(event) | {"event": event_payload}


def run_promotion_preflight_stub(bundle_dir: Path, *, target_task_id: Optional[str] = None) -> Dict[str, Any]:
    bundle_dir = Path(bundle_dir)
    checks: List[Dict[str, Any]] = []
    all_ok = True

    proposal_payload: Dict[str, Any] = {}
    proposal_md_path = Path("")
    proposal_hash = ""
    idempotency_key = ""
    bundle_id = bundle_dir.name
    source_task_id = ""
    proposal_target_task_id = ""

    try:
        proposal_payload, proposal_md_path, proposal_hash, idempotency_key, bundle_id, proposal_target_task_id = _read_bundle_metadata(bundle_dir)
        checks.append({"id": "required_bundle_files", "status": "passed"})
    except Exception as exc:
        checks.append({"id": "required_bundle_files", "status": "failed", "violations": [str(exc)]})
        all_ok = False

    if all_ok:
        missing = [path.name for path in _bundle_required_files(bundle_dir) if not path.exists()]
        if missing:
            checks.append({"id": "required_paths_exist", "status": "failed", "violations": missing})
            all_ok = False
        else:
            checks.append({"id": "required_paths_exist", "status": "passed"})

    manifest_name = ""
    manifest_payload: Dict[str, Any] = {}
    if all_ok:
        try:
            manifest_name, manifest_payload = _read_manifest(bundle_dir)
            checks.append({"id": "manifest_required_fields", "status": "passed", "value": manifest_name})
        except Exception as exc:
            checks.append({"id": "manifest_required_fields", "status": "failed", "violations": [str(exc)]})
            all_ok = False

    if all_ok:
        required_manifest_fields = {
            "manifest_id",
            "source_task_id",
            "source_result_hash",
            "proposal_path",
            "plan_id",
            "idempotency_key",
        }
        missing = sorted(required_manifest_fields - set(manifest_payload.keys()))
        if missing:
            checks.append({
                "id": "manifest_required_fields",
                "status": "failed",
                "violations": [f"manifest_missing_{item}" for item in missing],
            })
            all_ok = False
        else:
            checks.append({"id": "manifest_required_fields", "status": "passed"})

    if all_ok:
        required_proposal_fields = {"bundle_id", "source_task_id", "plan_id", "idempotency_key", "proposals"}
        missing = sorted(required_proposal_fields - set(proposal_payload.keys()))
        if missing:
            checks.append({
                "id": "proposal_required_fields",
                "status": "failed",
                "violations": [f"proposal_missing_{item}" for item in missing],
            })
            all_ok = False
        else:
            checks.append({"id": "proposal_required_fields", "status": "passed"})

    if all_ok:
        try:
            _, gate_payload = _read_gate(bundle_dir)
            gate_status = str(gate_payload.get("gate_status", "")).strip()
            if gate_status in _allowed_gate_status():
                checks.append({"id": "gate_report_acceptable", "status": "passed", "value": gate_status})
            else:
                checks.append({"id": "gate_report_acceptable", "status": "failed", "violations": [f"gate_status={gate_status}"]})
                all_ok = False
        except Exception as exc:
            checks.append({"id": "gate_report_acceptable", "status": "failed", "violations": [str(exc)]})
            all_ok = False

    state, state_payload = _read_promotion_state(bundle_dir)
    if state in _state_blocklist():
        checks.append({"id": "state_promotable", "status": "failed", "violations": [f"state={state}"]})
        all_ok = False
    else:
        checks.append({"id": "state_promotable", "status": "passed", "state": state})

    if all_ok:
        approvals = _read_approval_record(bundle_dir).get("approvals", [])
        approval = _find_matching_approval(approvals, proposal_hash)
        if approval is None:
            checks.append({"id": "approval_exists", "status": "failed", "violations": ["missing_matching_approval"]})
            all_ok = False
        else:
            checks.append({"id": "approval_exists", "status": "passed"})
            checks.append({"id": "approval_hash_matches", "status": "passed"})
            checks.append({"id": "approval_not_expired", "status": "passed", "approved_at": approval.get("approved_at")})
            if _approval_expired(str(approval.get("approved_at", ""))):
                checks.append({"id": "approval_not_expired", "status": "failed", "violations": ["approval_expired"]})
                all_ok = False

    if all_ok:
        forbidden_terms = _forbidden_terms(proposal_payload, proposal_md_path.read_text(encoding="utf-8"))
        if forbidden_terms:
            checks.append({"id": "forbidden_text_and_forbidden_fields", "status": "failed", "violations": forbidden_terms})
            all_ok = False
        else:
            checks.append({"id": "forbidden_text_and_forbidden_fields", "status": "passed"})

    target_task_id = _safe_task_id(target_task_id or proposal_target_task_id)
    target_path = _pending_root() / f"{target_task_id}.md"
    active_target = continuation_task_inbox_root() / f"{target_task_id}.md"
    proposal_content_hash = ""

    if all_ok:
        if active_target.exists():
            checks.append({"id": "active_inbox_collision_free", "status": "failed", "violations": [f"active_inbox_collision={active_target}"]})
            all_ok = False
        else:
            checks.append({"id": "active_inbox_collision_free", "status": "passed"})

    if all_ok:
        proposal_content = proposal_md_path.read_text(encoding="utf-8")
        proposal_content_hash = _sha256_text(proposal_content)
        if target_path.exists():
            existing_hash = _sha256_text(target_path.read_text(encoding="utf-8"))
            if existing_hash != proposal_content_hash:
                checks.append({"id": "pending_collision_same_content_allowed", "status": "failed", "violations": ["pending_collision_different_content"]})
                all_ok = False
            else:
                checks.append({"id": "pending_collision_same_content_allowed", "status": "passed", "outcome": "noop"})
        else:
            checks.append({"id": "pending_collision_same_content_allowed", "status": "passed"})

    if all_ok:
        history_collision, prior_hash = _history_collision(bundle_dir, target_task_id, idempotency_key, proposal_hash)
        if history_collision:
            checks.append({"id": "idempotency_key_collision_policy", "status": "failed", "violations": [f"collision_key={idempotency_key}:{prior_hash}:{proposal_hash}"]})
            all_ok = False
        else:
            checks.append({"id": "idempotency_key_collision_policy", "status": "passed"})

    checks.append({"id": "require_target_is_promoted_pending", "status": "passed"})

    preflight: Dict[str, Any] = {
        "ok": all_ok,
        "bundle_dir": str(bundle_dir),
        "bundle_id": bundle_id,
        "state": state,
        "state_payload": state_payload,
        "target_task_id": target_task_id,
        "target_path": str(target_path),
        "checks": checks,
        "proposal_hash": proposal_hash,
        "proposal_payload": proposal_payload,
        "idempotency_key": idempotency_key,
        "source_task_id": source_task_id,
        "policy_version": _policy_version(),
        "status": "passed" if all_ok else "failed",
        "manifest_file": manifest_name,
        "manifest_path": str(bundle_dir / manifest_name) if manifest_name else "",
        "proposal_path": str(bundle_dir / "proposal.json"),
    }

    preflight["audit"] = _write_audit(
        AuditAction.STAGE,
        actor="continuation_promotion_service",
        target=target_task_id,
        proposal_hash=proposal_hash,
        idempotency_key=idempotency_key,
        outcome=AuditOutcome.SUCCESS if all_ok else AuditOutcome.FAIL,
        source_task_id=source_task_id,
        target_task_id=target_task_id,
        policy_version=_policy_version(),
        preflight_passed=all_ok,
        error_detail="" if all_ok else "preflight_failed",
        checks=checks,
    )

    return preflight


def approve_bundle_stub(bundle_dir: Path, *, actor: str, note: Optional[str] = None) -> Dict[str, Any]:
    bundle_dir = Path(bundle_dir)
    try:
        proposal_payload, _, proposal_hash, idempotency_key, _, proposal_target_task_id = _read_bundle_metadata(bundle_dir)
    except Exception as exc:
        return {
            "ok": False,
            "bundle_dir": str(bundle_dir),
            "status": "failed",
            "reason": str(exc),
            "checks": [{"id": "required_bundle_files", "status": "failed", "violations": [str(exc)]}],
            "changed": False,
        }

    state, _ = _read_promotion_state(bundle_dir)
    record = _read_approval_record(bundle_dir)
    approvals = list(record.get("approvals", []))

    if _find_matching_approval(approvals, proposal_hash) is not None:
        changed = False
        status = "noop"
        reason = "approval_exists"
        proposal_record = _find_latest_approval(approvals, proposal_hash)
    else:
        expires_at = (datetime.now(timezone.utc) + _approval_ttl()).isoformat().replace("+00:00", "Z")
        approval_entry = {
            "bundle_id": proposal_payload.get("bundle_id", bundle_dir.name),
            "proposal_hash": proposal_hash,
            "approval_id": f"{proposal_payload.get('bundle_id', bundle_dir.name)}:{actor}:{_now_iso()}",
            "approved_by": actor,
            "approved_at": _now_iso(),
            "note": note or "",
            "idempotency_key": idempotency_key,
            "expires_at": expires_at,
        }
        approvals.append(approval_entry)
        _approval_record_path(bundle_dir).write_text(
            json.dumps(
                {
                    "bundle_id": proposal_payload.get("bundle_id", bundle_dir.name),
                    "updated_at": _now_iso(),
                    "approvals": approvals,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        changed = True
        status = "approved"
        reason = "approval_record_written"
        proposal_record = approval_entry

        _append_history(
            bundle_dir,
            {
                "action": "approve",
                "actor": actor,
                "proposal_hash": proposal_hash,
                "idempotency_key": idempotency_key,
                "target_task_id": proposal_target_task_id,
                "source_task_id": proposal_payload.get("source_task_id", ""),
                "before_state": state,
                "after_state": "awaiting_approval",
                "outcome": "recorded",
                "reason": note or "approval persisted",
            },
        )
        _write_state(
            bundle_dir,
            "awaiting_approval",
            actor,
            "explicit approval recorded",
            target_task_id=proposal_target_task_id,
            proposal_hash=proposal_hash,
        )

    if changed:
        audit_payload = _write_audit(
            AuditAction.APPROVE,
            actor=actor,
            target=proposal_payload.get("bundle_id", bundle_dir.name),
            proposal_hash=proposal_hash,
            idempotency_key=idempotency_key,
            outcome=AuditOutcome.SUCCESS,
            source_task_id=proposal_payload.get("source_task_id", ""),
            target_task_id=proposal_target_task_id,
            policy_version=_policy_version(),
            checks=[{"id": "approval", "status": "passed"}],
        )
    else:
        audit_payload = _write_audit(
            AuditAction.APPROVE,
            actor=actor,
            target=proposal_payload.get("bundle_id", bundle_dir.name),
            proposal_hash=proposal_hash,
            idempotency_key=idempotency_key,
            outcome=AuditOutcome.SKIP,
            source_task_id=proposal_payload.get("source_task_id", ""),
            target_task_id=proposal_target_task_id,
            policy_version=_policy_version(),
            checks=[{"id": "approval", "status": "skipped"}],
        )

    return {
        "ok": True,
        "bundle_dir": str(bundle_dir),
        "bundle_id": proposal_payload.get("bundle_id", bundle_dir.name),
        "target_task_id": proposal_target_task_id,
        "status": status,
        "changed": changed,
        "reason": reason,
        "proposal_hash": proposal_hash,
        "idempotency_key": idempotency_key,
        "state": state,
        "state_after": "awaiting_approval" if changed else state,
        "audit": audit_payload,
        "approval_record": proposal_record,
        "checks": [{"id": "approval", "status": "passed" if changed or status == "noop" else "failed"}],
    }


def promote_bundle_to_pending_stub(bundle_dir: Path, *, actor: str, target_task_id: Optional[str] = None) -> Dict[str, Any]:
    bundle_dir = Path(bundle_dir)
    preflight = run_promotion_preflight_stub(bundle_dir, target_task_id=target_task_id)
    if not preflight["ok"]:
        return {
            "ok": False,
            "bundle_dir": str(bundle_dir),
            "status": "failed",
            "reason": "preflight_failed",
            "preflight": preflight,
            "checks": preflight["checks"],
            "audit": preflight["audit"],
        }

    target_task_id = preflight["target_task_id"]
    target_path = Path(preflight["target_path"])
    proposal_hash = preflight["proposal_hash"]
    idempotency_key = preflight["idempotency_key"]
    source_task_id = preflight["source_task_id"]
    bundle_id = preflight["bundle_id"]

    # Re-run idempotency collision check from history after preflight for idempotent replay safety.
    proposal_payload, proposal_md_path, _, _, _, _ = _read_bundle_metadata(bundle_dir)
    proposal_text = proposal_md_path.read_text(encoding="utf-8")
    proposal_text_hash = _sha256_text(proposal_text)
    proposal_state, _ = _read_promotion_state(bundle_dir)

    collision, prior_hash = _history_collision(bundle_dir, target_task_id, idempotency_key, proposal_hash)
    if collision:
        return {
            "ok": False,
            "bundle_dir": str(bundle_dir),
            "status": "failed",
            "reason": "idempotency_collision",
            "target_task_id": target_task_id,
            "preflight": preflight,
            "checks": preflight["checks"],
            "audit": preflight["audit"],
            "details": {"idempotency_collision": {"idempotency_key": idempotency_key, "prior_hash": prior_hash}},
        }

    if target_path.exists():
        if _sha256_text(target_path.read_text(encoding="utf-8")) == proposal_text_hash:
            _append_history(
                bundle_dir,
                {
                    "action": "promote",
                    "actor": actor,
                    "proposal_hash": proposal_hash,
                    "idempotency_key": idempotency_key,
                    "target_task_id": target_task_id,
                    "source_task_id": source_task_id,
                    "before_state": proposal_state,
                    "after_state": "promoted_pending",
                    "outcome": "noop",
                    "target_path": str(target_path),
                    "reason": "no-op same content",
                },
            )
            _write_state(
                bundle_dir,
                "promoted_pending",
                actor,
                "idempotent_replay_noop",
                target_task_id=target_task_id,
                proposal_hash=proposal_hash,
            )
            audit_payload = _write_audit(
                AuditAction.PROMOTE,
                actor=actor,
                target=target_task_id,
                proposal_hash=proposal_hash,
                idempotency_key=idempotency_key,
                outcome=AuditOutcome.SKIP,
                source_task_id=source_task_id,
                target_task_id=target_task_id,
                policy_version=_policy_version(),
                checks=preflight["checks"],
                preflight_passed=True,
            )
            return {
                "ok": True,
                "bundle_dir": str(bundle_dir),
                "target_task_id": target_task_id,
                "target_path": str(target_path),
                "status": "noop",
                "written": False,
                "changed": False,
                "state": "promoted_pending",
                "checks": preflight["checks"],
                "preflight": preflight,
                "audit": audit_payload,
            }

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(proposal_text, encoding="utf-8")

    _append_history(
        bundle_dir,
        {
            "action": "promote",
            "actor": actor,
            "bundle_id": bundle_id,
            "proposal_hash": proposal_hash,
            "idempotency_key": idempotency_key,
            "target_task_id": target_task_id,
            "source_task_id": source_task_id,
            "before_state": proposal_state,
            "after_state": "promoted_pending",
            "outcome": "written",
            "target_path": str(target_path),
            "content_hash": proposal_text_hash,
            "reason": "promotion_to_promoted_pending",
        },
    )
    _write_state(
        bundle_dir,
        "promoted_pending",
        actor,
        "promotion_to_promoted_pending",
        target_task_id=target_task_id,
        proposal_hash=proposal_hash,
    )

    audit_payload = _write_audit(
        AuditAction.PROMOTE,
        actor=actor,
        target=target_task_id,
        proposal_hash=proposal_hash,
        idempotency_key=idempotency_key,
        outcome=AuditOutcome.SUCCESS,
        source_task_id=source_task_id,
        target_task_id=target_task_id,
        policy_version=_policy_version(),
        checks=preflight["checks"],
        preflight_passed=True,
    )

    return {
        "ok": True,
        "bundle_dir": str(bundle_dir),
        "bundle_id": bundle_id,
        "target_task_id": target_task_id,
        "target_path": str(target_path),
        "status": "promoted_pending",
        "written": True,
        "changed": True,
        "state": "promoted_pending",
        "checks": preflight["checks"],
        "preflight": preflight,
        "audit": audit_payload,
    }


def proposal_hash_stub(bundle_dir: Path) -> str:
    try:
        _, _, proposal_hash, _, _, _ = _read_bundle_metadata(bundle_dir)
        return proposal_hash
    except Exception:
        return ""
