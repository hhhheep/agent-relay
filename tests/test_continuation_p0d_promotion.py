from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unittest.mock import patch

import pytest

from solution_agent.continuation.promotion import (
    approve_bundle_stub,
    proposal_hash_stub,
    promote_bundle_to_pending_stub,
    run_promotion_preflight_stub,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _copy_promotion_contracts(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / ".solution" / "continuation"
    target = tmp_path / ".solution" / "continuation"
    target.mkdir(parents=True, exist_ok=True)
    for src in source.glob("*.yaml"):
        (target / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _patch_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)
    _copy_promotion_contracts(tmp_path)
    return tmp_path


def _write_bundle(
    root: Path,
    *,
    source_task_id: str = "source_001",
    plan_id: str = "plan_001",
    bundle_id: str = "bundle_001",
    proposal_id: str = "proposal_001",
    source_result_hash: str = "hash_001",
    idempotency_key: str = "source_001:hash_001:0.1.0:plan",
    proposals: list[dict] | None = None,
    proposal_text: str = "# Proposal\n\nsafe body.\n",
    gate_status: str = "passed",
    state: str = "drafted",
) -> Path:
    bundle_dir = root / "tasks" / "inbox" / "_proposed" / source_task_id / plan_id
    bundle_dir.mkdir(parents=True, exist_ok=True)

    proposal_payload = {
        "bundle_id": bundle_id,
        "proposal_id": proposal_id,
        "source_task_id": source_task_id,
        "source_result_hash": source_result_hash,
        "plan_id": plan_id,
        "idempotency_key": idempotency_key,
        "proposal_count": 1,
        "proposals": proposals if proposals is not None else [{"forbidden_actions": []}],
    }

    manifest_payload = {
        "manifest_id": f"{proposal_id}_manifest",
        "source_task_id": source_task_id,
        "source_result_hash": source_result_hash,
        "proposal_path": f"tasks/inbox/_proposed/{source_task_id}/{plan_id}/proposal.md",
        "plan_id": plan_id,
        "idempotency_key": idempotency_key,
        "proposal_count": 1,
    }

    gate_payload = {
        "report_id": f"{proposal_id}__gate",
        "bundle_id": bundle_id,
        "source_task_id": source_task_id,
        "source_result_hash": source_result_hash,
        "idempotency_key": idempotency_key,
        "gate_status": gate_status,
        "checks": [],
        "violations": [],
        "prompt_versions": {},
        "policy_version": "0.2.0",
    }

    (bundle_dir / "proposal.json").write_text(json.dumps(proposal_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (bundle_dir / "proposal.md").write_text(proposal_text, encoding="utf-8")
    (bundle_dir / "proposal_dry_run_manifest.json").write_text(json.dumps(manifest_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (bundle_dir / "gate_report.json").write_text(json.dumps(gate_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    if state:
        (bundle_dir / "promotion_state.json").write_text(
            json.dumps(
                {
                    "state": state,
                    "updated_at": _iso_now(),
                    "updated_by": "system",
                    "reason": "test setup",
                    "target_task_id": f"{source_task_id}_{plan_id}",
                    "proposal_hash": proposal_hash_stub(bundle_dir),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    return bundle_dir


def _write_approval(bundle_dir: Path, proposal_hash: str, *, actor: str = "reviewer", idempotency_key: str | None = None, expired: bool = False) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    exp = now - timedelta(hours=1) if expired else now + timedelta(hours=1)
    payload = {
        "bundle_id": bundle_dir.name,
        "updated_at": now.isoformat().replace("+00:00", "Z"),
        "approvals": [
            {
                "bundle_id": bundle_dir.name,
                "proposal_hash": proposal_hash,
                "approval_id": f"{bundle_dir.name}:{actor}:{now.isoformat()}",
                "approved_by": actor,
                "approved_at": now.isoformat().replace("+00:00", "Z"),
                "note": "approved by test",
                "idempotency_key": idempotency_key or "idempotent",
                "expires_at": exp.isoformat().replace("+00:00", "Z"),
            }
        ],
    }
    (bundle_dir / "approval_record.json").write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return payload["approvals"][0]


def _read_history_lines(bundle_dir: Path) -> list[dict]:
    history_path = bundle_dir / "promotion_history.jsonl"
    if not history_path.exists():
        return []
    rows = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _read_audit_events(root: Path) -> list[dict]:
    audit_path = root / "continuation" / "audit" / "events.jsonl"
    if not audit_path.exists():
        return []
    rows = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def test_approve_records_and_does_not_promote(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)

    result = approve_bundle_stub(bundle_dir, actor="alice", note="approve")
    assert result["ok"] is True
    assert result["status"] == "approved"
    assert result["changed"] is True
    assert result["state_after"] == "awaiting_approval"

    audit_events = _read_audit_events(tmp_path)
    assert any(event["action"] == "approve" for event in audit_events)

    history = _read_history_lines(bundle_dir)
    assert any(row.get("action") == "approve" for row in history)

    target_path = tmp_path / "tasks" / "inbox" / "_promoted_pending" / "source_001_plan_001.md"
    assert not target_path.exists()
    assert not (tmp_path / "tasks" / "inbox" / "source_001_plan_001.md").exists()


def test_promote_fails_without_approval(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)

    result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert not (tmp_path / "tasks" / "inbox" / "_promoted_pending").exists()


def test_promote_fails_with_stale_hash_approval_record(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)
    _write_approval(bundle_dir, "stale_hash", idempotency_key="source_001:hash_001:0.1.0:plan")

    result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert not (tmp_path / "tasks" / "inbox" / "_promoted_pending" / "source_001_plan_001.md").exists()


def test_promote_fails_when_gate_report_failed(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path, gate_status="failed")
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert not any(check["id"] == "gate_report_acceptable" and check["status"] == "passed" for check in result["checks"])
    assert not (tmp_path / "tasks" / "inbox" / "_promoted_pending" / "source_001_plan_001.md").exists()


@pytest.mark.parametrize("state", ["rejected", "revoked", "expired", "superseded"])
def test_promote_blocked_for_hard_states(tmp_path, monkeypatch, state):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path, state=state)
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert result["ok"] is False
    assert any(check["id"] == "state_promotable" and check["status"] == "failed" for check in result["checks"])


@pytest.mark.parametrize("missing", ["proposal.json", "proposal.md", "proposal_dry_run_manifest.json", "manifest.json", "gate_report.json"])
def test_promote_fails_with_missing_required_artifact(tmp_path, monkeypatch, missing):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)
    (bundle_dir / missing).unlink()
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert result["ok"] is False
    assert result["status"] == "failed"


def test_promote_fails_on_active_inbox_collision(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    active = tmp_path / "tasks" / "inbox" / "source_001_plan_001.md"
    active.write_text("# existing", encoding="utf-8")

    result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert active.exists()
    assert not (tmp_path / "tasks" / "inbox" / "_promoted_pending" / "source_001_plan_001.md").exists()


def test_promote_rejects_forbidden_text_in_proposal(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(
        tmp_path,
        proposal_text="# Proposal\n\nSchedule_remote this task immediately.\n",
    )
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert any(check["id"] == "forbidden_text_and_forbidden_fields" and check["status"] == "failed" for check in result["preflight"]["checks"])
    assert not (tmp_path / "tasks" / "inbox" / "_promoted_pending" / "source_001_plan_001.md").exists()


def test_promote_rejects_forbidden_frontmatter_field_tokens(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(
        tmp_path,
        proposals=[{"forbidden_actions": ["direct_dispatch", "scheduler_submit"]}],
    )
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert any(check["id"] == "forbidden_text_and_forbidden_fields" and check["status"] == "failed" for check in result["preflight"]["checks"])


def test_promote_same_content_is_idempotent(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    first = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert first["ok"] is True
    assert first["status"] == "promoted_pending"
    assert first["written"] is True

    second = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert second["ok"] is True
    assert second["status"] in {"noop", "skipped"}
    assert second["changed"] is False

    rows = _read_history_lines(bundle_dir)
    outcomes = {row["action"]: row["outcome"] for row in rows if row.get("action") == "promote"}
    assert outcomes.get("promote") == "noop"


def test_promote_same_idempotency_key_different_content_fails(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)
    first_hash = proposal_hash_stub(bundle_dir)
    _write_approval(bundle_dir, first_hash, idempotency_key="source_001:hash_001:0.1.0:plan")
    assert promote_bundle_to_pending_stub(bundle_dir, actor="alice")["ok"] is True

    # Same idempotency key, different proposal content/hash
    (bundle_dir / "proposal.md").write_text("# Proposal\n\nupdated content intentionally different.\n", encoding="utf-8")
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    second = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert second["ok"] is False
    assert second["status"] == "failed"
    assert "preflight_failed" == second.get("reason", "")
    assert any(
        check["id"] in {"idempotency_key_collision_policy", "pending_collision_same_content_allowed"}
        for check in second["checks"]
    )


def test_promote_records_history_and_audit_event(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert result["ok"] is True

    rows = _read_history_lines(bundle_dir)
    assert any(row.get("action") == "promote" and row.get("outcome") == "written" for row in rows)

    audit = _read_audit_events(tmp_path)
    assert any(event.get("action") == "promote" for event in audit)


def test_promote_no_dispatch_no_remote_no_registry_side_effects(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"tasks": {}}, indent=2) + "\n", encoding="utf-8")

    before_active = (tmp_path / "tasks" / "inbox" / "_promoted_pending").exists()
    before_registry = registry.read_text(encoding="utf-8")

    with (
        patch("subprocess.run") as run_mock,
        patch("subprocess.check_output") as check_output_mock,
        patch("subprocess.Popen") as popen_mock,
    ):
        run_mock.assert_not_called()
        run_mock.reset_mock()
        check_output_mock.assert_not_called()
        check_output_mock.reset_mock()

        result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
        assert result["ok"] is True

        assert run_mock.call_count == 0
        assert check_output_mock.call_count == 0
        assert popen_mock.call_count == 0

    assert registry.read_text(encoding="utf-8") == before_registry
    assert before_active is False
    assert not (tmp_path / "remote" / "materials.txt").exists()


def test_promote_writes_only_to_promoted_pending_dir(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert result["ok"] is True
    assert result["target_path"] == str(tmp_path / "tasks" / "inbox" / "_promoted_pending" / "source_001_plan_001.md")
    assert not (tmp_path / "tasks" / "inbox" / "source_001_plan_001.md").exists()


def test_active_inbox_activation_not_implemented(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    # promote should never emit an active-path write
    result = promote_bundle_to_pending_stub(bundle_dir, actor="alice")
    assert result["ok"] is True
    assert result["target_path"].replace("\\", "/").endswith("tasks/inbox/_promoted_pending/source_001_plan_001.md")
    assert not (tmp_path / "tasks" / "inbox" / "source_001_plan_001.md").exists()


def test_preflight_requires_allowed_target(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)
    _write_approval(bundle_dir, proposal_hash_stub(bundle_dir), idempotency_key="source_001:hash_001:0.1.0:plan")

    preflight = run_promotion_preflight_stub(bundle_dir, target_task_id="source_001_plan_001")
    assert preflight["ok"] is True
    assert preflight["target_path"].replace("\\", "/").endswith("tasks/inbox/_promoted_pending/source_001_plan_001.md")


def test_approve_does_not_promote(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    bundle_dir = _write_bundle(tmp_path)

    approved = approve_bundle_stub(bundle_dir, actor="alice", note="approve before promote")
    assert approved["ok"] is True
    assert approved["status"] == "approved"

    target_path = tmp_path / "tasks" / "inbox" / "_promoted_pending" / "source_001_plan_001.md"
    active_path = tmp_path / "tasks" / "inbox" / "source_001_plan_001.md"
    assert not target_path.exists()
    assert not active_path.exists()
