from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import json

from solution_agent.continuation import api_council
from solution_agent.continuation.knowledge_pack import (
    build_knowledge_pack_manifest_dry_run,
    select_remote_safe_knowledge_subset,
)
from solution_agent.continuation import ledger as continuation_ledger

import pytest
import yaml


CANONICAL_SECTIONS = [
    "project_brief",
    "chronology",
    "experiment_lines",
    "workplace_profiles",
    "capability_inventory",
    "result_index",
    "decision_log",
    "failure_taxonomy",
    "action_requests",
]


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)


def _base_items(actor: str = "system", line: str = "L1", workplace_profile: str = "line-a") -> list[dict]:
    return [
        {
            "section_id": section,
            "classification": "remote_safe",
            "path": f"knowledge/{section}.json",
            "workplace_profile": workplace_profile,
            "line": line,
        }
        for section in CANONICAL_SECTIONS
    ]


def _build_subset() -> dict:
    manifest = build_knowledge_pack_manifest_dry_run(_base_items())
    return select_remote_safe_knowledge_subset(manifest, line="L1", workplace_profile="line-a")


def _valid_response(council_id: str) -> dict:
    return {
        "schema_version": 1,
        "council_id": council_id,
        "status": "valid",
        "interpretation_votes": [{"model": "local", "vote": "proceed"}],
        "risk_flags": ["minor_risk"],
        "next_step_candidates": [{"kind": "bounded_probe", "scope": "local"}],
        "disagreement_summary": {"text": "low risk"},
    }


def test_council_request_from_remote_safe_subset_creates_request_artifact(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    subset = _build_subset()
    request = api_council.build_api_council_request_dry_run(
        subset, actor="system", source_task_id="task_001", line="L1", workplace_profile="line-a"
    )
    written = api_council.write_api_council_request_dry_run(request)

    assert written["status"] == "accepted"
    assert written["created"] is True
    assert written["replayed"] is False
    assert written["council_id"] == request["council_id"]
    assert written["request_path"] == str(tmp_path / "continuation" / "api_council" / request["council_id"] / "request.json")
    assert Path(written["request_path"]).exists()


def test_request_only_allows_expected_outputs_and_forbidden_actions(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    subset = _build_subset()
    request = api_council.build_api_council_request_dry_run(
        subset, actor="system", source_task_id="task_001", line="L1", workplace_profile="line-a"
    )

    assert set(request["allowed_outputs"]) == {
        "interpretation_votes",
        "risk_flags",
        "next_step_candidates",
        "disagreement_summary",
    }
    assert set(request["forbidden_actions"]) == {
        "write_inbox",
        "promote",
        "dispatch",
        "scheduler_submit",
        "registry_mutation",
        "remote_execution",
        "source_edit",
        "path_guessing",
        "credential_request",
    }


def test_request_write_is_idempotent_replay_safe(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    subset = _build_subset()
    request = api_council.build_api_council_request_dry_run(
        subset, actor="system", source_task_id="task_001", line="L1", workplace_profile="line-a"
    )

    first = api_council.write_api_council_request_dry_run(request)
    second = api_council.write_api_council_request_dry_run(request)

    assert first["created"] is True
    assert second["created"] is False
    assert second["replayed"] is True
    assert second["request_path"] == first["request_path"]


def test_request_write_same_council_id_with_different_payload_is_idempotency_collision(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    subset = _build_subset()
    request = api_council.build_api_council_request_dry_run(
        subset, actor="system", source_task_id="task_001", line="L1", workplace_profile="line-a"
    )
    api_council.write_api_council_request_dry_run(request)

    modified = json.loads(json.dumps(request))
    modified["line"] = "L2"
    blocked = api_council.write_api_council_request_dry_run(modified, council_id=request["council_id"])

    assert blocked["status"] == "blocked"
    assert any("idempotency_collision" in reason for reason in blocked.get("reasons", []))
    assert blocked["created"] is False
    assert blocked["replayed"] is False


def test_response_validation_accepts_valid_advisory_payload(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    subset = _build_subset()
    request = api_council.build_api_council_request_dry_run(
        subset, actor="system", source_task_id="task_001", line="L1", workplace_profile="line-a"
    )
    response = _valid_response(request["council_id"])

    result = api_council.validate_api_council_response(response, request=request)

    assert result["status"] == "valid"
    assert result["reasons"] == []


def test_response_validation_rejects_direct_actions(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    subset = _build_subset()
    request = api_council.build_api_council_request_dry_run(
        subset, actor="system", source_task_id="task_001", line="L1", workplace_profile="line-a"
    )
    response = _valid_response(request["council_id"])
    response["interpretation_votes"] = [{"model": "local", "vote": "direct_dispatch"}]
    result = api_council.validate_api_council_response(response, request=request)

    assert result["status"] == "rejected"
    assert any("forbidden" in reason for reason in result["reasons"])

    direct = api_council.validate_api_council_response(
        {"schema_version": 1, "council_id": request["council_id"], "interpretation_votes": [], "risk_flags": [], "next_step_candidates": [], "disagreement_summary": "", "status": "valid", "dispatch": True},
        request=request,
    )
    assert direct["status"] == "rejected"


def test_response_validation_rejects_credentials_and_path_guessing(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    subset = _build_subset()
    request = api_council.build_api_council_request_dry_run(
        subset, actor="system", source_task_id="task_001", line="L1", workplace_profile="line-a"
    )
    response = _valid_response(request["council_id"])
    response["disagreement_summary"] = "requires api_key for remote credentials"
    response["next_step_candidates"] = [{"kind": "bounded_probe", "path": "/tmp/unsafe/path"}]
    result = api_council.validate_api_council_response(response, request=request)

    assert result["status"] == "rejected"
    assert any("credential" in reason for reason in result["reasons"]) or any("path_guessing" in reason for reason in result["reasons"])


def test_response_write_is_idempotent_and_collision_safe(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    subset = _build_subset()
    request = api_council.build_api_council_request_dry_run(
        subset, actor="system", source_task_id="task_001", line="L1", workplace_profile="line-a"
    )
    response = _valid_response(request["council_id"])

    first = api_council.write_api_council_response_dry_run(response, council_id=request["council_id"])
    second = api_council.write_api_council_response_dry_run(response, council_id=request["council_id"])
    assert first["created"] is True
    assert second["created"] is False
    assert second["replayed"] is True
    assert second["response_path"] == first["response_path"]

    collision = api_council.write_api_council_response_dry_run(
        {**response, "risk_flags": ["new flag"]}, council_id=request["council_id"]
    )
    assert collision["status"] == "rejected"
    assert any("idempotency_collision" in reason for reason in collision.get("reasons", []))
    assert collision["created"] is False


def test_response_summary_reports_counts_and_advisory_only(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    subset = _build_subset()
    request = api_council.build_api_council_request_dry_run(
        subset, actor="system", source_task_id="task_001", line="L1", workplace_profile="line-a"
    )
    response = _valid_response(request["council_id"])
    summary = api_council.summarize_api_council_response(response)

    assert summary["interpretation_vote_count"] == 1
    assert summary["next_step_candidate_count"] == 1
    assert summary["risk_flags_count"] == 1
    assert summary["advisory_only"] is True


def test_api_council_no_subprocess_network_api_calls(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    with (
        patch("solution_agent.continuation.api_council.subprocess.run") as run_mock,
        patch("solution_agent.continuation.api_council.subprocess.check_output") as check_output_mock,
        patch("solution_agent.continuation.api_council.subprocess.Popen") as popen_mock,
        patch("solution_agent.continuation.api_council.socket.create_connection") as create_connection_mock,
    ):
        run_mock.side_effect = AssertionError("subprocess should not be called")
        check_output_mock.side_effect = AssertionError("subprocess should not be called")
        popen_mock.side_effect = AssertionError("subprocess should not be called")
        create_connection_mock.side_effect = AssertionError("network should not be called")

        subset = _build_subset()
        request = api_council.build_api_council_request_dry_run(
            subset, actor="system", source_task_id="task_001", line="L1", workplace_profile="line-a"
        )
        written_request = api_council.write_api_council_request_dry_run(request)
        response = _valid_response(request["council_id"])
        validated = api_council.validate_api_council_response(response, request=request)
        written_response = api_council.write_api_council_response_dry_run(response, council_id=request["council_id"])
        summary = api_council.summarize_api_council_response(response)

    assert written_request["created"] is True
    assert validated["status"] == "valid"
    assert written_response["created"] is True
    assert summary["validation_status"] == "valid"
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called


def test_run_ledger_allows_api_council_stage(tmp_path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_schema = yaml.safe_load(Path(".solution/continuation/run_ledger_schema.yaml").read_text(encoding="utf-8"))
    assert "api_council" in set(run_schema["allowed_stages"])

    result = continuation_ledger.append_continuation_run_event(
        stage="api_council",
        status="succeeded",
        actor="system",
        idempotency_key="api_council_validation:001",
        run_id="api_council_stage_check",
    )
    assert result["written"] in (True, False)
    assert result["path"] == str(tmp_path / "continuation" / "run_ledger" / "api_council_stage_check.jsonl")
