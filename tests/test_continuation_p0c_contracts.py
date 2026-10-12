from __future__ import annotations

import re
from pathlib import Path

import yaml


POLICY_PATH = Path(".solution/continuation/p0c_policy.yaml")
PLAN_PATH = Path("docs/CONTINUATION_PLANNER_P0C_PROPOSED_STAGING_PLAN.md")


def _load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"{path} should contain a YAML object"
    return payload


def _normalize_roots(values):
    return [str(value).strip().replace("\\", "/").rstrip("/") + "/" for value in values]


def test_p0c_policy_exists_and_internal_fields():
    policy = _load_yaml(POLICY_PATH)

    assert policy["schema_version"] == 1
    assert policy["policy_version"] == "0.1.0"
    assert policy["policy_id"] == "p0c_proposed_staging"
    assert policy["status"] == "planning"
    assert policy["promotion_forbidden"] is True
    assert policy["dispatch_forbidden"] is True
    assert policy["registry_mutation_forbidden"] is True

    required_keys = {
        "allowed_write_roots",
        "forbidden_write_roots",
        "required_gate_checks",
        "required_draft_fields",
        "required_audit_fields",
        "idempotency_collision_rules",
        "scheduler_dry_run_requirements",
    }
    assert required_keys.issubset(policy)
    assert isinstance(policy["required_gate_checks"], list) and policy["required_gate_checks"]
    assert isinstance(policy["required_audit_fields"], list) and policy["required_audit_fields"]
    assert isinstance(policy["idempotency_collision_rules"], dict) and policy["idempotency_collision_rules"]


def test_p0c_policy_write_roots_allow_only_proposed_and_forbid_active_inbox_registry():
    policy = _load_yaml(POLICY_PATH)

    allowed = set(_normalize_roots(policy["allowed_write_roots"]))
    forbidden = set(_normalize_roots(policy["forbidden_write_roots"]))

    assert "tasks/inbox/_proposed/" in allowed
    assert "tasks/inbox/" in forbidden
    assert all(
        root == "tasks/inbox/_proposed/" or not root.startswith("tasks/inbox/")
        for root in allowed
    ), f"unexpected inbox root in allowed list: {sorted(allowed)}"
    assert any(root in forbidden for root in {"registry.json/", "registry/", ".solution/task_registry.yaml/"})
    assert "tasks/inbox/" not in allowed
    assert "tasks/inbox/*/" in forbidden
    assert allowed.isdisjoint(forbidden)


def test_p0c_required_gates_are_complete_and_required():
    policy = _load_yaml(POLICY_PATH)
    required = {
        "p0b_gate",
        "static_draft_gate",
        "inbox_metabolizer_dry_run",
        "scheduler_dry_run_no_dispatch",
    }
    observed = {item["name"] for item in policy["required_gate_checks"]}
    assert required.issubset(observed)
    for name in required:
        item = next(item for item in policy["required_gate_checks"] if item["name"] == name)
        assert item["required"] is True


def test_p0c_required_draft_fields_include_mandatory_contract_fields():
    policy = _load_yaml(POLICY_PATH)
    required = {
        "source_task_id",
        "source_result_hash",
        "claim_limitation",
        "forbidden_actions",
        "expected_outputs",
    }
    observed = set(policy["required_draft_fields"])
    assert required.issubset(observed)


def test_p0c_required_audit_fields_include_minimum_observability_contract():
    policy = _load_yaml(POLICY_PATH)
    required = {
        "event_id",
        "timestamp",
        "actor",
        "action",
        "target",
        "idempotency_key",
        "policy_version",
        "inputs_hash",
        "outputs_hash",
        "prompt_versions",
    }
    observed = set(policy["required_audit_fields"])
    assert required.issubset(observed)


def test_p0c_idempotency_collision_rules_define_safe_rollback():
    policy = _load_yaml(POLICY_PATH)
    rules = policy["idempotency_collision_rules"]
    assert rules["same_idempotency_key_exists"] == "skip_and_log"
    assert rules["same_idempotency_key_diff_hash"] in {"collision_fail_stop", "collision_no_overwrite"}
    assert rules["same_source_task_different_hash"] in {
        "write_with_new_hash_if_diff_payload",
        "overwrite_and_log_diff",
    }
    assert rules["same_source_task_same_hash_different_prompt_version"] in {"warn_and_require_human", "warn_and_abort"}


def _assert_heading_present(path: Path, heading: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert re.search(rf"^##\s*{re.escape(heading)}\s*$", text, re.MULTILINE | re.IGNORECASE), (
        f"missing heading: {heading}"
    )


def test_p0c_plan_document_includes_required_sections():
    required_headings = [
        "Scope",
        "Non-Goals",
        "Target Directory Layout (`tasks/inbox/_proposed/`)",
        "P0b -> P0c Flow",
        "State Machine Changes",
        "Gate Checklist",
        "Inbox Metabolizer Compatibility Requirements",
        "Scheduler Dry-Run Requirements",
        "Audit and Idempotency Rules",
        "Failure Rollback Behavior",
        "Human Approval Requirements",
        "Threat Model",
        "Pressure Fixtures to Add",
        "P0c Acceptance Criteria",
        "P0d Boundaries",
    ]

    for heading in required_headings:
        _assert_heading_present(PLAN_PATH, heading)


def test_p0c_plan_documents_allowed_roots_and_forbidden_actions():
    text = PLAN_PATH.read_text(encoding="utf-8")
    assert "tasks/inbox/_proposed/" in text
    assert "tasks/inbox/" in text
    assert "Auto-promotion" in text
    assert "active `tasks/inbox`" in text or "active inbox" in text
    assert "dispatch" in text.lower()
    assert "registry" in text.lower()
    assert "threat model" in text.lower()
