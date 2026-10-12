from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from solution_agent.continuation.activation import (
    activation_feature_status,
    activate_pending_to_inbox_stub,
    run_activation_preflight_contract_stub,
)


STATE_MACHINE_PATH = Path(".solution/continuation/activation_state_machine.yaml")
POLICY_PATH = Path(".solution/continuation/activation_policy.yaml")
PREFLIGHT_PATH = Path(".solution/continuation/activation_preflight_checklist.yaml")
AUDIT_PATH = Path(".solution/continuation/activation_audit_schema.yaml")


def _load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"{path} must contain YAML object"
    return payload


def _copy_activation_contracts(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / ".solution" / "continuation"
    target = tmp_path / ".solution" / "continuation"
    target.mkdir(parents=True, exist_ok=True)
    for src in source.glob("*.yaml"):
        (target / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _patch_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)
    _copy_activation_contracts(tmp_path)


def _bundle_hash(bundle_dir: Path) -> str:
    proposal_json = bundle_dir / "proposal.json"
    payload = json.loads(proposal_json.read_text(encoding="utf-8"))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _write_bundle_artifacts(
    bundle_dir: Path,
    *,
    source_task_id: str = "source_001",
    plan_id: str = "plan_001",
    bundle_id: str = "bundle_001",
    proposal_id: str = "proposal_001",
    source_result_hash: str = "hash_001",
    idempotency_key: str = "source_001:hash_001:0.1.0:plan",
    state: str = "promoted_pending",
    claim_limitation: str = "local-only activation candidate",
    forbidden_actions: list[str] | None = None,
) -> dict:
    bundle_dir.mkdir(parents=True, exist_ok=True)

    proposals = [{"forbidden_actions": forbidden_actions or []}]
    proposal_payload = {
        "bundle_id": bundle_id,
        "proposal_id": proposal_id,
        "source_task_id": source_task_id,
        "source_result_hash": source_result_hash,
        "plan_id": plan_id,
        "idempotency_key": idempotency_key,
        "proposal_count": 1,
        "proposals": proposals,
    }

    manifest_payload = {
        "manifest_id": f"{proposal_id}_manifest",
        "source_task_id": source_task_id,
        "source_result_hash": source_result_hash,
        "proposal_path": f"tasks/inbox/_proposed/{source_task_id}/{plan_id}/proposal.md",
        "plan_id": plan_id,
        "idempotency_key": idempotency_key,
        "proposal_count": 1,
        "claim_limitation": claim_limitation,
        "forbidden_actions": forbidden_actions or [],
        "outputs": ["action_requests.json"],
    }

    gate_payload = {
        "report_id": f"{proposal_id}__gate",
        "bundle_id": bundle_id,
        "source_task_id": source_task_id,
        "source_result_hash": source_result_hash,
        "idempotency_key": idempotency_key,
        "gate_status": "passed",
        "checks": [],
        "violations": [],
        "prompt_versions": {},
        "policy_version": "0.2.0",
    }

    (bundle_dir / "proposal.json").write_text(json.dumps(proposal_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (bundle_dir / "proposal_dry_run_manifest.json").write_text(json.dumps(manifest_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (bundle_dir / "gate_report.json").write_text(json.dumps(gate_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (bundle_dir / "promotion_state.json").write_text(
        json.dumps(
            {
                "state": state,
                "updated_at": "2026-05-18T00:00:00Z",
                "updated_by": "test",
                "reason": "fixture setup",
                "target_task_id": f"{source_task_id}_{plan_id}",
                "proposal_hash": _bundle_hash(bundle_dir),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_dir / "promotion_history.jsonl").write_text(
        '{"event":"promotion","proposal_hash":"'
        + _bundle_hash(bundle_dir)
        + '","target_task_id":"'
        + f"{source_task_id}_{plan_id}"
        + '"}\n',
        encoding="utf-8",
    )

    (bundle_dir / "proposal.md").write_text(
        "# Proposal\n\nContinuation candidate body.\n",
        encoding="utf-8",
    )

    return {
        "bundle_dir": bundle_dir,
        "source_task_id": source_task_id,
        "plan_id": plan_id,
        "idempotency_key": idempotency_key,
        "target_task_id": f"{source_task_id}_{plan_id}",
        "proposal_hash": _bundle_hash(bundle_dir),
        "proposal_id": proposal_id,
        "source_result_hash": source_result_hash,
        "bundle_id": bundle_id,
    }


def _write_activation_audit_entry(root: Path, target_task_id: str, proposal_hash: str, *, source_task_id: str = "source_001") -> None:
    audit_path = root / "continuation" / "audit" / "events.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if audit_path.exists():
        rows = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows.append(
        json.dumps(
            {
                "action": "promote",
                "target": target_task_id,
                "source_task_id": source_task_id,
                "inputs_hash": proposal_hash,
                "proposal_hash": proposal_hash,
                "idempotency_key": "id-001",
            },
            sort_keys=True,
        )
    )
    audit_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_pending_task(
    root: Path,
    *,
    source_task_id: str = "source_001",
    plan_id: str = "plan_001",
    bundle_id: str = "bundle_001",
    proposal_id: str = "proposal_001",
    source_result_hash: str = "hash_001",
    idempotency_key: str = "source_001:hash_001:0.1.0:plan",
    body: str = "Safe continuation body.",
    state: str = "promoted_pending",
    blocked_state: str | None = None,
) -> tuple[Path, dict]:
    bundle_dir = root / "tasks" / "inbox" / "_proposed" / source_task_id / plan_id
    meta = _write_bundle_artifacts(
        bundle_dir,
        source_task_id=source_task_id,
        plan_id=plan_id,
        bundle_id=bundle_id,
        proposal_id=proposal_id,
        source_result_hash=source_result_hash,
        idempotency_key=idempotency_key,
        state=blocked_state or state,
    )

    front = {
        "task_id": meta["target_task_id"],
        "source_task_id": source_task_id,
        "source_result_hash": source_result_hash,
        "proposal_id": proposal_id,
        "plan_id": plan_id,
        "bundle_id": bundle_id,
        "proposal_count": 1,
        "claim_limitation": "local-only activation candidate",
        "forbidden_actions": [],
        "outputs": ["action_requests.json"],
        "idempotency_key": idempotency_key,
        "activation_approved": True,
    }

    pending_root = root / "tasks" / "inbox" / "_promoted_pending"
    pending_root.mkdir(parents=True, exist_ok=True)
    pending_path = pending_root / f"{meta['target_task_id']}.md"
    pending_path.write_text(
        "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body + "\n",
        encoding="utf-8",
    )

    _write_activation_audit_entry(root, meta["target_task_id"], meta["proposal_hash"], source_task_id=source_task_id)
    return pending_path, meta


def _read_front_matter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    payload = yaml.safe_load(parts[1])
    return payload if isinstance(payload, dict) else {}


def test_activation_contracts_enabled_after_p0f_c(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)

    state_machine = _load_yaml(STATE_MACHINE_PATH)
    policy = _load_yaml(POLICY_PATH)
    preflight = _load_yaml(PREFLIGHT_PATH)
    audit = _load_yaml(AUDIT_PATH)

    assert state_machine["direct_file_edit_for_state_change"] == "forbidden"
    assert state_machine["transitions"]

    assert policy["active_inbox_activation"]["enabled"] is True
    assert policy["activation_is_not_dispatch"] is True
    assert policy["activation_requires_explicit_actor"] is True
    assert policy["explicit_activation_command_required"] is True

    separation = policy["separation_constraints"]
    assert separation["disallow_scheduler_dispatch"] is True
    assert separation["disallow_registry_mutation"] is True
    assert separation["disallow_remote_execution"] is True
    assert separation["disallow_active_inbox_write"] is False
    assert policy["scheduler_trigger"]["enabled"] is False
    assert policy["activation_target_root"] == "tasks/inbox/"

    check_ids = {item["id"] for item in preflight["checks"]}
    required_checks = {
        "pending_file_from_audited_promotion",
        "pending_file_under_promoted_pending",
        "promotion_history_exists",
        "promotion_audit_event_exists",
        "activation_or_promotion_approval_exists",
        "active_target_is_deterministic",
        "front_matter_required_fields",
        "no_source_edit_markers",
    }
    assert required_checks.issubset(check_ids)

    assert audit["side_effects"]["scheduler_dispatch"] is False
    assert audit["side_effects"]["registry_mutation"] is False
    assert audit["side_effects"]["remote_execution"] is False


def test_activation_feature_status_reports_explicit_local_write(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    status = activation_feature_status()
    assert status["status"] == "enabled"
    assert status["enabled"] is True
    assert status["active_inbox_write"] is True
    assert status["scheduler_dispatch"] is False
    assert status["registry_mutation"] is False
    assert status["remote_execution"] is False
    assert status["active_inbox_activation_enabled"] is True


def test_activate_without_pending_file_fails_closed(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    missing = tmp_path / "tasks" / "inbox" / "_promoted_pending" / "absent.md"
    result = activate_pending_to_inbox_stub(missing)
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["reason"] == "pending_file_missing"
    assert result["written"] is False
    assert not (tmp_path / "tasks" / "inbox").exists()


def test_activate_without_required_promotion_history_or_audit_fails_closed(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, meta = _write_pending_task(tmp_path)
    bundle_dir = Path(meta["bundle_dir"])

    (bundle_dir / "promotion_history.jsonl").unlink()
    preflight_no_history = run_activation_preflight_contract_stub(pending)
    assert preflight_no_history["ok"] is False
    assert any(
        check["id"] == "promotion_history_exists" and check["status"] == "failed"
        for check in preflight_no_history["checks"]
    )

    (bundle_dir / "promotion_history.jsonl").write_text(
        '{"event":"promotion","proposal_hash":"'
        + meta["proposal_hash"]
        + '","target_task_id":"'
        + meta["target_task_id"]
        + '"}\n',
        encoding="utf-8",
    )
    audit_path = tmp_path / "continuation" / "audit" / "events.jsonl"
    audit_path.write_text("", encoding="utf-8")

    preflight_no_audit = run_activation_preflight_contract_stub(pending)
    assert preflight_no_audit["ok"] is False
    assert any(
        check["id"] == "promotion_audit_event_exists" and check["status"] == "failed"
        for check in preflight_no_audit["checks"]
    )


@pytest.mark.parametrize("state", ["rejected", "revoked", "expired", "superseded", "logically_revoked"])
def test_activation_preflight_rejects_blocked_promotion_state(tmp_path, monkeypatch, state):
    _patch_workspace(tmp_path, monkeypatch)
    pending, meta = _write_pending_task(tmp_path, blocked_state=state)

    result = run_activation_preflight_contract_stub(pending)
    assert result["ok"] is False
    assert any(check["id"] == "blocked_states" and check["status"] == "failed" for check in result["checks"])


def test_activation_rejects_active_target_collision_with_different_content(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, meta = _write_pending_task(tmp_path)
    active_target = tmp_path / "tasks" / "inbox" / f"{meta['target_task_id']}.md"
    active_target.parent.mkdir(parents=True, exist_ok=True)
    active_target.write_text("existing active body", encoding="utf-8")
    before_hash = hashlib.sha256(active_target.read_text(encoding="utf-8").encode("utf-8")).hexdigest()

    result = activate_pending_to_inbox_stub(pending)
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["written"] is False
    assert result["action"] == "activation_failed"
    assert hashlib.sha256(active_target.read_text(encoding="utf-8").encode("utf-8")).hexdigest() == before_hash


def test_activation_rejects_same_idempotency_key_with_different_pending_content(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, meta = _write_pending_task(tmp_path)

    first = activate_pending_to_inbox_stub(pending)
    assert first["ok"] is True
    assert first["written"] is True
    assert first["idempotency_key"] == meta["idempotency_key"]

    active_target = tmp_path / "tasks" / "inbox" / f"{meta['target_task_id']}.md"
    before_active = active_target.read_text(encoding="utf-8")
    before_hash = hashlib.sha256(before_active.encode("utf-8")).hexdigest()

    pending.write_text(
        "---\n"
        + yaml.safe_dump(
            {
                "task_id": meta["target_task_id"],
                "source_task_id": meta["source_task_id"],
                "source_result_hash": meta["source_result_hash"],
                "proposal_id": meta["proposal_id"],
                "plan_id": "plan_001",
                "bundle_id": "bundle_001",
                "proposal_count": 1,
                "claim_limitation": "local-only activation candidate",
                "forbidden_actions": [],
                "outputs": ["action_requests.json"],
                "idempotency_key": meta["idempotency_key"],
                "activation_approved": True,
            }
        )
        + "---\nchanged active target safety test\n",
        encoding="utf-8",
    )
    second = activate_pending_to_inbox_stub(pending)
    assert second["ok"] is False
    assert second["status"] == "failed"
    assert second["written"] is False
    assert any(
        check["id"] == "idempotency_replay_or_reject" and check["status"] == "failed"
        for check in second["checks"]
    )

    after_hash = hashlib.sha256(active_target.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert after_hash == before_hash
    assert active_target.read_text(encoding="utf-8") == before_active


def test_activation_is_idempotent_for_same_idempotency_key_and_content(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, meta = _write_pending_task(tmp_path)

    first = activate_pending_to_inbox_stub(pending)
    assert first["ok"] is True
    assert first["status"] == "activated_in_inbox"
    assert first["written"] is True
    assert first["target_task_id"] == meta["target_task_id"]

    second = activate_pending_to_inbox_stub(pending)
    assert second["ok"] is True
    assert second["status"] == "activation_replayed"
    assert second["written"] is False
    assert second["action"] == "activate_pending_to_inbox_noop"
    assert second["target_task_id"] == meta["target_task_id"]

    inbox_files = list((tmp_path / "tasks" / "inbox").glob("*.md"))
    assert len(inbox_files) == 1


def test_activation_does_not_create_or_mutate_registry_file(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, _ = _write_pending_task(tmp_path)
    registry = tmp_path / "registry.json"
    if registry.exists():
        registry.unlink()

    result = activate_pending_to_inbox_stub(pending)
    assert result["ok"] is True
    assert not registry.exists()


def test_activation_success_writes_single_active_inbox_markdown(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, meta = _write_pending_task(tmp_path)

    result = activate_pending_to_inbox_stub(pending)
    assert result["ok"] is True
    assert result["written"] is True

    active_target = tmp_path / "tasks" / "inbox" / f"{meta['target_task_id']}.md"
    assert active_target.exists()
    assert active_target == Path(result["active_target"])
    assert len(list((tmp_path / "tasks" / "inbox").glob("*.md"))) == 1


def test_activation_writes_audit_and_history(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, meta = _write_pending_task(tmp_path)

    result = activate_pending_to_inbox_stub(pending)
    assert result["ok"] is True

    audit_path = tmp_path / "continuation" / "audit" / "events.jsonl"
    audit_rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(row.get("event_type") == "activated_in_inbox" for row in audit_rows)

    history_rows = [
        json.loads(line)
        for line in (Path(meta["bundle_dir"]) / "activation_history.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(row.get("event_type") == "activated_in_inbox" for row in history_rows)
    assert any(row.get("outcome") == "success" for row in history_rows if row.get("event_type") == "activated_in_inbox")


def test_activation_frontmatter_metadata_includes_safety_flags(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, meta = _write_pending_task(tmp_path)

    result = activate_pending_to_inbox_stub(pending)
    assert result["ok"] is True

    active_path = tmp_path / "tasks" / "inbox" / f"{meta['target_task_id']}.md"
    front = _read_front_matter(active_path.read_text(encoding="utf-8"))
    continuation = front["continuation"]
    assert continuation["source"] == "local_continuation_planner"
    assert continuation["activation_state"] == "activated_in_inbox"
    assert continuation["dispatch_allowed"] is False
    assert continuation["auto_dispatch_allowed"] is False
    assert continuation["registry_mutation_allowed"] is False
    assert continuation["remote_execution_allowed"] is False


def test_no_dispatch_or_scheduler_or_registry_side_effects_on_activation(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, _ = _write_pending_task(tmp_path)
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"tasks": {}}, sort_keys=True) + "\n", encoding="utf-8")
    before_registry = registry.read_text(encoding="utf-8")

    with (
        patch("subprocess.run") as run_mock,
        patch("subprocess.Popen") as popen_mock,
        patch("subprocess.check_output") as check_output_mock,
    ):
        result = activate_pending_to_inbox_stub(pending)
        assert result["ok"] is True
        assert run_mock.call_count == 0
        assert popen_mock.call_count == 0
        assert check_output_mock.call_count == 0

    assert registry.read_text(encoding="utf-8") == before_registry
    assert not (tmp_path / "continuation" / "scheduler_dispatch").exists()
    assert not (tmp_path / "scheduler" / "run_queue.json").exists()


def test_activation_without_no_network_subprocess_calls(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, _ = _write_pending_task(tmp_path)

    with (
        patch("subprocess.run") as run_mock,
        patch("subprocess.check_output") as check_output_mock,
        patch("subprocess.Popen") as popen_mock,
    ):
        result = activate_pending_to_inbox_stub(pending)
        assert result["ok"] is True
        assert run_mock.call_count == 0
        assert check_output_mock.call_count == 0
        assert popen_mock.call_count == 0


def test_activation_rejects_remote_path_probe_artifacts(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, meta = _write_pending_task(tmp_path, body="Remote candidate path /mnt/copy/ task")
    result = activate_pending_to_inbox_stub(pending)
    assert result["ok"] is False
    assert any(
        check["id"] == "no_guessed_remote_path_text" and check["status"] == "failed"
        for check in result["checks"]
    )
    assert not (tmp_path / "tasks" / "inbox" / f"{meta['target_task_id']}.md").exists()
