from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from solution_agent.cli import cli
from solution_agent.tools.patch_tools import PatchIntentResult
from solution_agent.continuation.self_repair_rehearsal import (
    run_b14_l72_self_repair_rehearsal,
    run_self_repair_rehearsal,
    build_proposal_candidates_from_rehearsal_summary,
)


def _with_known_planner_policy(monkeypatch) -> dict[str, object]:
    policy = {
        "schema_version": 1,
        "allowed_next_step_kinds": [
            "bounded_probe",
            "inventory_audit",
            "decision_synthesis",
            "metric_completion",
            "missing_capability_request",
            "codex_debug_request",
            "no_safe_continuation",
            "decision_synthesis",
        ],
        "max_tasks_per_source_result": 3,
        "forbidden_actions": [
            "write_inbox",
            "promote",
            "dispatch",
            "scheduler_submit",
            "registry_mutation",
            "remote_execution",
            "source_edit",
            "path_guessing",
            "remote_write",
            "subprocess",
            "active_inbox_write",
            "default_forbidden",
        ],
    }
    monkeypatch.setattr("solution_agent.continuation.knowledge_to_proposal._policy", lambda: policy)
    return policy


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")



def _write_repair_target_anchor(
    tmp_path: Path,
    target_file: str,
    action_lines: list[str],
) -> Path:
    path = tmp_path / target_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(action_lines) + "\n", encoding="utf-8")
    return path

def _make_b14_l72_partial(tmp_path: Path) -> Path:
    artifacts = tmp_path / "results" / "b14_l72_large_image_bbox_sidecar_smoke" / "latest" / "artifacts"
    _write_json(
        artifacts / "decision_summary.json",
        {
            "status": "partial",
            "reason": "missing_capability",
            "next_action": "complete_l71_or_add_dedicated_allowlisted_smoke_runner",
            "action_requests": [
                {
                    "kind": "missing_capability",
                    "message": "Missing reusable utils/b14_bbox_sidecar.py sidecar module from L71.",
                    "requested_action": "Complete L71 sidecar integration or provide a dedicated allowlisted smoke runner before claiming L72 metrics.",
                },
                {
                    "kind": "missing_capability",
                    "message": "Missing optional --enable-bbox-sidecar CLI flag from L71.",
                    "requested_action": "Complete L71 sidecar integration or provide a dedicated allowlisted smoke runner before claiming L72 metrics.",
                },
            ],
        },
    )
    (artifacts / "artifact_summary.md").write_text(
        "L72 produced a capability-aware partial smoke result; no bbox metric claims are allowed.\n",
        encoding="utf-8",
    )
    return artifacts


def _make_b14_l72_ready(tmp_path: Path) -> Path:
    artifacts = tmp_path / "results" / "b14_l72_large_image_bbox_sidecar_smoke" / "latest" / "artifacts"
    _write_json(
        artifacts / "action_requests.json",
        [
            {
                "kind": "missing_capability",
                "message": "Need to patch tasks/inbox/_proposed/repro_repair_seed.py after recoverable parsing drift.",
                "requested_action": "Apply a bounded self-repair patch to restore deterministic sidecar helper behavior for B14 L72.",
            },
        ],
    )
    _write_repair_target_anchor(
        tmp_path,
        "tasks/inbox/_proposed/repro_repair_seed.py",
        [
            "missing_capability",
            "Need to patch tasks/inbox/_proposed/repro_repair_seed.py after recoverable parsing drift.",
            "Apply a bounded self-repair patch to restore deterministic sidecar helper behavior for B14 L72.",
            "",
            "# existing recovery markdown placeholder",
        ],
    )
    _write_json(
        artifacts / "decision_summary.json",
        {
            "status": "pass",
            "reason": "source_repair_ready",
            "next_action": "replay_smoke_with_deterministic_sidecar_helper",
        },
    )
    (artifacts / "artifact_summary.md").write_text(
        "B14 L72 readiness fixture uses explicit source-repair action and resolved summary metadata for replay-safe rehearsal.\n",
        encoding="utf-8",
    )
    return artifacts


def test_b14_l72_rehearsal_emits_plan_decision_repair_result_and_rerun_intent(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    artifacts = _make_b14_l72_partial(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "b14_l72"

    result = run_b14_l72_self_repair_rehearsal(result_dir=artifacts, output_dir=output_dir)

    assert result["status"] == "written"
    summary = result["summary"]
    assert summary["needs_source_repair"] is True
    assert summary["source_task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    assert summary["downstream_task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    assert summary["patch_applied"] is False
    assert summary["source_edited"] is False
    assert summary["dispatch_performed"] is False
    assert summary["remote_execution_performed"] is False

    decision = json.loads(Path(summary["artifacts"]["repair_executor_decision"]).read_text(encoding="utf-8"))
    repair_result = json.loads(Path(summary["artifacts"]["repair_result"]).read_text(encoding="utf-8"))
    rerun_intent = json.loads(Path(summary["artifacts"]["rerun_intent"]).read_text(encoding="utf-8"))

    assert summary["patch_intent_validation_errors"] == decision["patch_intent_validation_errors"]
    assert repair_result["patch_intent_validation_errors"] == decision["patch_intent_validation_errors"]
    assert decision["decision"]["route"] in {"codex", "bounded_self_repair", "deterministic_self", "human", "stop"}
    if decision["decision"]["route"] in {"bounded_self_repair", "deterministic_self"}:
        assert summary["patch_intent_generated"] is True
    else:
        assert summary["patch_intent_generated"] is False
        assert summary["next_recommendation"] == "route_to_codex_or_human_before_rerun"
    assert summary["apply_readiness"]["route"] == decision["decision"]["route"]
    assert summary["apply_readiness"]["can_apply"] is (decision["decision"]["route"] in {"bounded_self_repair", "deterministic_self"})
    assert summary["apply_readiness"]["status"] == ("ready" if decision["decision"]["route"] in {"bounded_self_repair", "deterministic_self"} else "blocked")
    assert summary["apply_readiness"]["blockers"] == summary["blocked_by"]
    assert repair_result["repair_id"] == decision["repair_id"]
    assert rerun_intent["repair_id"] == decision["repair_id"]
    assert rerun_intent["downstream_task_id"] == "b14_l72_large_image_bbox_sidecar_smoke"
    assert not (tmp_path / "tasks" / "inbox").exists()


def test_source_repair_rehearse_cli_writes_local_rehearsal_artifacts(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    artifacts = _make_b14_l72_partial(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "cli_b14_l72"
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "source-repair",
            "rehearse",
            "--result-dir",
            str(artifacts),
            "--output-dir",
            str(output_dir),
            "--source-task-id",
            "b14_l72_large_image_bbox_sidecar_smoke",
            "--downstream-task-id",
            "b14_l72_large_image_bbox_sidecar_smoke",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["status"] == "completed"
    assert Path(payload["summary"]["artifacts"]["repair_result"]).exists()
    assert Path(payload["summary"]["artifacts"]["rerun_intent"]).exists()
    assert not (tmp_path / "tasks" / "inbox").exists()


def _make_self_repair_safe_patch_artifact(tmp_path: Path) -> Path:
    artifacts = tmp_path / "results" / "safe_patch_task" / "latest" / "artifacts"
    action_requests = [
        {
            "kind": "missing_capability",
            "message": "Need to patch tasks/inbox/_proposed/repro_repair_seed.py after recoverable parsing drift.",
            "requested_action": "Insert a compatibility shim in the proposed task markdown path before continuing.",
        },
    ]
    _write_json(artifacts / "action_requests.json", action_requests)
    _write_repair_target_anchor(
        tmp_path,
        "tasks/inbox/_proposed/repro_repair_seed.py",
        [
            "missing_capability",
            "Need to patch tasks/inbox/_proposed/repro_repair_seed.py after recoverable parsing drift.",
            "Insert a compatibility shim in the proposed task markdown path before continuing.",
            "",
            "# existing compatibility shim placeholder",
        ],
    )
    _write_json(
        artifacts / "decision_summary.json",
        {
            "status": "partial",
            "reason": "missing_capability",
            "next_action": "repair_inbox_candidate",
        },
    )
    return artifacts


def test_self_repair_rehearsal_marks_apply_ready_for_deterministic_route(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    artifacts = _make_self_repair_safe_patch_artifact(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "safe_route"

    result = run_self_repair_rehearsal(
        result_dir=artifacts,
        output_dir=output_dir,
        source_task_id="safe_patch_task",
        downstream_task_id="safe_patch_task",
        repair_type="closure_artifact_repair",
        available_tests=["action_request_schema_test", "decision_json_schema_test", "continuation_bundle_gate_test"],
    )

    summary = result["summary"]
    assert summary["source_task_id"] == "safe_patch_task"
    assert summary["can_self_repair"] is True
    assert summary["executor_route"] == "deterministic_self"
    assert summary["apply_readiness"]["route"] == "deterministic_self"
    assert summary["apply_readiness"]["status"] == "ready"
    assert summary["apply_readiness"]["can_apply"] is True
    assert summary["apply_readiness"]["blockers"] == []
    assert summary["patch_applied"] is True
    assert summary["source_edited"] is True
    repair_result = json.loads(Path(summary["artifacts"]["repair_result"]).read_text(encoding="utf-8"))
    assert repair_result["execution_status"] == "applied"
    assert repair_result["execution_blockers"] == []
    assert repair_result["execution_side_effects"]["source_edited"] is True
    assert "### repair placeholder for deterministic_self" in Path(tmp_path / "tasks/inbox/_proposed/repro_repair_seed.py").read_text(encoding="utf-8")


def test_self_repair_rehearsal_normalizes_mixed_case_route_for_controlled_apply(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    artifacts = _make_self_repair_safe_patch_artifact(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "mixed_case_controlled_apply"
    from solution_agent.continuation import self_repair_rehearsal

    original_selector = self_repair_rehearsal.select_repair_executor

    def _select_with_mixed_case_route(*args, **kwargs):
        decision = original_selector(*args, **kwargs)
        decision["decision"]["route"] = "BOUNDED_SELF_REPAIR"
        return decision

    monkeypatch.setattr(self_repair_rehearsal, "select_repair_executor", _select_with_mixed_case_route)

    result = self_repair_rehearsal.run_self_repair_rehearsal(
        result_dir=artifacts,
        output_dir=output_dir,
        source_task_id="safe_patch_task",
        downstream_task_id="safe_patch_task",
        repair_type="closure_artifact_repair",
        available_tests=["action_request_schema_test", "decision_json_schema_test", "continuation_bundle_gate_test"],
    )

    summary = result["summary"]
    assert summary["source_task_id"] == "safe_patch_task"
    assert summary["executor_route"] == "bounded_self_repair"
    assert summary["can_self_repair"] is True, summary["blocked_by"]
    assert summary["apply_readiness"]["route"] == "bounded_self_repair"
    assert summary["apply_readiness"]["status"] == "ready"
    assert summary["apply_readiness"]["can_apply"] is True
    assert summary["patch_applied"] is True
    assert summary["source_edited"] is True
    repair_result = json.loads(Path(summary["artifacts"]["repair_result"]).read_text(encoding="utf-8"))
    assert repair_result["execution_status"] == "applied"
    assert repair_result["execution_side_effects"]["source_edited"] is True


def test_self_repair_rehearsal_can_build_proposal_candidates_blocked_readiness(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    _with_known_planner_policy(monkeypatch)
    artifacts = _make_b14_l72_partial(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "b14_l72"
    result = run_b14_l72_self_repair_rehearsal(result_dir=artifacts, output_dir=output_dir)
    knowledge_subset = {
        "source_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
        "line_filter": "L1",
        "workplace_profile_filter": "line-a",
    }

    candidates = build_proposal_candidates_from_rehearsal_summary(
        result["summary"],
        knowledge_subset,
        actor="system",
    )

    assert candidates["candidate_count"] == 1
    candidate = candidates["candidates"][0]
    assert candidate["status"] == "proposal_candidate_created"
    assert candidate["kind"] == "bounded_probe"
    assert candidate["next_step_readiness"]["can_stage"] is False
    assert "self_repair_apply_readiness_blocked" in candidate["next_step_readiness"]["blockers"]
    assert candidate["apply_readiness"]["status"] == "blocked"
    repair_result = json.loads(Path(result["summary"]["artifacts"]["repair_result"]).read_text(encoding="utf-8"))
    rerun_intent = json.loads(Path(result["summary"]["artifacts"]["rerun_intent"]).read_text(encoding="utf-8"))

    assert candidate["repair_result"] == repair_result
    assert candidate["rerun_intent"] == rerun_intent


def test_b14_l72_rehearsal_readiness_status_matrix(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    _with_known_planner_policy(monkeypatch)
    artifacts = _make_b14_l72_partial(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "b14_l72_readiness_matrix"
    result = run_b14_l72_self_repair_rehearsal(result_dir=artifacts, output_dir=output_dir)

    base_summary = result["summary"]
    knowledge_subset = {
        "source_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
        "line_filter": "L1",
        "workplace_profile_filter": "line-a",
    }

    cases = [
        (
            "baseline_blocked",
            {"status": base_summary["apply_readiness"]["status"], "can_apply": False, "blockers": []},
            {"status_reason": "self_repair_apply_readiness_blocked"},
        ),
        (
            "explicit_partial",
            {"status": "partial", "can_apply": False, "blockers": []},
            {"status_reason": "self_repair_apply_readiness_partial"},
        ),
        (
            "ready_with_blocker",
            {"status": "ready", "can_apply": False, "blockers": ["repair_plan_validation_error:partial_schema"]},
            {"payload_blocker": "repair_plan_validation_error:partial_schema"},
        ),
    ]

    for _, payload, expectations in cases:
        summary = dict(base_summary)
        summary["apply_readiness"] = dict(base_summary["apply_readiness"])
        summary["apply_readiness"].update(payload)
        summary["blocked_by"] = summary["apply_readiness"]["blockers"]

        candidates = build_proposal_candidates_from_rehearsal_summary(
            summary,
            knowledge_subset,
            actor="system",
        )
        candidate = candidates["candidates"][0]
        assert candidates["candidate_count"] == 1
        assert candidate["status"] == "proposal_candidate_created"
        assert candidate["kind"] == "bounded_probe"
        assert candidate["next_step_readiness"]["can_stage"] is False
    assert candidate["apply_readiness"]["status"] == payload["status"]
    if expectations.get("status_reason"):
        assert expectations["status_reason"] in candidate["next_step_readiness"]["blockers"]
    if expectations.get("status_reason") == "self_repair_apply_readiness_partial":
        assert "self_repair_apply_readiness_partial" in candidate["next_step_readiness"]["blockers"]
    if expectations.get("payload_blocker"):
        assert "repair_plan_validation_error:partial_schema" in candidate["next_step_readiness"]["blockers"]


def test_rehearsal_apply_readiness_status_normalized_case(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    _with_known_planner_policy(monkeypatch)
    artifacts = _make_b14_l72_partial(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "mixed_case_readiness"
    result = run_b14_l72_self_repair_rehearsal(result_dir=artifacts, output_dir=output_dir)

    summary = dict(result["summary"])
    summary["apply_readiness"] = {
        "status": "BLOCKED",
        "can_apply": True,
        "blockers": [],
        "route": summary["executor_route"],
    }
    summary["blocked_by"] = summary["apply_readiness"]["blockers"]

    candidates = build_proposal_candidates_from_rehearsal_summary(
        summary,
        {
            "source_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "line_filter": "L1",
            "workplace_profile_filter": "line-a",
        },
        actor="system",
    )

    assert candidates["candidate_count"] == 1
    candidate = candidates["candidates"][0]
    assert candidate["apply_readiness"]["status"] == "blocked"
    assert candidate["next_step_readiness"]["can_stage"] is False
    assert "self_repair_apply_readiness_blocked" in candidate["next_step_readiness"]["blockers"]


def test_rehearsal_apply_readiness_route_is_normalized_case(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    _with_known_planner_policy(monkeypatch)
    artifacts = _make_b14_l72_partial(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "mixed_case_route"
    result = run_b14_l72_self_repair_rehearsal(result_dir=artifacts, output_dir=output_dir)

    summary = dict(result["summary"])
    summary["apply_readiness"] = {
        "status": "ready",
        "can_apply": True,
        "blockers": [],
        "route": "BOUNDED_SELF_REPAIR",
    }
    summary["blocked_by"] = summary["apply_readiness"]["blockers"]

    candidates = build_proposal_candidates_from_rehearsal_summary(
        summary,
        {
            "source_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "line_filter": "L1",
            "workplace_profile_filter": "line-a",
        },
        actor="system",
    )

    assert candidates["candidate_count"] == 1
    candidate = candidates["candidates"][0]
    assert candidate["apply_readiness"]["route"] == "bounded_self_repair"
    assert candidate["next_step_readiness"]["route"] == "bounded_self_repair"

def test_b14_l72_rehearsal_can_build_proposal_candidates_ready_state(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    _with_known_planner_policy(monkeypatch)
    artifacts = _make_b14_l72_ready(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "b14_l72_ready"

    result = run_self_repair_rehearsal(
        result_dir=artifacts,
        output_dir=output_dir,
        source_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        downstream_task_id="b14_l72_large_image_bbox_sidecar_smoke",
        repair_type="closure_artifact_repair",
        available_tests=["action_request_schema_test", "decision_json_schema_test", "continuation_bundle_gate_test"],
    )
    candidates = build_proposal_candidates_from_rehearsal_summary(
        result["summary"],
        {
            "source_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "line_filter": "L1",
            "workplace_profile_filter": "line-a",
        },
        actor="system",
    )

    assert candidates["candidate_count"] == 1
    candidate = candidates["candidates"][0]
    assert candidate["status"] == "proposal_candidate_created"
    assert candidate["next_step_readiness"]["can_stage"] is True
    assert candidate["apply_readiness"]["status"] == "ready"
    assert candidate["apply_readiness"]["can_apply"] is True
    assert candidate["next_step_readiness"]["blockers"] == []
    repair_result = json.loads(Path(result["summary"]["artifacts"]["repair_result"]).read_text(encoding="utf-8"))
    rerun_intent = json.loads(Path(result["summary"]["artifacts"]["rerun_intent"]).read_text(encoding="utf-8"))
    assert candidate["repair_result"] == repair_result
    assert candidate["rerun_intent"] == rerun_intent


def test_self_repair_rehearsal_rejects_invalid_apply_readiness_payload(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    _with_known_planner_policy(monkeypatch)
    artifacts = _make_b14_l72_partial(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "b14_l72_invalid_readiness"
    result = run_b14_l72_self_repair_rehearsal(result_dir=artifacts, output_dir=output_dir)

    summary = dict(result["summary"])
    summary["apply_readiness"] = "ready"
    candidates = build_proposal_candidates_from_rehearsal_summary(
        summary,
        {
            "source_task_id": "b14_l72_large_image_bbox_sidecar_smoke",
            "line_filter": "L1",
            "workplace_profile_filter": "line-a",
        },
        actor="system",
    )

    assert candidates["candidate_count"] == 1
    candidate = candidates["candidates"][0]
    assert candidate["next_step_readiness"]["can_stage"] is False
    assert "self_repair_apply_readiness_invalid" in candidate["next_step_readiness"]["blockers"]
    assert candidate["apply_readiness"] == {}


def test_self_repair_rehearsal_can_build_proposal_candidates_ready_readiness(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    _with_known_planner_policy(monkeypatch)
    artifacts = _make_self_repair_safe_patch_artifact(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "safe_route"
    result = run_self_repair_rehearsal(
        result_dir=artifacts,
        output_dir=output_dir,
        source_task_id="safe_patch_task",
        downstream_task_id="safe_patch_task",
        repair_type="closure_artifact_repair",
        available_tests=["action_request_schema_test", "decision_json_schema_test", "continuation_bundle_gate_test"],
    )

    candidates = build_proposal_candidates_from_rehearsal_summary(
        result["summary"],
        {
            "source_task_id": "safe_patch_task",
            "line_filter": "L1",
            "workplace_profile_filter": "line-a",
        },
        actor="system",
    )

    assert candidates["candidate_count"] == 1
    candidate = candidates["candidates"][0]
    assert candidate["status"] == "proposal_candidate_created"
    assert candidate["next_step_readiness"]["can_stage"] is True
    assert candidate["apply_readiness"]["status"] == "ready"
    repair_result = json.loads(Path(result["summary"]["artifacts"]["repair_result"]).read_text(encoding="utf-8"))
    rerun_intent = json.loads(Path(result["summary"]["artifacts"]["rerun_intent"]).read_text(encoding="utf-8"))

    assert candidate["repair_result"] == repair_result
    assert candidate["rerun_intent"] == rerun_intent


def test_self_repair_rehearsal_blocked_when_repair_result_execution_status_blocked(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    _with_known_planner_policy(monkeypatch)
    artifacts = _make_self_repair_safe_patch_artifact(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "execution_status_blocked"

    result = run_self_repair_rehearsal(
        result_dir=artifacts,
        output_dir=output_dir,
        source_task_id="safe_patch_task",
        downstream_task_id="safe_patch_task",
        repair_type="closure_artifact_repair",
        available_tests=["action_request_schema_test", "decision_json_schema_test", "continuation_bundle_gate_test"],
    )

    repair_result_path = Path(result["summary"]["artifacts"]["repair_result"])
    repair_result = json.loads(repair_result_path.read_text(encoding="utf-8"))
    repair_result["execution_status"] = "blocked"
    repair_result["execution_blockers"] = ["synthetic_blocker_from_test"]
    repair_result_path.write_text(
        json.dumps(repair_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    candidates = build_proposal_candidates_from_rehearsal_summary(
        result["summary"],
        {
            "source_task_id": "safe_patch_task",
            "line_filter": "L1",
            "workplace_profile_filter": "line-a",
        },
        actor="system",
    )

    assert candidates["candidate_count"] == 1
    candidate = candidates["candidates"][0]
    assert candidate["next_step_readiness"]["can_stage"] is False
    assert "self_repair_repair_result_execution_blocked" in candidate["next_step_readiness"]["blockers"]


def test_self_repair_rehearsal_records_apply_blockers_when_patch_fails(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    artifacts = _make_self_repair_safe_patch_artifact(tmp_path)
    output_dir = tmp_path / "continuation" / "self_repair_rehearsals" / "apply_failed"

    def _failing_build_patch(_root, _patch_intent, *, write_scope):
        return PatchIntentResult(
            ok=False,
            patch_text="",
            quality_report={"status": "rejected"},
            errors=["anchor_not_found"],
        )

    monkeypatch.setattr(
        "solution_agent.continuation.self_repair_rehearsal.build_patch_from_intent",
        _failing_build_patch,
    )

    result = run_self_repair_rehearsal(
        result_dir=artifacts,
        output_dir=output_dir,
        source_task_id="safe_patch_task",
        downstream_task_id="safe_patch_task",
        repair_type="closure_artifact_repair",
        available_tests=["action_request_schema_test", "decision_json_schema_test", "continuation_bundle_gate_test"],
    )

    summary = result["summary"]
    assert summary["can_self_repair"] is False
    assert summary["patch_applied"] is False
    assert summary["source_edited"] is False
    assert summary["apply_readiness"]["can_apply"] is False
    assert summary["apply_readiness"]["status"] == "blocked"
    assert summary["apply_readiness"]["blockers"] == ["self_repair_apply_validation:anchor_not_found"]
    assert summary["blocked_by"] == ["self_repair_apply_validation:anchor_not_found"]
    repair_result = json.loads(Path(summary["artifacts"]["repair_result"]).read_text(encoding="utf-8"))
    assert repair_result["execution_status"] == "blocked"
    assert repair_result["execution_blockers"] == ["self_repair_apply_validation:anchor_not_found"]


def _normalize_path(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return str(Path(path))
