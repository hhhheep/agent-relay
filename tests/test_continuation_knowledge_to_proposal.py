from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from solution_agent.continuation import knowledge_to_proposal


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)
    source_root = Path(".solution/continuation")
    if source_root.exists():
        for name in [
            "planner_policy.yaml",
            "api_council_policy.yaml",
            "api_council_schema.yaml",
            "knowledge_pack_policy.yaml",
            "knowledge_pack_manifest_schema.yaml",
            "knowledge_pack_schema.yaml",
            "run_ledger_schema.yaml",
            "p0c_policy.yaml",
        ]:
            source = source_root / name
            if source.exists():
                target = tmp_path / ".solution" / "continuation" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _load_policy() -> dict[str, Any]:
    payload = yaml.safe_load(Path(".solution/continuation/planner_policy.yaml").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _knowledge_subset(*, source_task_id: str = "task_001", pack_id: str = "kp_001") -> dict[str, Any]:
    return {
        "source_task_id": source_task_id,
        "pack_id": pack_id,
        "source_knowledge_pack_id": pack_id,
        "line_filter": "L1",
        "workplace_profile_filter": "line-a",
    }


def _base_response(
    council_id: str = "ac_001",
    *,
    category: str = "success_but_weak_evidence",
    risk_flags: list[str] | None = None,
    next_step_candidates: list[Any] | None = None,
    disagreement: str = "evidence is borderline",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "council_id": council_id,
        "status": "valid",
        "interpretation_votes": [{"model": "local", "vote": "proceed"}],
        "risk_flags": risk_flags or [],
        "next_step_candidates": next_step_candidates or [],
        "disagreement_summary": disagreement,
        "category": category,
    }


def _load_run_ledger_schema() -> dict[str, Any]:
    payload = yaml.safe_load(Path(".solution/continuation/run_ledger_schema.yaml").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_valid_council_response_emits_at_most_max_candidates(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    policy = _load_policy()
    candidates_input = [
        {"kind": "bounded_probe", "reason": "probe_1"},
        {"kind": "inventory_audit", "reason": "audit_1"},
        {"kind": "decision_synthesis", "reason": "synthesis"},
        {"kind": "metric_completion", "reason": "metric_1"},
    ]
    response = _base_response(
        next_step_candidates=candidates_input,
        category="success_but_weak_evidence",
    )
    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(
        response,
        knowledge_subset=_knowledge_subset(),
        actor="system",
    )

    assert built["candidate_count"] == len(built["candidates"])
    assert built["candidate_count"] <= policy["max_tasks_per_source_result"]


def test_candidate_kinds_are_restricted_to_policy_kinds(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    allowed = set(_load_policy()["allowed_next_step_kinds"])
    response = _base_response(
        next_step_candidates=[
            {"kind": "bounded_probe", "reason": "safe"},
            {"kind": "unsupported_kind", "reason": "unsafe"},
            {"kind": "direct_dispatch", "reason": "must dispatch"},
        ],
        category="missing_capability",
        risk_flags=["missing_capability"],
    )

    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    assert built["candidate_count"] >= 1
    assert {candidate["kind"] for candidate in built["candidates"]} <= allowed


def test_direct_action_candidate_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[{"kind": "direct_dispatch", "reason": "dispatch immediately"}],
        category="success_but_weak_evidence",
    )

    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    assert built["candidate_count"] == 1
    candidate = built["candidates"][0]

    assert candidate["status"] == "rejected"
    assert any("forbidden" in reason for reason in candidate.get("reasons", []))
    assert candidate["status"] != "proposal_candidate_created"


def test_candidate_passes_apply_readiness_metadata_from_raw_candidate(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[
            {
                "kind": "bounded_probe",
                "reason": "rehearsal candidate",
                "apply_readiness": {
                    "route": "bounded_self_repair",
                    "status": "blocked",
                    "can_apply": False,
                    "blockers": ["patch_intent_validation:target_file_is_required"],
                },
                "repair_result": {"artifact_type": "repair_result", "patch_intent_validation_errors": ["target_file_is_required"]},
                "rerun_intent": {"artifact_type": "rerun_intent", "repair_id": "repair_001"},
            }
        ],
        category="missing_capability",
    )

    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    assert built["candidate_count"] == 1
    candidate = built["candidates"][0]

    assert "apply_readiness" in candidate
    assert candidate["apply_readiness"]["status"] == "blocked"
    assert candidate["apply_readiness"]["blockers"] == ["patch_intent_validation:target_file_is_required"]
    assert candidate["repair_result"] == {"artifact_type": "repair_result", "patch_intent_validation_errors": ["target_file_is_required"]}
    assert candidate["rerun_intent"] == {"artifact_type": "rerun_intent", "repair_id": "repair_001"}


def test_candidate_defaults_ready_metadata_for_repair_artifacts_when_missing(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[
            {
                "kind": "bounded_probe",
                "reason": "rehearsal artifact missing readiness",
                "repair_result": {"artifact_type": "repair_result", "patch_intent_validation_errors": ["target_file_is_required"]},
                "rerun_intent": {"artifact_type": "rerun_intent", "repair_id": "repair_001"},
            }
        ],
        category="missing_capability",
    )

    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    assert built["candidate_count"] == 1
    candidate = built["candidates"][0]

    assert candidate["next_step_readiness"] == {
        "schema_version": 1,
        "can_stage": False,
        "route": "",
        "blockers": ["candidate_missing_next_step_readiness_for_repair_artifacts"],
    }
    assert candidate["can_stage"] is False


def test_candidate_missing_readiness_route_is_normalized_case(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[
            {
                "kind": "bounded_probe",
                "reason": "repair candidate with mixed-case route",
                "route": "BOUNDED_SELF_REPAIR",
                "repair_result": {"artifact_type": "repair_result", "patch_intent_validation_errors": ["target_file_is_required"]},
                "rerun_intent": {"artifact_type": "rerun_intent", "repair_id": "repair_002"},
            }
        ],
        category="missing_capability",
    )

    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    assert built["candidate_count"] == 1
    candidate = built["candidates"][0]

    assert candidate["next_step_readiness"]["route"] == "bounded_self_repair"


def test_candidate_normalizes_nested_repair_and_readiness_metadata(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[
            {
                "kind": "bounded_probe",
                "reason": "repair artifacts with mixed-case metadata",
                "repair_result": {
                    "route": "BOUNDED_SELF_REPAIR",
                    "repair_type": "SELF_REPAIR",
                    "execution_status": "PARTIAL",
                },
                "rerun_intent": {
                    "route": "BOUNDED_SELF_REPAIR",
                    "repair_type": "SELF_REPAIR",
                    "execution_status": "BLOCKED",
                },
                "next_step_readiness": {
                    "route": "BOUNDED_SELF_REPAIR",
                    "status": "BLOCKED",
                    "can_apply": "false",
                    "blockers": ["  patch_intent_validation:target_file_is_required  ", ""],
                },
            }
        ],
        category="missing_capability",
    )

    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    assert built["candidate_count"] == 1
    candidate = built["candidates"][0]

    assert candidate["repair_result"] == {
        "route": "bounded_self_repair",
        "repair_type": "self_repair",
        "execution_status": "partial",
    }
    assert candidate["rerun_intent"] == {
        "route": "bounded_self_repair",
        "repair_type": "self_repair",
        "execution_status": "blocked",
    }
    assert candidate["next_step_readiness"]["route"] == "bounded_self_repair"
    assert candidate["next_step_readiness"]["status"] == "blocked"
    assert candidate["next_step_readiness"]["can_apply"] is False
    assert candidate["next_step_readiness"]["blockers"] == [
        "patch_intent_validation:target_file_is_required"
    ]


def test_candidate_preserves_b14_l74_bounded_validation_summary_in_next_step_readiness(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[
            {
                "kind": "bounded_probe",
                "reason": "proposal carries bounded validation readiness",
                "next_step_readiness": {
                    "route": "BOUNDED_SELF_REPAIR",
                    "status": "READY",
                    "can_stage": True,
                    "blockers": [],
                    "bounded_validation": {
                        "bucket_id": "B14-L74",
                        "status": "REPAIR_ONCE",
                        "next_recommendation": "REPAIR_ONCE",
                        "present_valid_artifact_ids": ["metrics", "bbox_predictions"],
                        "missing_artifact_ids": ["action_requests"],
                        "invalid_artifact_ids": ["closure_decision"],
                        "route_reasons": ["artifact_repair_once_possible"],
                        "non_valid_artifact_ids": ["action_requests", "closure_decision"],
                        "artifact_statuses": {
                            "metrics": "VALID",
                            "closure_decision": "INVALID",
                        },
                    },
                },
            }
        ],
        category="missing_capability",
    )

    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(
        response,
        _knowledge_subset(),
    )
    assert built["candidate_count"] == 1
    candidate = built["candidates"][0]

    assert candidate["next_step_readiness"]["can_stage"] is True
    assert candidate["next_step_readiness"]["bounded_validation"] == {
        "bucket_id": "B14-L74",
        "status": "repair_once",
        "next_recommendation": "repair_once",
        "present_valid_artifact_ids": ["metrics", "bbox_predictions"],
        "missing_artifact_ids": ["action_requests"],
        "invalid_artifact_ids": ["closure_decision"],
        "route_reasons": ["artifact_repair_once_possible"],
        "non_valid_artifact_ids": ["action_requests", "closure_decision"],
        "artifact_statuses": {
            "metrics": "valid",
            "closure_decision": "invalid",
        },
    }


def test_no_safe_continuation_is_not_stageable(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[{"kind": "no_safe_continuation", "reason": "unsafe to continue"}],
        category="scheduler_resource_wait",
        risk_flags=["unknown"],
    )

    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    assert built["candidate_count"] == 1
    assert built["candidates"][0]["kind"] == "no_safe_continuation"
    assert built["candidates"][0]["can_stage"] is False
    assert built["stageable_candidate_count"] == 0


def test_codex_debug_request_is_advisory_only_and_no_direct_source_edit(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[{"kind": "codex_debug_request", "reason": "source issue", "source_edit": True}],
        category="source_code_required",
    )

    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    candidate = built["candidates"][0]

    assert candidate["kind"] == "codex_debug_request"
    assert candidate["can_stage"] is False
    assert candidate["requires_human_approval"] is True
    assert candidate["status"] == "rejected"
    assert any("forbidden_field:source_edit" in reason for reason in candidate.get("reasons", []))


def test_missing_artifact_and_capability_map_to_requests_not_experimental_failures(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    allowed = set(_load_policy()["allowed_next_step_kinds"])

    artifact_response = _base_response(category="missing_artifact", risk_flags=["missing_artifact"])
    artifact_built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(
        artifact_response,
        _knowledge_subset(),
    )

    assert all(candidate["kind"] in allowed for candidate in artifact_built["candidates"])
    assert artifact_built["candidates"][0]["kind"] in {"inventory_audit", "metric_completion"}

    capability_response = _base_response(category="missing_capability", risk_flags=["missing_capability"])
    capability_built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(
        capability_response,
        _knowledge_subset(),
    )

    assert capability_built["candidates"][0]["kind"] == "missing_capability_request"


def test_weak_metric_maps_to_bounded_probe_or_decision_synthesis_with_claim_limits(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        category="success_but_weak_evidence",
        risk_flags=["weak", "metric_incomplete"],
        next_step_candidates=[],
    )
    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())

    assert built["candidate_count"] in {1, 2}
    assert {
        candidate["kind"] for candidate in built["candidates"]
    } <= {"bounded_probe", "decision_synthesis"}
    assert all(candidate["claim_limitations"] for candidate in built["candidates"])


def test_writes_are_under_proposal_candidates_root_only(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[{"kind": "bounded_probe", "reason": "bounded follow-up"}],
        category="success_but_weak_evidence",
    )
    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    written = knowledge_to_proposal.write_proposal_candidates_dry_run(built)

    candidates_dir = Path(written["candidates_path"]).parent
    assert written["created"] is True
    assert candidates_dir == tmp_path / "continuation" / "proposal_candidates" / built["proposal_run_id"]
    assert Path(written["summary_path"]).parent == candidates_dir
    assert Path(written["candidates_path"]).exists()
    assert Path(written["summary_path"]).exists()
    assert list(candidates_dir.iterdir()) == sorted(
        [
            Path(written["candidates_path"]),
            Path(written["summary_path"]),
        ]
    )


def test_same_proposal_run_id_with_same_payload_is_safe_replay(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[{"kind": "bounded_probe", "reason": "probe"}],
        category="success_but_weak_evidence",
    )
    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())

    first = knowledge_to_proposal.write_proposal_candidates_dry_run(built)
    second = knowledge_to_proposal.write_proposal_candidates_dry_run(
        built,
        proposal_run_id=built["proposal_run_id"],
    )

    assert first["created"] is True
    assert second["created"] is False
    assert second["replayed"] is True
    assert second["candidates_path"] == first["candidates_path"]


def test_same_proposal_run_id_with_different_payload_fails_closed(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[{"kind": "bounded_probe", "reason": "probe"}],
        category="success_but_weak_evidence",
    )
    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    first = knowledge_to_proposal.write_proposal_candidates_dry_run(built)

    assert first["created"] is True

    altered = json.loads(json.dumps(built))
    altered["status"] = "proposal_candidates_created"
    altered["candidate_count"] = 0
    altered["stageable_candidate_count"] = 0
    altered["candidates"][0]["reason"] = "mutated reason"

    second = knowledge_to_proposal.write_proposal_candidates_dry_run(altered, proposal_run_id=built["proposal_run_id"])

    assert second["created"] is False
    assert second["replayed"] is False
    assert second["status"] == "proposal_candidates_rejected"
    assert any("idempotency_collision" in reason for reason in second.get("reasons", []))


def test_candidate_side_effects_all_false(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[{"kind": "bounded_probe", "reason": "bounded"}],
        category="success_but_weak_evidence",
    )
    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())

    for candidate in built["candidates"]:
        assert all(value is False for value in candidate["side_effects"].values())
    written = knowledge_to_proposal.write_proposal_candidates_dry_run(built)
    assert written["summary"]["all_side_effects_false"] is True


def test_no_subprocess_network_api_calls(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[{"kind": "bounded_probe", "reason": "bounded"}],
        category="success_but_weak_evidence",
    )
    _ = _knowledge_subset()

    with (
        patch("solution_agent.continuation.knowledge_to_proposal.subprocess.run") as run_mock,
        patch("solution_agent.continuation.knowledge_to_proposal.subprocess.check_output") as check_output_mock,
        patch("solution_agent.continuation.knowledge_to_proposal.subprocess.Popen") as popen_mock,
        patch("solution_agent.continuation.knowledge_to_proposal.socket.create_connection") as create_connection_mock,
        patch("solution_agent.continuation.api_council.subprocess.run") as ac_run_mock,
        patch("solution_agent.continuation.api_council.subprocess.check_output") as ac_check_output_mock,
        patch("solution_agent.continuation.api_council.subprocess.Popen") as ac_popen_mock,
        patch("solution_agent.continuation.api_council.socket.create_connection") as ac_create_connection_mock,
    ):
        built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
        written = knowledge_to_proposal.write_proposal_candidates_dry_run(built)

    assert built["status"] in {"proposal_candidates_created", "proposal_candidates_rejected"}
    assert written["created"] is True
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called
    assert not ac_run_mock.called
    assert not ac_check_output_mock.called
    assert not ac_popen_mock.called
    assert not ac_create_connection_mock.called


def test_optional_proposal_candidate_ledger_stage_if_available(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_ledger_schema = _load_run_ledger_schema()
    if "proposal_candidate" not in set(run_ledger_schema.get("allowed_stages", [])):
        return

    response = _base_response(
        next_step_candidates=[{"kind": "bounded_probe", "reason": "bounded"}],
        category="success_but_weak_evidence",
    )
    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    written = knowledge_to_proposal.write_proposal_candidates_dry_run(built)
    ledger_path = tmp_path / "continuation" / "run_ledger" / f"{built['proposal_run_id']}.jsonl"
    # write stage events next to the continuation ledger root, not under proposals
    assert ledger_path.exists()
    events = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(event.get("stage") == "proposal_candidate" for event in events)


def test_optional_proposal_candidate_observation_log_written(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[{"kind": "bounded_probe", "reason": "bounded"}],
        category="success_but_weak_evidence",
    )
    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())
    written = knowledge_to_proposal.write_proposal_candidates_dry_run(built)

    observation_path = tmp_path / "continuation" / "observations" / f"{built['proposal_run_id']}.jsonl"
    assert written["created"] is True
    assert observation_path.exists()

    records = [json.loads(line) for line in observation_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["stage"] == "proposal_candidate"
    assert records[0]["run_id"] == built["proposal_run_id"]
    assert records[0]["status"] in {"created", "proposal_candidates_created"}
    assert records[0]["artifact_path"] == written["candidates_path"]
    assert records[0]["payload"]["candidate_count"] == written["candidate_count"]


def test_observation_log_is_idempotent_for_same_payload(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    response = _base_response(
        next_step_candidates=[{"kind": "bounded_probe", "reason": "bounded"}],
        category="success_but_weak_evidence",
    )
    built = knowledge_to_proposal.build_proposal_candidates_from_council_dry_run(response, _knowledge_subset())

    first = knowledge_to_proposal.write_proposal_candidates_dry_run(built)
    second = knowledge_to_proposal.write_proposal_candidates_dry_run(
        built,
        proposal_run_id=built["proposal_run_id"],
    )

    observation_path = tmp_path / "continuation" / "observations" / f"{built['proposal_run_id']}.jsonl"
    assert first["created"] is True
    assert second["created"] is False
    assert second["replayed"] is True
    assert observation_path.exists()
    records = [json.loads(line) for line in observation_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(records) == 1
