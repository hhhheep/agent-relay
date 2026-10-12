from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import patch

import yaml

from solution_agent.continuation.scheduler_explain import (
    explain_active_inbox_task,
    explain_night_auto_pending_scheduler_dry_run,
    parse_task_front_matter,
)


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)


def _write_active_inbox_task(
    root: Path,
    *,
    task_id: str,
    continuation: dict | None = None,
    body: str = "Stable continuation explanation fixture.",
    front_updates: dict | None = None,
    use_bom: bool = False,
    malformed_front: bool = False,
    missing_front: bool = False,
) -> Path:
    inbox = root / "tasks" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{task_id}.md"

    if missing_front:
        path.write_text(body or "No front matter", encoding="utf-8")
        return path

    front = {
        "task_id": task_id,
        "source_task_id": "source_001",
        "source_result_hash": "hash_001",
        "continuation": continuation or {},
        "claim_limitation": "intents only, no execution",
        "worker": "small_task_worker",
        "line": "A_continuation_line",
        "batch": "batch_continuation",
        "dag_root": "root_continuation",
        "prefix": "cont_",
        "resource_hint": "cpu",
        "title": f"Continuation {task_id}",
    }
    if front_updates:
        front.update(front_updates)

    front_text = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, default_flow_style=False)
    document = f"---\n{front_text}---\n{body}"
    if malformed_front:
        document = "---\n- not: [ valid\n\n"

    if use_bom:
        path.write_text(document, encoding="utf-8-sig")
    else:
        path.write_text(document, encoding="utf-8")
    return path


def _write_night_auto_pending_artifact(
    root: Path,
    *,
    mission_id: str = "B14",
    proposal_run_id: str = "proposal_run_001",
    candidate_id: str = "candidate_001",
    payload_updates: dict | None = None,
) -> Path:
    path = (
        root
        / "outputs"
        / "night_runs"
        / mission_id
        / "night_auto_pending"
        / proposal_run_id
        / candidate_id
        / "night_auto_pending_artifact.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "mission_id": mission_id,
        "proposal_run_id": proposal_run_id,
        "candidate_id": candidate_id,
        "scheduler_visible": False,
        "source_decision": {
            "decision_id": f"{mission_id}:root",
            "minimal_safe_action": "cpu_inventory",
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
    if payload_updates:
        payload.update(payload_updates)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def test_active_inbox_task_is_visible_in_p1_explain(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_visible",
        continuation={"dispatch_allowed": False, "scheduler_admission": {"status": "passed"}},
    )

    report = explain_active_inbox_task(path)

    assert report["task_id"] == "task_visible"
    assert report["visible_in_active_inbox"] is True
    assert report["eligible"] is False
    assert "dispatch_allowed_false" in report["reasons"]
    assert report["continuation_metadata"]["continuation"]["dispatch_allowed"] is False
    assert report["side_effects"] == {"dispatch": False, "registry_mutation": False, "remote_execution": False}


def test_night_auto_pending_scheduler_dry_run_writes_read_only_report(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    artifact_path = _write_night_auto_pending_artifact(tmp_path)
    before = artifact_path.read_text(encoding="utf-8")

    with (
        patch("subprocess.run") as run_mock,
        patch("subprocess.check_output") as check_output_mock,
        patch("subprocess.Popen") as popen_mock,
    ):
        report = explain_night_auto_pending_scheduler_dry_run(mission_id="B14")

    assert report["mission_id"] == "B14"
    assert report["status"] == "blocked"
    assert report["eligible"] is False
    assert report["counts"] == {
        "candidate_total": 1,
        "eligible_count": 0,
        "blocked_count": 1,
        "matched_count": 1,
    }
    assert report["candidate_reports"][0]["candidate_id"] == "candidate_001"
    assert "claim_limit_no_scheduler_dispatch" in report["reasons"]
    assert report["side_effects"] == {
        "scheduler_dispatch": False,
        "registry_mutation": False,
        "remote_api_call": False,
        "dataset_or_checkpoint_mutation": False,
        "active_inbox_write": False,
    }
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert artifact_path.read_text(encoding="utf-8") == before

    report_path = tmp_path / "outputs" / "night_runs" / "B14" / "scheduler_dry_run" / "night_auto_scheduler_explain_report.json"
    assert report_path.exists()
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["report_schema"] == "continuation_night_auto_scheduler_explain_v1"
    assert persisted["side_effects"]["scheduler_dispatch"] is False


def test_night_auto_scheduler_dry_run_surfaces_b14_l74_next_step_readiness_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    _write_night_auto_pending_artifact(
        tmp_path,
        payload_updates={
            "source_decision": {
                "decision_id": "B14:root",
                "minimal_safe_action": "cpu_inventory",
                "next_step_readiness": {
                    "schema_version": 1,
                    "can_stage": True,
                    "route": "BOUNDED_SELF_REPAIR",
                    "blockers": [],
                    "bounded_validation": {
                        "bucket_id": "B14-L74",
                        "status": "REPAIR_ONCE",
                        "next_recommendation": "REPAIR_ONCE",
                        "present_valid_artifact_ids": ["metrics", "bbox_predictions"],
                        "missing_artifact_ids": ["action_requests"],
                        "invalid_artifact_ids": ["closure_decision"],
                        "route_reasons": ["artifact_repair_once_possible"],
                        "artifact_statuses": {
                            "metrics": "VALID",
                            "closure_decision": "INVALID",
                        },
                    },
                },
            }
        },
    )

    report = explain_night_auto_pending_scheduler_dry_run(mission_id="B14")

    candidate_report = report["candidate_reports"][0]
    assert candidate_report["next_step_readiness"] == {
        "schema_version": 1,
        "can_stage": True,
        "route": "bounded_self_repair",
        "blockers": [],
        "bounded_validation": {
            "bucket_id": "B14-L74",
            "status": "repair_once",
            "next_recommendation": "repair_once",
            "present_valid_artifact_ids": ["metrics", "bbox_predictions"],
            "missing_artifact_ids": ["action_requests"],
            "invalid_artifact_ids": ["closure_decision"],
            "route_reasons": ["artifact_repair_once_possible"],
            "artifact_statuses": {
                "metrics": "valid",
                "closure_decision": "invalid",
            },
        },
    }
    assert candidate_report["bounded_validation_readiness"] == {
        "bucket_id": "B14-L74",
        "status": "repair_once",
        "next_recommendation": "repair_once",
        "present_valid_artifact_ids": ["metrics", "bbox_predictions"],
        "missing_artifact_ids": ["action_requests"],
        "invalid_artifact_ids": ["closure_decision"],
        "route_reasons": ["artifact_repair_once_possible"],
        "artifact_statuses": {
            "metrics": "valid",
            "closure_decision": "invalid",
        },
    }


def test_night_auto_scheduler_dry_run_requires_explicit_mission_scope(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    _write_night_auto_pending_artifact(tmp_path)

    report = explain_night_auto_pending_scheduler_dry_run()

    assert report["status"] == "blocked"
    assert report["eligible"] is False
    assert "missing_scope_mission_id" in report["reasons"]
    report_root = tmp_path / "outputs" / "night_runs" / "unknown_mission" / "scheduler_dry_run"
    assert not report_root.exists()


def test_night_auto_scheduler_dry_run_scope_filters_without_mutating_artifacts(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    artifact_path = _write_night_auto_pending_artifact(tmp_path)
    before = artifact_path.read_text(encoding="utf-8")

    report = explain_night_auto_pending_scheduler_dry_run(
        mission_id="B14",
        proposal_run_id="proposal_run_001",
        candidate_id="candidate_missing",
    )

    assert report["status"] == "blocked"
    assert report["eligible"] is False
    assert report["counts"]["matched_count"] == 0
    assert "requested_candidate_not_found" in report["reasons"]
    assert artifact_path.read_text(encoding="utf-8") == before


def test_visible_is_not_eligible_for_continuation_default_dispatch_block(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_default_blocked",
        continuation={"dispatch_allowed": False, "scheduler_admission": {"status": "passed"}},
    )
    report = explain_active_inbox_task(path)

    assert report["visible_in_active_inbox"] is True
    assert report["eligible"] is False
    assert "dispatch_allowed_false" in report["reasons"]


def test_dispatch_allowed_false_blocks_eligibility_even_with_scheduler_admission(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_dispatch_blocked",
        continuation={"dispatch_allowed": False, "scheduler_admission": {"status": "passed"}},
    )
    report = explain_active_inbox_task(path)
    assert "dispatch_allowed_false" in report["reasons"]
    assert report["eligible"] is False


def test_missing_scheduler_admission_blocks_eligibility(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_no_admission",
        continuation={"dispatch_allowed": True},
    )
    report = explain_active_inbox_task(path)

    assert report["visible_in_active_inbox"] is True
    assert report["eligible"] is False
    assert "scheduler_admission_missing" in report["reasons"]


def test_revoked_or_superseded_continuation_blocked(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    for state in ["revoked", "superseded", "logically_revoked"]:
        path = _write_active_inbox_task(
            tmp_path,
            task_id=f"task_{state}",
            continuation={"dispatch_allowed": True, "scheduler_admission": {"status": "passed"}, "activation_state": state},
        )
        report = explain_active_inbox_task(path)
        assert report["eligible"] is False
        assert f"continuation_{state}" in report["reasons"]


def test_selector_fields_reported_for_worker_line_batch_and_resources(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    task_id = "task_selector_fields"
    path = _write_active_inbox_task(
        tmp_path,
        task_id=task_id,
        continuation={"dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
        front_updates={
            "worker": "materialized_execution",
            "line": "line_selector",
            "batch": "batch_selector",
            "dag_root": "dag_root_selector",
            "prefix": "sel_",
            "resource_hint": "gpu",
        },
        body="No dispatch text.",
    )
    report = explain_active_inbox_task(path)

    assert report["selector"]["task"]["worker"] == "materialized_execution"
    assert report["selector"]["task"]["line"] == "line_selector"
    assert report["selector"]["task"]["batch"] == "batch_selector"
    assert report["selector"]["task"]["dag_root"] == "dag_root_selector"
    assert report["selector"]["task"]["prefix"] == "sel_"
    assert report["selector"]["resource_lane"] == "gpu"
    assert report["selector"]["resource_hint"] == "gpu"


def test_requested_worker_is_filter_only_and_never_overrides(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_worker_filter",
        continuation={"dispatch_allowed": False, "scheduler_admission": {"status": "passed"}},
    )
    report = explain_active_inbox_task(path, requested_worker="materialized_execution")

    assert report["selector"]["request"]["worker"] == "materialized_execution"
    assert "worker_scope_mismatch" in report["reasons"]
    assert report["selector"]["task"]["worker"] == "small_task_worker"
    assert report["eligible"] is False


def test_scope_mismatch_is_ineligible_and_read_only(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_scope_mismatch",
        continuation={"dispatch_allowed": False, "scheduler_admission": {"status": "passed"}},
        front_updates={"line": "expected_line", "batch": "expected_batch"},
    )
    before = path.read_text(encoding="utf-8")
    report = explain_active_inbox_task(path, scope={"line": "wrong_line", "batch": "expected_batch"})

    assert report["eligible"] is False
    assert report["visible_in_active_inbox"] is True
    assert "line_scope_mismatch" in report["reasons"]
    assert path.read_text(encoding="utf-8") == before


def test_non_continuation_active_task_is_marked_manual_review(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_non_continuation",
        continuation=None,
        front_updates={"continuation": None, "dispatch_allowed": False, "scheduler_admission": {"status": "passed"}},
    )
    report = explain_active_inbox_task(path)

    assert report["visible_in_active_inbox"] is True
    assert report["eligible"] is False
    assert report["continuation_metadata"]["continuation"] == {}
    assert "non_continuation_task" in report["reasons"]
    assert "manual_review_required" in report["reasons"]


def test_no_dispatch_or_registry_or_remote_side_effects(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_no_side_effects",
        continuation={"dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
    )
    registry = tmp_path / "registry.json"
    registry_payload = {"tasks": {"task_no_side_effects": {"status": "pending"}}}
    registry.write_text(yaml.safe_dump(registry_payload, sort_keys=True), encoding="utf-8")
    before = registry.read_text(encoding="utf-8")
    before_task = path.read_text(encoding="utf-8")

    with (
        patch("subprocess.run") as run_mock,
        patch("subprocess.check_output") as check_output_mock,
        patch("subprocess.Popen") as popen_mock,
    ):
        report = explain_active_inbox_task(path)

    assert report["side_effects"] == {"dispatch": False, "registry_mutation": False, "remote_execution": False}
    assert report["eligible"] is False
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert registry.read_text(encoding="utf-8") == before
    assert path.read_text(encoding="utf-8") == before_task


def test_dispatch_request_phrase_is_reported_and_never_executed(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_dispatch_request",
        continuation={"dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
        body="Please dispatch this continuation now.",
    )
    report = explain_active_inbox_task(path)

    assert "dispatch_language_detected" in report["reasons"]
    assert report["eligible"] is False


def test_negated_dispatch_phrase_does_not_enable_execution(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_dispatch_negated",
        continuation={"dispatch_allowed": True, "scheduler_admission": {"status": "passed"}},
        body="Please do not dispatch this task automatically.",
    )
    report = explain_active_inbox_task(path)

    assert "dispatch_language_negated" in report["reasons"]
    assert report["eligible"] is False


def test_missing_front_matter_in_active_inbox_is_manual_review(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_missing_front",
        missing_front=True,
        body="Plain body only",
    )
    before = path.read_text(encoding="utf-8")

    report = explain_active_inbox_task(path)

    assert report["visible_in_active_inbox"] is True
    assert report["eligible"] is False
    assert "front_matter_missing" in report["reasons"]
    assert "manual_review_required" in report["reasons"]
    assert path.read_text(encoding="utf-8") == before


def test_malformed_yaml_front_matter_is_reported_and_read_only(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_malformed_front",
        malformed_front=True,
        front_updates={"continuation": {"dispatch_allowed": True, "scheduler_admission": {"status": "passed"}}},
    )
    before = path.read_text(encoding="utf-8")

    report = explain_active_inbox_task(path)

    assert report["visible_in_active_inbox"] is True
    assert report["eligible"] is False
    assert "front_matter_malformed" in report["reasons"]
    assert "manual_review_required" in report["reasons"]
    assert path.read_text(encoding="utf-8") == before


def test_bom_front_matter_is_parsed_without_mutation(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    path = _write_active_inbox_task(
        tmp_path,
        task_id="task_bom",
        continuation={"dispatch_allowed": False, "scheduler_admission": {"status": "passed"}},
        use_bom=True,
        front_updates={"worker": "codex"},
    )
    report = explain_active_inbox_task(path)

    assert report["task_id"] == "task_bom"
    assert report["selector"]["task"]["worker"] == "codex"
    assert report["visible_in_active_inbox"] is True


def test_parse_task_front_matter_handles_bom_and_malformed():
    bom_text = (
        "\ufeff---\n"
        "task_id: parse_bom\n"
        "continuation:\n  dispatch_allowed: true\n"
        "---\n"
        "body text\n"
    )
    front = parse_task_front_matter(bom_text)
    assert front["task_id"] == "parse_bom"
    assert front["continuation"]["dispatch_allowed"] is True

    assert parse_task_front_matter("no front matter only body") == {}
    assert parse_task_front_matter("---\n: bad\n---\n") == {}
