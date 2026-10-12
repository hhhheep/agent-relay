from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import yaml

from solution_agent.cli import cli
from solution_agent.continuation.memory_observer import read_continuation_observations
from solution_agent.continuation.mission_remote_loop import (
    MissionRemoteContinuationDecision,
    MissionRemoteGateReport,
    assess_local_mission_remote_preflight,
    assess_mission_remote_launch_packet,
    assess_mission_remote_gate,
    build_local_mission_remote_followup_candidates,
    execute_mission_remote_launch_once,
    finalize_mission_remote_collect_judge,
    run_local_mission_remote_rehearsal,
    sync_finalize_mission_remote_iteration,
    summarize_local_mission_remote_rehearsal,
    write_mission_remote_launch_packet,
    write_local_mission_remote_followup_candidates,
    _mission_remote_kb_update_payload,
)


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)
    monkeypatch.setattr(
        "solution_agent.continuation.policy.solution_continuation_dir",
        lambda: repo_root / ".solution" / "continuation",
    )


def _append_budget_rows(run_root: Path, *rows: dict) -> None:
    budget_path = run_root / "budget_ledger.jsonl"
    budget_path.parent.mkdir(parents=True, exist_ok=True)
    with budget_path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_last_remote_preflight(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "state" / "last_remote_preflight.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _project_knowledge() -> dict:
    return {
        "project_id": "B14_spatial_grace_graph_utility",
        "current_state": {
            "mainline": "mask/evidence -> calibrated bbox -> gated P2 fallback",
        },
        "evidence_refs": [
            {"ref": "docs/B14_PROJECT_CHRONOLOGY_FOR_AGENTS_2026-05-21.md", "authority": "chronology"},
            {"ref": "results/L72_result.yaml", "authority": "collected_result"},
        ],
        "claim_limits": [
            {"limit": "bounded validation is proxy evidence only; no formal claim"},
        ],
        "next_safe_action": {
            "action": "Run a bounded baseline-vs-sidecar comparison probe.",
            "expected_outputs": ["comparison_report.json", "claim_limited_summary.md"],
        },
    }


def _discussion_payload() -> dict:
    return {
        "key_uncertainty": "Whether sidecar crop-retoken candidates improve large-image bbox proxy coverage over baseline.",
        "rejected_options": [{"option": "formal IoU claim", "reason": "Current evidence is bounded/proxy only."}],
        "if_success_next": "Expand to a small mission-budget validation bucket.",
        "if_failure_next": "Prune sidecar and inspect mask/evidence calibration.",
        "stop_boundaries": ["formal_claim", "core_source_behavior"],
        "fairness_protocol": {
            "baseline": "existing large-image bbox smoke without sidecar",
            "variant": "sidecar crop-retoken bbox smoke",
            "metrics": ["candidate_count", "coordinate_validity", "proxy_recall"],
            "input_scope": "same bounded large-image bucket",
            "falsification_test": "variant must not win by invalid boxes",
        },
    }


def _mission_payload() -> dict:
    return {
        "mission_id": "B14_mission_remote_v1",
        "proxy_budget_limit": 2,
        "remote_budget": {
            "max_consecutive_proxy_or_diagnostic_tasks": 2,
        },
    }


def _task_payload() -> dict:
    return {
        "task_id": "B14-L74-local-rehearsal",
        "task_class": "bounded_validation_bucket",
        "workplace_profile": {
            "profile": "EXAMPLE_WORKER_A_B14",
            "host": "EXAMPLE_WORKER_A",
            "remote_root": "/srv/example-workspaces-b/B14_agent_handoff_20260521/runs",
            "remote_workdir": "/srv/example-workspaces-b/B14_agent_handoff_20260521",
        },
        "discussion_evidence_pack": {
            "canonical_formulation": "bounded baseline-vs-sidecar validation bucket for B14-L74",
            "current_state_chronology": ["B14 chronology reviewed", "bounded bucket selected"],
            "materialized_results": ["L72 artifacts available"],
            "diagnostic_proxy_results": ["sidecar smoke is bounded proxy evidence only"],
            "negative_results": ["no formal IoU claim available"],
            "artifact_inventory": ["metrics.json", "bbox_predictions.jsonl", "evidence_payload.jsonl"],
            "claim_limits": ["proxy_evidence_only", "no_formal_claim_upgrade"],
            "open_uncertainties": ["whether sidecar reduces bbox uncertainty on the shared bounded bucket"],
        },
        "formulation_faithfulness": {
            "canonical_or_diagnostic": "diagnostic",
            "canonical_formulation": "collect bounded sidecar-vs-baseline evidence without replacing the canonical learnable model",
            "learnable_modules": ["mask/evidence model", "bbox decoder"],
            "forbidden_substitutions": ["heuristic postprocess as completion", "formal claim from proxy evidence"],
            "proxy_budget_used": 1,
            "diagnostic_cannot_prove": ["cannot prove final IoU or submission readiness"],
            "route_back_to_canonical": "use bounded evidence to choose the next canonical implementation step",
        },
        "proxy_budget_used": 1,
        "fake_collect_mode": "artifact_complete",
    }


def test_mission_remote_gate_allows_bounded_validation_bucket() -> None:
    gate = assess_mission_remote_gate(
        _mission_payload(),
        _task_payload(),
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert gate.status == "allow"
    assert gate.can_rehearse_locally is True
    assert gate.allow_remote_launch is False
    assert gate.collect_judge_contract_present is True
    assert gate.discussion_evidence_pack_present is True
    assert gate.formulation_faithfulness_present is True
    assert all(value is False for value in gate.side_effects.values())


def test_mission_remote_gate_blocks_missing_workplace_full_dataset_and_formal_claim() -> None:
    task = _task_payload()
    task["workplace_profile"] = {}
    task["full_dataset"] = True
    task["formal_claim_requested"] = True

    gate = assess_mission_remote_gate(
        _mission_payload(),
        task,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert gate.status == "block"
    assert "missing_workplace_profile" in gate.reason_codes
    assert "unbounded_or_full_dataset_scope" in gate.reason_codes
    assert "formal_claim_upgrade_requested" in gate.reason_codes


def test_mission_remote_gate_blocks_proxy_budget_exhaustion_and_missing_evidence_pack() -> None:
    task = _task_payload()
    task["discussion_evidence_pack"] = {}
    task["proxy_budget_used"] = 2
    task["formulation_faithfulness"]["proxy_budget_used"] = 2

    gate = assess_mission_remote_gate(
        _mission_payload(),
        task,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert gate.status == "block"
    assert "discussion_evidence_pack_incomplete" in gate.reason_codes
    assert "proxy_budget_exhausted" in gate.reason_codes
    assert gate.evidence_gap["type"] == "evidence_gap"
    assert "canonical_formulation" in gate.evidence_gap["missing_fields"]


def test_mission_remote_preflight_surfaces_gate_and_evidence_gap_without_rehearsal() -> None:
    task = _task_payload()
    task["discussion_evidence_pack"] = {}

    result = assess_local_mission_remote_preflight(
        _mission_payload(),
        task,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert result["status"] == "ready"
    assert result["gate"]["status"] == "block"
    assert result["preflight"]["status"] == "blocked"
    assert result["preflight"]["declared_only"] is True
    assert result["preflight"]["collect_judge_contract_present"] is True
    assert result["preflight"]["evidence_gap"]["type"] == "evidence_gap"
    assert "discussion_evidence_pack_incomplete" in result["gate"]["reason_codes"]
    assert all(value is False for value in result["side_effects"].values())


def test_mission_remote_preflight_blocks_third_consecutive_diagnostic_task_from_budget_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    result = assess_local_mission_remote_preflight(
        _mission_payload(),
        _task_payload(),
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
        run_root=run_root,
    )

    assert result["gate"]["status"] == "allow"
    assert result["gate"]["prior_budget_ledger_entries"] == 2
    assert result["gate"]["prior_consecutive_proxy_or_diagnostic_tasks"] == 0
    assert result["gate"]["prior_local_rehearsal_consecutive_proxy_or_diagnostic_tasks"] == 2
    assert result["gate"]["proxy_or_diagnostic_budget_limit"] == 2
    assert Path(result["gate"]["budget_ledger_path"]).exists()


def test_mission_remote_preflight_blocks_third_consecutive_real_remote_diagnostic_task_from_budget_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    _append_budget_rows(
        run_root,
        {
            "execution_scope": "remote_task",
            "executed": True,
            "canonical_or_diagnostic": "diagnostic",
            "consecutive_proxy_or_diagnostic_tasks_used": 1,
            "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": 0,
            "remote_tasks_total_used": 1,
        },
        {
            "execution_scope": "remote_task",
            "executed": True,
            "canonical_or_diagnostic": "diagnostic",
            "consecutive_proxy_or_diagnostic_tasks_used": 2,
            "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": 0,
            "remote_tasks_total_used": 2,
        },
    )

    result = assess_local_mission_remote_preflight(
        _mission_payload(),
        _task_payload(),
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
        run_root=run_root,
    )

    assert result["gate"]["status"] == "block"
    assert "proxy_chain_budget_exhausted" in result["gate"]["reason_codes"]
    assert result["gate"]["prior_budget_ledger_entries"] == 2
    assert result["gate"]["prior_consecutive_proxy_or_diagnostic_tasks"] == 2
    assert result["gate"]["prior_local_rehearsal_consecutive_proxy_or_diagnostic_tasks"] == 0


def test_mission_remote_preflight_blocks_until_prior_remote_collect_judge_is_finished(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    _append_budget_rows(
        run_root,
        {
            "iteration_id": "iteration_003",
            "execution_scope": "remote_task",
            "executed": True,
            "collect_judge_completed": False,
            "canonical_or_diagnostic": "diagnostic",
            "consecutive_proxy_or_diagnostic_tasks_used": 1,
            "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": 0,
            "remote_tasks_total_used": 1,
        },
    )

    result = assess_local_mission_remote_preflight(
        _mission_payload(),
        _task_payload(),
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
        run_root=run_root,
    )

    assert result["gate"]["status"] == "block"
    assert "prior_remote_collect_judge_incomplete" in result["gate"]["reason_codes"]
    assert result["gate"]["prior_remote_collect_judge_pending"] is True
    assert result["gate"]["prior_pending_remote_iteration_id"] == "iteration_003"


def test_mission_remote_preflight_ignores_legacy_transport_failed_remote_row_for_budget_and_pending_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    _append_budget_rows(
        run_root,
        {
            "iteration_id": "iteration_004",
            "execution_scope": "remote_task",
            "executed": True,
            "launch_status": "unknown",
            "collect_judge_completed": False,
            "canonical_or_diagnostic": "diagnostic",
            "consecutive_proxy_or_diagnostic_tasks_used": 1,
            "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": 2,
            "remote_tasks_total_used": 1,
            "local_rehearsals_total_used": 2,
        },
    )

    result = assess_local_mission_remote_preflight(
        _mission_payload(),
        _task_payload(),
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
        run_root=run_root,
    )

    assert result["gate"]["status"] == "allow"
    assert "prior_remote_collect_judge_incomplete" not in result["gate"]["reason_codes"]
    assert result["gate"]["prior_remote_collect_judge_pending"] is False
    assert result["gate"]["prior_pending_remote_iteration_id"] == ""
    assert result["gate"]["prior_consecutive_proxy_or_diagnostic_tasks"] == 0


def test_local_mission_remote_rehearsal_writes_gate_collect_judge_decision_and_budget_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    result = run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=tmp_path / "outputs" / "night_runs" / "B14_mission_remote_v1",
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    iteration_dir = Path(result["iteration_dir"])
    assert result["status"] == "written"
    assert (iteration_dir / "decision.yaml").exists()
    assert (iteration_dir / "mission_gate_report.yaml").exists()
    assert (iteration_dir / "remote_launch_preflight.yaml").exists()
    assert (iteration_dir / "launch_record.yaml").exists()
    assert (iteration_dir / "wait_report.yaml").exists()
    assert (iteration_dir / "collect_report.yaml").exists()
    assert (iteration_dir / "judge_report.yaml").exists()
    assert (iteration_dir / "continuation_decision.yaml").exists()
    assert (iteration_dir / "information_gain.yaml").exists()
    assert (iteration_dir / "budget_update.yaml").exists()
    assert (iteration_dir / "factual_memory_append.jsonl").exists()
    assert (iteration_dir / "kb_update_proposal" / "kb_update_manifest.yaml").exists()
    assert (iteration_dir / "kb_update_proposal" / "proposal_pointer.yaml").exists()
    assert Path(result["mission_state_path"]).exists()
    assert Path(result["checkpoint_path"]).exists()

    judge_report = json.loads((iteration_dir / "fake_collect_output" / "b14_l74_artifact_readiness_report.json").read_text(encoding="utf-8"))
    assert judge_report["status"] == "artifact_complete"

    continuation_decision = json.loads(json.dumps(result["continuation_decision"]))
    assert continuation_decision["next_action"] == "continue"
    assert continuation_decision["judge_status"] == "artifact_complete"
    assert continuation_decision["information_gain"]["direction_decision"] == 2

    wait_report = json.loads(json.dumps(result["wait_report"]))
    assert wait_report["status"] == "bounded_wait_skipped_local_dry_run"
    assert wait_report["executed"] is False

    information_gain = json.loads(json.dumps(result["information_gain"]))
    assert information_gain["total_score"] == 9
    assert information_gain["routing"] == "useful_bounded_evidence_continue_or_expand"
    assert Path(result["factual_memory_path"]).exists()
    assert Path(result["kb_update_proposal_dir"]).exists()
    assert result["kb_update_proposal"]["status"] == "ready"
    assert Path(result["kb_update_proposal"]["proposal_dir"]).exists()

    budget_lines = (Path(result["budget_ledger_path"]).read_text(encoding="utf-8")).splitlines()
    assert len(budget_lines) == 1
    budget_entry = json.loads(budget_lines[0])
    assert budget_entry["execution_scope"] == "local_rehearsal"
    assert budget_entry["executed"] is False
    assert budget_entry["remote_tasks_total_used"] == 0
    assert budget_entry["canonical_or_diagnostic"] == "diagnostic"
    assert budget_entry["consecutive_proxy_or_diagnostic_tasks_used"] == 0
    assert budget_entry["local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 1
    assert budget_entry["next_action"] == "continue"

    mission_state = json.loads(Path(result["mission_state_path"]).read_text(encoding="utf-8"))
    assert mission_state["current_iteration"] == 1
    assert mission_state["last_decision_id"].endswith(":continue")
    assert mission_state["parent_decision_id"] == mission_state["last_decision_id"]
    assert mission_state["claim_limits"] == ["proxy_evidence_only", "no_formal_claim_upgrade"]

    checkpoint = json.loads(Path(result["checkpoint_path"]).read_text(encoding="utf-8"))
    assert checkpoint["status"] == "ready"
    assert checkpoint["parent_decision_id"] == mission_state["last_decision_id"]
    assert checkpoint["checkpoint_id"].endswith("__" + mission_state["last_decision_id"])

    observation = result["continuation_observation"]
    assert observation["written"] is True
    records = read_continuation_observations(run_id=_mission_payload()["mission_id"])
    assert len(records) == 1
    assert records[0]["stage"] == "mission_remote_rehearsal"
    assert records[0]["status"] == "ready"
    assert records[0]["actor"] == "mission_remote_loop"
    assert records[0]["source_task_id"] == _task_payload()["task_id"]
    assert records[0]["payload"]["judge_status"] == "artifact_complete"
    assert records[0]["payload"]["next_action"] == "continue"
    assert Path(records[0]["payload"]["factual_memory_path"]).exists()
    assert Path(records[0]["payload"]["kb_update_proposal_dir"]).exists()


def test_kb_update_proposal_id_changes_when_same_iteration_is_rejudged() -> None:
    gate = MissionRemoteGateReport(
        mission_id="B14_mission_remote_v1",
        task_id="B14-L74-bounded-sidecar-vs-baseline-validation",
        status="allow",
        can_rehearse_locally=True,
        collect_judge_contract_present=True,
        discussion_evidence_pack_present=True,
        formulation_faithfulness_present=True,
    )
    project = _project_knowledge()
    task = _task_payload()
    discussion = _discussion_payload()
    stop_decision = MissionRemoteContinuationDecision(
        mission_id="B14_mission_remote_v1",
        task_id="B14-L74-bounded-sidecar-vs-baseline-validation",
        status="blocked",
        next_action="stop",
        judge_status="artifact_missing",
        reason_text="missing collectable evidence",
        claim_limits=["proxy_evidence_only", "no_formal_claim_upgrade"],
    )
    pivot_decision = MissionRemoteContinuationDecision(
        mission_id="B14_mission_remote_v1",
        task_id="B14-L74-bounded-sidecar-vs-baseline-validation",
        status="written",
        next_action="pivot",
        judge_status="pivot",
        reason_text="sidecar mechanism evidence missing but closure artifacts are valid",
        claim_limits=["bounded_proxy_evidence_only", "no_formal_claim_upgrade"],
    )

    stop_payload = _mission_remote_kb_update_payload(
        mission_id="B14_mission_remote_v1",
        task_id="B14-L74-bounded-sidecar-vs-baseline-validation",
        iteration_id="iteration_005",
        decision_id="B14_mission_remote_v1:B14-L74-bounded-sidecar-vs-baseline-validation:iteration_005:stop",
        task_payload=task,
        discussion_payload=discussion,
        project_knowledge=project,
        gate=gate,
        judge_report={"status": "artifact_missing"},
        decision=stop_decision,
    )
    pivot_payload = _mission_remote_kb_update_payload(
        mission_id="B14_mission_remote_v1",
        task_id="B14-L74-bounded-sidecar-vs-baseline-validation",
        iteration_id="iteration_005",
        decision_id="B14_mission_remote_v1:B14-L74-bounded-sidecar-vs-baseline-validation:iteration_005:pivot",
        task_payload=task,
        discussion_payload=discussion,
        project_knowledge=project,
        gate=gate,
        judge_report={"status": "pivot"},
        decision=pivot_decision,
    )

    assert stop_payload["proposal_id"].startswith("B14_mission_remote_v1__iteration_005__")
    assert pivot_payload["proposal_id"].startswith("B14_mission_remote_v1__iteration_005__")
    assert stop_payload["proposal_id"] != pivot_payload["proposal_id"]
    assert stop_payload["source_id"].endswith(":stop")
    assert pivot_payload["source_id"].endswith(":pivot")


def test_local_mission_remote_rehearsal_uses_existing_validator_to_route_pivot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    task = _task_payload()
    task["fake_collect_mode"] = "pivot"
    result = run_local_mission_remote_rehearsal(
        _mission_payload(),
        task,
        run_root=tmp_path / "outputs" / "night_runs" / "B14_mission_remote_v1",
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert result["judge_report"]["status"] == "pivot"
    assert result["continuation_decision"]["next_action"] == "pivot"


def test_local_mission_remote_rehearsal_resets_proxy_chain_after_canonical_iteration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    canonical_task = _task_payload()
    canonical_task["formulation_faithfulness"]["canonical_or_diagnostic"] = "canonical"
    canonical_task["formulation_faithfulness"].pop("diagnostic_cannot_prove")
    canonical_task["formulation_faithfulness"].pop("route_back_to_canonical")
    canonical_task["proxy_budget_used"] = 0
    result = run_local_mission_remote_rehearsal(
        _mission_payload(),
        canonical_task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert result["gate"]["status"] == "allow"
    summary = summarize_local_mission_remote_rehearsal(run_root)
    assert summary["latest_canonical_or_diagnostic"] == "canonical"
    assert summary["latest_consecutive_proxy_or_diagnostic_tasks_used"] == 0
    assert summary["latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 0


def test_mission_remote_budget_updates_use_formulation_proxy_budget_when_top_level_value_is_stale_zero(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    task = _task_payload()
    task["proxy_budget_used"] = 0
    task["formulation_faithfulness"]["proxy_budget_used"] = 1
    task["remote_task_id"] = "B14-L74-remote-task"

    rehearsal = run_local_mission_remote_rehearsal(
        _mission_payload(),
        task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert rehearsal["gate"]["status"] == "allow"
    rehearsal_budget = yaml.safe_load((Path(rehearsal["iteration_dir"]) / "budget_update.yaml").read_text(encoding="utf-8"))
    assert rehearsal_budget["proxy_or_diagnostic_tasks_used"] == 1

    monkeypatch.setattr(
        "solution_agent.orchestrator.core.run_remote",
        lambda *args, **kwargs: {"status": "running", "run_id": "remote_run_proxy_budget"},
    )
    launch = execute_mission_remote_launch_once(
        _mission_payload(),
        task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    assert launch["budget_update"]["proxy_or_diagnostic_tasks_used"] == 1

    collect_dir = tmp_path / "remote_collect_proxy_budget"
    collect_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("stdout.log", "remote stdout\n"),
        ("stderr.log", ""),
        ("bbox_predictions.jsonl", json.dumps({"image_id": "img-proxy", "bbox": [1, 2, 3, 4]}) + "\n"),
        ("metrics.json", json.dumps({"execution_path": 1, "bbox_proxy_quality": 0.4}) + "\n"),
        ("evidence_payload.jsonl", json.dumps({"image_id": "img-proxy", "evidence": "sidecar-present"}) + "\n"),
        ("decision_summary.json", json.dumps({"status": "claim_limited_complete"}) + "\n"),
        ("action_requests.json", "[]\n"),
    ):
        (collect_dir / name).write_text(payload, encoding="utf-8")

    finalized = finalize_mission_remote_collect_judge(
        _mission_payload(),
        task,
        run_root=run_root,
        iteration_id=Path(launch["iteration_dir"]).name,
        collect_output_dir=collect_dir,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
        sync_pull_result={"status": "ok"},
    )

    assert finalized["status"] == "written"
    finalized_budget = yaml.safe_load((Path(finalized["iteration_dir"]) / "budget_update.yaml").read_text(encoding="utf-8"))
    assert finalized_budget["proxy_or_diagnostic_tasks_used"] == 1

    summary = summarize_local_mission_remote_rehearsal(run_root)
    assert summary["latest_budget_update"]["proxy_or_diagnostic_tasks_used"] == 1


def test_mission_remote_followup_candidates_write_stageable_advisory_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    built = build_local_mission_remote_followup_candidates(run_root, actor="mission_remote_loop")
    assert built["status"] == "ready"
    assert built["can_write_proposal"] is True
    assert built["proposal_run_id"] == f"{mission_id}__mission_remote__iteration_001"
    assert built["candidate_count"] == 1
    candidate = built["candidates"][0]
    assert candidate["kind"] == "bounded_probe"
    assert candidate["can_stage"] is True
    assert candidate["mission_id"] == mission_id
    assert candidate["mission_gate_pass"] is True
    assert candidate["continuation_lane"] == "mission_remote"
    assert candidate["next_step_readiness"]["bounded_validation"]["status"] == "artifact_complete"

    written = write_local_mission_remote_followup_candidates(run_root, actor="mission_remote_loop")
    assert written["status"] == "written"
    assert written["written"] is True
    assert Path(written["candidates_path"]).exists()
    proposal_payload = json.loads(Path(written["candidates_path"]).read_text(encoding="utf-8"))
    assert proposal_payload["status"] == "proposal_candidates_created"
    assert proposal_payload["candidates"][0]["kind"] == "bounded_probe"
    assert proposal_payload["candidates"][0]["mission_id"] == mission_id


def test_mission_remote_followup_candidates_block_when_latest_decision_not_stageable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    blocked_task = _task_payload()
    blocked_task["discussion_evidence_pack"] = {}
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        blocked_task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    built = build_local_mission_remote_followup_candidates(run_root)
    assert built["status"] == "blocked"
    assert built["can_write_proposal"] is False
    assert "mission_gate_blocked" in built["reasons"]
    assert "next_action_not_stageable" in built["reasons"]

    written = write_local_mission_remote_followup_candidates(run_root)
    assert written["written"] is False
    assert written["created"] is False
    assert written["replayed"] is False


def test_mission_remote_followup_candidates_block_third_diagnostic_bounded_probe_after_budget_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    built = build_local_mission_remote_followup_candidates(run_root, actor="mission_remote_loop")
    assert built["status"] == "ready"
    assert built["can_write_proposal"] is True
    assert built["next_action"] == "continue"
    assert built["latest_canonical_or_diagnostic"] == "diagnostic"
    assert built["latest_consecutive_proxy_or_diagnostic_tasks_used"] == 0
    assert built["latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 2
    assert built["proxy_or_diagnostic_budget_limit"] == 2
    assert built["reasons"] == []

    written = write_local_mission_remote_followup_candidates(run_root, actor="mission_remote_loop")
    assert written["written"] is True
    assert Path(written["candidates_path"]).exists()


def test_mission_remote_cli_writes_local_rehearsal_artifacts(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_path = tmp_path / "mission.json"
    task_path = tmp_path / "task.json"
    project_path = tmp_path / "project.json"
    discussion_path = tmp_path / "discussion.json"
    mission_path.write_text(json.dumps(_mission_payload(), indent=2), encoding="utf-8")
    task_path.write_text(json.dumps(_task_payload(), indent=2), encoding="utf-8")
    project_path.write_text(json.dumps(_project_knowledge(), indent=2), encoding="utf-8")
    discussion_path.write_text(json.dumps(_discussion_payload(), indent=2), encoding="utf-8")

    runner = CliRunner()
    run_root = tmp_path / "run_root"
    result = runner.invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-dry-run",
            "--mission-path",
            str(mission_path),
            "--task-path",
            str(task_path),
            "--run-root",
            str(run_root),
            "--project-knowledge-path",
            str(project_path),
            "--discussion-payload-path",
            str(discussion_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "written"
    assert Path(payload["budget_ledger_path"]).exists()


def test_mission_remote_preflight_cli_writes_gate_and_preflight_artifacts(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_path = tmp_path / "mission.json"
    task_path = tmp_path / "task.json"
    project_path = tmp_path / "project.json"
    discussion_path = tmp_path / "discussion.json"
    blocked_task = _task_payload()
    blocked_task["discussion_evidence_pack"] = {}
    mission_path.write_text(json.dumps(_mission_payload(), indent=2), encoding="utf-8")
    task_path.write_text(json.dumps(blocked_task, indent=2), encoding="utf-8")
    project_path.write_text(json.dumps(_project_knowledge(), indent=2), encoding="utf-8")
    discussion_path.write_text(json.dumps(_discussion_payload(), indent=2), encoding="utf-8")

    output_dir = tmp_path / "preflight"
    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-preflight",
            "--mission-path",
            str(mission_path),
            "--task-path",
            str(task_path),
            "--output-dir",
            str(output_dir),
            "--project-knowledge-path",
            str(project_path),
            "--discussion-payload-path",
            str(discussion_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "written"
    assert payload["gate"]["status"] == "block"
    assert payload["preflight"]["status"] == "blocked"
    assert Path(payload["mission_gate_report_path"]).exists()
    assert Path(payload["remote_launch_preflight_path"]).exists()


def test_mission_remote_preflight_cli_reads_prior_budget_history_when_run_root_is_supplied(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_path = tmp_path / "mission.json"
    task_path = tmp_path / "task.json"
    project_path = tmp_path / "project.json"
    discussion_path = tmp_path / "discussion.json"
    mission_path.write_text(json.dumps(_mission_payload(), indent=2), encoding="utf-8")
    task_path.write_text(json.dumps(_task_payload(), indent=2), encoding="utf-8")
    project_path.write_text(json.dumps(_project_knowledge(), indent=2), encoding="utf-8")
    discussion_path.write_text(json.dumps(_discussion_payload(), indent=2), encoding="utf-8")

    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    output_dir = tmp_path / "preflight_with_budget"
    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-preflight",
            "--mission-path",
            str(mission_path),
            "--task-path",
            str(task_path),
            "--output-dir",
            str(output_dir),
            "--run-root",
            str(run_root),
            "--project-knowledge-path",
            str(project_path),
            "--discussion-payload-path",
            str(discussion_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["gate"]["status"] == "allow"
    assert "proxy_chain_budget_exhausted" not in payload["gate"]["reason_codes"]
    assert payload["gate"]["prior_budget_ledger_entries"] == 2
    assert payload["gate"]["prior_consecutive_proxy_or_diagnostic_tasks"] == 0
    assert payload["gate"]["prior_local_rehearsal_consecutive_proxy_or_diagnostic_tasks"] == 2


def test_mission_remote_status_cli_reads_default_mission_run_root(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-status",
            "--mission-id",
            mission_id,
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["present"] is True
    assert payload["iteration_count"] == 1
    assert payload["checkpoint_count"] == 1
    assert payload["latest_gate_status"] == "allow"
    assert payload["latest_judge_status"] == "artifact_complete"
    assert payload["latest_next_action"] == "continue"
    assert payload["latest_canonical_or_diagnostic"] == "diagnostic"
    assert payload["latest_consecutive_proxy_or_diagnostic_tasks_used"] == 0
    assert payload["latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 1
    assert payload["latest_followup_proposal_status"] == "ready"
    assert payload["latest_followup_can_write_proposal"] is True
    assert payload["latest_followup_candidate_count"] == 1
    assert payload["latest_followup_reasons"] == []
    assert payload["latest_information_gain_total_score"] == 9
    assert payload["latest_information_gain_routing"] == "useful_bounded_evidence_continue_or_expand"
    assert Path(payload["latest_factual_memory_path"]).exists()
    assert Path(payload["latest_kb_update_proposal_dir"]).exists()
    assert payload["latest_kb_update_status"] == "ready"
    assert Path(payload["latest_kb_update_global_proposal_dir"]).exists()
    assert payload["mission_state_status"] == "active"
    assert payload["mission_state_current_iteration"] == 1
    assert payload["mission_state_last_decision_id"].endswith(":continue")
    assert payload["latest_checkpoint_parent_decision_id"] == payload["mission_state_last_decision_id"]
    assert Path(payload["mission_state_path"]).exists()
    assert Path(payload["latest_checkpoint_path"]).exists()
    assert Path(payload["budget_ledger_path"]).exists()


def test_mission_remote_status_cli_surfaces_blocked_followup_budget_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-status",
            "--mission-id",
            mission_id,
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["latest_followup_proposal_status"] == "ready"
    assert payload["latest_followup_can_write_proposal"] is True
    assert payload["latest_followup_candidate_count"] == 1
    assert payload["latest_followup_reasons"] == []
    assert payload["latest_consecutive_proxy_or_diagnostic_tasks_used"] == 0
    assert payload["latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 2


def test_mission_remote_status_cli_uses_effective_budget_row_when_latest_audit_row_is_legacy_transport_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    _append_budget_rows(
        run_root,
        {
            "iteration_id": "iteration_003",
            "execution_scope": "remote_task",
            "executed": True,
            "launch_status": "unknown",
            "collect_judge_completed": False,
            "canonical_or_diagnostic": "diagnostic",
            "consecutive_proxy_or_diagnostic_tasks_used": 1,
            "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": 2,
            "remote_tasks_total_used": 1,
            "next_action": "collect_and_judge_pending",
        },
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-status",
            "--mission-id",
            mission_id,
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["latest_followup_proposal_status"] == "ready"
    assert payload["latest_budget_update"]["execution_scope"] == "local_rehearsal"
    assert payload["latest_budget_update"]["next_action"] == "continue"
    assert payload["latest_budget_update_uses_fallback"] is True
    assert payload["latest_budget_audit_update"]["execution_scope"] == "remote_task"
    assert payload["latest_budget_audit_update"]["launch_status"] == "unknown"


def test_mission_remote_status_cli_surfaces_gate_evidence_gap_and_declared_side_effects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    blocked_task = _task_payload()
    blocked_task["discussion_evidence_pack"] = {}
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        blocked_task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-status",
            "--mission-id",
            mission_id,
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["latest_gate_status"] == "block"
    assert "discussion_evidence_pack_incomplete" in payload["latest_gate_reason_codes"]
    assert payload["latest_gate_evidence_gap"]["type"] == "evidence_gap"
    assert "canonical_formulation" in payload["latest_gate_evidence_gap"]["missing_fields"]
    assert payload["latest_collect_judge_contract_present"] is True
    assert payload["latest_discussion_evidence_pack_present"] is False
    assert payload["latest_formulation_faithfulness_present"] is True
    assert payload["latest_side_effects"]["ssh"] is False
    assert payload["latest_side_effects"]["remote_execution"] is False
    assert payload["latest_side_effects"]["scheduler_dispatch"] is False


def test_mission_remote_history_cli_reads_latest_iterations_and_budget_progression(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id

    first_task = _task_payload()
    first_task["fake_collect_mode"] = "artifact_complete"
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        first_task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    second_task = _task_payload()
    second_task["fake_collect_mode"] = "pivot"
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        second_task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-history",
            "--mission-id",
            mission_id,
            "--limit",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["present"] is True
    assert payload["iteration_count"] == 2
    assert payload["checkpoint_count"] == 2
    assert payload["budget_ledger_entries"] == 2
    assert len(payload["history"]) == 2
    assert payload["history"][0]["iteration_id"] == "iteration_001"
    assert payload["history"][0]["judge_status"] == "artifact_complete"
    assert payload["history"][0]["next_action"] == "continue"
    assert payload["history"][0]["collect_judge_contract_present"] is True
    assert payload["history"][0]["discussion_evidence_pack_present"] is True
    assert payload["history"][0]["formulation_faithfulness_present"] is True
    assert payload["history"][0]["side_effects"]["remote_execution"] is False
    assert payload["history"][0]["local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 1
    assert payload["history"][0]["information_gain_total_score"] == 9
    assert Path(payload["history"][0]["factual_memory_path"]).exists()
    assert Path(payload["history"][0]["kb_update_proposal_dir"]).exists()
    assert payload["history"][0]["kb_update_status"] == "ready"
    assert payload["history"][1]["iteration_id"] == "iteration_002"
    assert payload["history"][1]["judge_status"] == "pivot"
    assert payload["history"][1]["next_action"] == "pivot"
    assert payload["history"][1]["gate_evidence_gap"]["missing_fields"] == []
    assert payload["history"][1]["local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 2
    assert payload["history"][1]["information_gain_routing"] == "useful_bounded_evidence_continue_or_expand"
    assert payload["history"][1]["kb_update_status"] == "ready"
    assert payload["history"][1]["budget_update"]["iteration"] == 2
    assert payload["history"][1]["budget_update"]["local_rehearsals_total_used"] == 2


def test_mission_remote_status_and_history_normalize_legacy_local_rehearsal_budget_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    budget_path = run_root / "budget_ledger.jsonl"
    legacy_rows: list[dict] = []
    for raw_line in budget_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(raw_line)
        row["consecutive_proxy_or_diagnostic_tasks_used"] = int(
            row.get("local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used") or 0
        )
        row.pop("execution_scope", None)
        row.pop("executed", None)
        row.pop("local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used", None)
        legacy_rows.append(row)
    budget_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in legacy_rows),
        encoding="utf-8",
    )

    for iteration_id in ("iteration_001", "iteration_002"):
        budget_update_path = run_root / iteration_id / "budget_update.yaml"
        budget_update = yaml.safe_load(budget_update_path.read_text(encoding="utf-8"))
        budget_update["consecutive_proxy_or_diagnostic_tasks_used"] = int(
            budget_update.get("local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used") or 0
        )
        budget_update.pop("execution_scope", None)
        budget_update.pop("executed", None)
        budget_update.pop("local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used", None)
        budget_update_path.write_text(
            yaml.safe_dump(budget_update, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    status_result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-status",
            "--mission-id",
            mission_id,
        ],
    )

    assert status_result.exit_code == 0, status_result.output
    status_payload = json.loads(status_result.output)
    assert status_payload["latest_followup_proposal_status"] == "ready"
    assert status_payload["latest_followup_can_write_proposal"] is True
    assert status_payload["latest_consecutive_proxy_or_diagnostic_tasks_used"] == 0
    assert status_payload["latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 2
    assert status_payload["latest_budget_update"]["execution_scope"] == "local_rehearsal"

    history_result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-history",
            "--mission-id",
            mission_id,
            "--limit",
            "2",
        ],
    )

    assert history_result.exit_code == 0, history_result.output
    history_payload = json.loads(history_result.output)
    assert history_payload["history"][0]["execution_scope"] == "local_rehearsal"
    assert history_payload["history"][0]["consecutive_proxy_or_diagnostic_tasks_used"] == 0
    assert history_payload["history"][0]["local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 1
    assert history_payload["history"][1]["execution_scope"] == "local_rehearsal"
    assert history_payload["history"][1]["consecutive_proxy_or_diagnostic_tasks_used"] == 0
    assert history_payload["history"][1]["local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 2


def test_mission_remote_followup_proposal_cli_writes_advisory_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-followup-proposal",
            "--mission-id",
            mission_id,
            "--actor",
            "mission_remote_loop",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "written"
    assert payload["written"] is True
    assert payload["candidate_count"] == 1
    assert Path(payload["candidates_path"]).exists()


def test_mission_remote_followup_proposal_cli_blocks_proxy_chain_exhausted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-followup-proposal",
            "--mission-id",
            mission_id,
            "--actor",
            "mission_remote_loop",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "written"
    assert payload["written"] is True
    assert payload["candidate_count"] == 1
    assert payload["latest_consecutive_proxy_or_diagnostic_tasks_used"] == 0
    assert payload["latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 2
    assert payload["proxy_or_diagnostic_budget_limit"] == 2
    assert payload["reasons"] == []


def test_mission_remote_followup_proposal_cli_blocks_after_real_remote_proxy_chain_exhaustion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    _append_budget_rows(
        run_root,
        {
            "execution_scope": "remote_task",
            "executed": True,
            "collect_judge_completed": False,
            "iteration_id": "iteration_002",
            "canonical_or_diagnostic": "diagnostic",
            "consecutive_proxy_or_diagnostic_tasks_used": 2,
            "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": 1,
            "remote_tasks_total_used": 2,
        },
        {
            "execution_scope": "remote_collect_judge",
            "executed": True,
            "collect_judge_completed": True,
            "iteration_id": "iteration_002",
            "canonical_or_diagnostic": "diagnostic",
            "consecutive_proxy_or_diagnostic_tasks_used": 2,
            "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": 1,
            "remote_tasks_total_used": 2,
        },
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-followup-proposal",
            "--mission-id",
            mission_id,
            "--actor",
            "mission_remote_loop",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert payload["written"] is False
    assert payload["candidate_count"] == 0
    assert payload["latest_consecutive_proxy_or_diagnostic_tasks_used"] == 2
    assert payload["latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 1
    assert "proxy_chain_budget_exhausted" in payload["reasons"]


def test_mission_remote_launch_packet_resolves_b14_profile_and_stays_ready_after_two_local_rehearsals(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    result = assess_mission_remote_launch_packet(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert result["status"] == "ready"
    assert result["resolved_workplace_profile"]["status"] == "resolved"
    assert result["resolved_workplace_profile"]["profile"] == "EXAMPLE_WORKER_A_B14"
    assert result["resolved_workplace_profile"]["host"] == "EXAMPLE_WORKER_A"
    assert result["resolved_workplace_profile"]["remote_workdir"] == "/srv/example-workspaces-b/B14_agent_handoff_20260521"
    assert result["resolved_workplace_profile"]["remote_root"] == "/srv/example-workspaces-b/B14_agent_handoff_20260521/runs"
    assert result["gate"]["prior_consecutive_proxy_or_diagnostic_tasks"] == 0
    assert result["gate"]["prior_local_rehearsal_consecutive_proxy_or_diagnostic_tasks"] == 2
    assert result["launch_record"]["status"] == "command_packet_ready"
    assert result["remote_dispatch_task_id"] == ""
    assert result["live_launch_ready"] is False
    assert result["live_launch_blockers"] == ["remote_task_id_missing"]
    assert result["preflight"]["live_launch_ready"] is False
    assert result["launch_record"]["live_launch_ready"] is False
    assert result["launch_packet"]["worker"] == "materialized_execution"
    assert result["launch_packet"]["remote_dispatch_task_id"] == ""
    assert result["launch_packet"]["command_task_id"] == "__REMOTE_TASK_ID_REQUIRED__"
    assert "--host" in result["launch_packet"]["argv"]
    assert "run-remote" in result["launch_packet"]["argv"]
    assert "__REMOTE_TASK_ID_REQUIRED__" in result["launch_packet"]["argv"]
    assert result["launch_packet"]["argv"][4] == "__REMOTE_TASK_ID_REQUIRED__"


def test_mission_remote_launch_packet_blocks_when_real_remote_proxy_chain_is_exhausted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    _append_budget_rows(
        run_root,
        {
            "execution_scope": "remote_task",
            "executed": True,
            "canonical_or_diagnostic": "diagnostic",
            "consecutive_proxy_or_diagnostic_tasks_used": 1,
            "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": 0,
            "remote_tasks_total_used": 1,
        },
        {
            "execution_scope": "remote_task",
            "executed": True,
            "canonical_or_diagnostic": "diagnostic",
            "consecutive_proxy_or_diagnostic_tasks_used": 2,
            "local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used": 0,
            "remote_tasks_total_used": 2,
        },
    )

    result = assess_mission_remote_launch_packet(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert result["status"] == "blocked"
    assert "proxy_chain_budget_exhausted" in result["reason_codes"]
    assert result["launch_packet"] == {}
    assert result["launch_record"]["status"] == "blocked"


def test_mission_remote_launch_packet_requires_exact_profile_resolution_and_does_not_guess_alias() -> None:
    task = _task_payload()
    task["workplace_profile"] = {
        "host": "EXAMPLE_WORKER_A",
        "remote_root": "/srv/example-workspaces-b/B14_agent_handoff_20260521/runs",
        "remote_workdir": "/srv/example-workspaces-b/B14_agent_handoff_20260521",
    }

    result = assess_mission_remote_launch_packet(
        _mission_payload(),
        task,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert result["status"] == "blocked"
    assert result["resolved_workplace_profile"]["status"] == "blocked"
    assert "workplace_profile_resolution_required" in result["reason_codes"]
    assert result["launch_packet"] == {}


def test_mission_remote_launch_packet_marks_live_launch_ready_when_remote_task_binding_is_present() -> None:
    task = _task_payload()
    task["remote_task_id"] = "B14-L74-remote-task"

    result = assess_mission_remote_launch_packet(
        _mission_payload(),
        task,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert result["status"] == "ready"
    assert result["remote_dispatch_task_id"] == "B14-L74-remote-task"
    assert result["live_launch_ready"] is True
    assert result["live_launch_blockers"] == []
    assert result["preflight"]["remote_dispatch_task_id"] == "B14-L74-remote-task"
    assert result["preflight"]["live_launch_ready"] is True
    assert result["launch_record"]["remote_dispatch_task_id"] == "B14-L74-remote-task"
    assert result["launch_record"]["live_launch_ready"] is True
    assert result["launch_packet"]["remote_dispatch_task_id"] == "B14-L74-remote-task"
    assert result["launch_packet"]["command_task_id"] == "B14-L74-remote-task"
    assert result["launch_packet"]["argv"][4] == "B14-L74-remote-task"


def test_mission_remote_launch_packet_surfaces_matching_remote_transport_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    task = _task_payload()
    task["remote_task_id"] = "B14-L74-remote-task"
    preflight_path = _write_last_remote_preflight(
        tmp_path,
        {
            "ok": True,
            "checked_at": "2026-06-12T21:18:44+08:00",
            "host": "EXAMPLE_WORKER_A",
            "remote_workdir": "/srv/example-workspaces-b/B14_agent_handoff_20260521",
            "remote_root": "/srv/example-workspaces-b/B14_agent_handoff_20260521/runs",
            "profile_id": "EXAMPLE_WORKER_A__p7735__example_project__uid1000",
            "transport": {
                "status": "ok",
                "reason_code": "transport_ok",
                "automation_retry_safe": True,
                "recovery_action": "continue_remote_collect_judge_flow",
                "ssh_user": "agent-user",
                "ssh_port": "7735",
            },
        },
    )
    monkeypatch.setattr(
        "solution_agent.continuation.mission_remote_loop._last_remote_preflight_path",
        lambda: preflight_path,
    )

    result = assess_mission_remote_launch_packet(
        _mission_payload(),
        task,
        run_root=tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"],
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert result["live_launch_ready"] is True
    assert result["preflight"]["latest_remote_transport"]["status"] == "ok"
    assert result["preflight"]["latest_remote_transport"]["matched_workplace"] is True
    assert result["launch_record"]["latest_remote_transport"]["reason_code"] == "transport_ok"
    assert result["latest_remote_transport"]["automation_retry_safe"] is True


def test_mission_remote_status_surfaces_latest_remote_transport_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_id = _mission_payload()["mission_id"]
    run_root = tmp_path / "outputs" / "night_runs" / mission_id
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    preflight_path = _write_last_remote_preflight(
        tmp_path,
        {
            "ok": False,
            "checked_at": "2026-06-12T21:15:20+08:00",
            "host": "EXAMPLE_WORKER_A",
            "remote_workdir": "/srv/example-workspaces-b/B14_agent_handoff_20260521",
            "remote_root": "/srv/example-workspaces-b/B14_agent_handoff_20260521/runs",
            "transport": {
                "status": "local_transport_permission_denied",
                "reason_code": "ssh_connect_permission_denied_from_current_context",
                "automation_retry_safe": False,
                "recovery_action": "run the generated launch packet from a context with network/SSH permission",
            },
        },
    )
    monkeypatch.setattr(
        "solution_agent.continuation.mission_remote_loop._last_remote_preflight_path",
        lambda: preflight_path,
    )

    payload = summarize_local_mission_remote_rehearsal(run_root)

    assert payload["latest_remote_transport_status"] == "local_transport_permission_denied"
    assert payload["latest_remote_transport_reason_code"] == "ssh_connect_permission_denied_from_current_context"
    assert payload["latest_remote_transport_automation_retry_safe"] is False
    assert payload["latest_remote_transport"]["matched_workplace"] is True


def test_mission_remote_launch_packet_cli_writes_run_scoped_artifacts(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_path = tmp_path / "mission.json"
    task_path = tmp_path / "task.json"
    project_path = tmp_path / "project.json"
    discussion_path = tmp_path / "discussion.json"
    mission_path.write_text(json.dumps(_mission_payload(), indent=2), encoding="utf-8")
    task_path.write_text(json.dumps(_task_payload(), indent=2), encoding="utf-8")
    project_path.write_text(json.dumps(_project_knowledge(), indent=2), encoding="utf-8")
    discussion_path.write_text(json.dumps(_discussion_payload(), indent=2), encoding="utf-8")

    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    output_dir = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"] / "launch_packet_001"
    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-launch-packet",
            "--mission-path",
            str(mission_path),
            "--task-path",
            str(task_path),
            "--output-dir",
            str(output_dir),
            "--run-root",
            str(run_root),
            "--project-knowledge-path",
            str(project_path),
            "--discussion-payload-path",
            str(discussion_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "written"
    assert payload["launch_record"]["status"] == "command_packet_ready"
    assert payload["live_launch_ready"] is False
    assert payload["live_launch_blockers"] == ["remote_task_id_missing"]
    assert Path(payload["mission_gate_report_path"]).exists()
    assert Path(payload["remote_launch_preflight_path"]).exists()
    assert Path(payload["launch_record_path"]).exists()
    assert Path(payload["launch_packet_path"]).exists()
    assert Path(payload["launch_packet_markdown_path"]).exists()


def test_mission_remote_launch_packet_cli_accepts_remote_task_id_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_path = tmp_path / "mission.json"
    task_path = tmp_path / "task.json"
    project_path = tmp_path / "project.json"
    discussion_path = tmp_path / "discussion.json"
    mission_path.write_text(json.dumps(_mission_payload(), indent=2), encoding="utf-8")
    task_path.write_text(json.dumps(_task_payload(), indent=2), encoding="utf-8")
    project_path.write_text(json.dumps(_project_knowledge(), indent=2), encoding="utf-8")
    discussion_path.write_text(json.dumps(_discussion_payload(), indent=2), encoding="utf-8")

    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    output_dir = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"] / "launch_packet_002"
    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-launch-packet",
            "--mission-path",
            str(mission_path),
            "--task-path",
            str(task_path),
            "--output-dir",
            str(output_dir),
            "--run-root",
            str(run_root),
            "--project-knowledge-path",
            str(project_path),
            "--discussion-payload-path",
            str(discussion_path),
            "--remote-task-id",
            "B14-L74-remote-task",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["live_launch_ready"] is True
    assert payload["remote_dispatch_task_id"] == "B14-L74-remote-task"
    assert payload["launch_packet"]["command_task_id"] == "B14-L74-remote-task"
    assert payload["launch_packet"]["argv"][4] == "B14-L74-remote-task"


def test_execute_mission_remote_launch_once_records_pending_collect_and_blocks_next_launch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    task = _task_payload()
    task["remote_task_id"] = "B14-L74-remote-task"

    captured: dict[str, object] = {}

    def _fake_run_remote(
        task_id: str,
        *,
        host: str,
        remote_root: str,
        worker: str,
        remote_workdir: str | None = None,
        detach: bool = False,
        execution_phase: str = "full",
        **_: object,
    ) -> dict[str, object]:
        captured.update(
            {
                "task_id": task_id,
                "host": host,
                "remote_root": remote_root,
                "worker": worker,
                "remote_workdir": remote_workdir,
                "detach": detach,
                "execution_phase": execution_phase,
            }
        )
        return {"status": "running", "run_id": "remote_run_001"}

    monkeypatch.setattr("solution_agent.orchestrator.core.run_remote", _fake_run_remote)

    result = execute_mission_remote_launch_once(
        _mission_payload(),
        task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert result["status"] == "launched"
    assert result["live_launch_ready"] is True
    assert result["launch_packet"]["command_task_id"] == "B14-L74-remote-task"
    assert captured["task_id"] == "B14-L74-remote-task"
    assert captured["host"] == "EXAMPLE_WORKER_A"
    assert captured["remote_root"] == "/srv/example-workspaces-b/B14_agent_handoff_20260521/runs"
    assert captured["worker"] == "materialized_execution"
    registered_task = tmp_path / "tasks" / "pending" / "B14-L74-remote-task" / "task.md"
    assert registered_task.exists()
    registered_text = registered_task.read_text(encoding="utf-8")
    assert "worker: materialized_execution" in registered_text
    assert "profile: EXAMPLE_WORKER_A_B14" in registered_text
    assert "remote_root: /srv/example-workspaces-b/B14_agent_handoff_20260521/runs" in registered_text
    assert result["task_registration"]["status"] == "created"
    assert result["task_registration"]["task_id"] == "B14-L74-remote-task"


def test_execute_mission_remote_launch_once_respects_remote_task_id_override_in_packet_and_launch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    captured: dict[str, object] = {}

    def _fake_run_remote(
        task_id: str,
        *,
        host: str,
        remote_root: str,
        worker: str,
        remote_workdir: str | None = None,
        detach: bool = False,
        execution_phase: str = "full",
        **_: object,
    ) -> dict[str, object]:
        captured["task_id"] = task_id
        return {"status": "running", "run_id": "remote_run_002"}

    monkeypatch.setattr("solution_agent.orchestrator.core.run_remote", _fake_run_remote)

    result = execute_mission_remote_launch_once(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
        remote_task_id="B14-L74-remote-task",
    )

    assert result["status"] == "launched"
    assert result["remote_dispatch_task_id"] == "B14-L74-remote-task"
    assert result["live_launch_ready"] is True
    assert result["launch_packet"]["command_task_id"] == "B14-L74-remote-task"
    assert captured["task_id"] == "B14-L74-remote-task"
    registered_task = tmp_path / "tasks" / "pending" / "B14-L74-remote-task" / "task.md"
    assert registered_task.exists()
    assert "Logical task id: `B14-L74-local-rehearsal`" in registered_task.read_text(encoding="utf-8")

    summary = summarize_local_mission_remote_rehearsal(run_root)
    assert summary["latest_remote_collect_judge_pending"] is True
    assert summary["latest_pending_remote_iteration_id"] == "iteration_001"
    assert summary["latest_followup_proposal_status"] == "blocked"
    assert summary["latest_followup_reasons"] == ["prior_remote_collect_judge_incomplete"]

    followup = build_local_mission_remote_followup_candidates(
        run_root,
        actor="mission_remote_loop",
    )
    assert followup["status"] == "blocked"
    assert followup["reasons"] == ["prior_remote_collect_judge_incomplete"]
    assert followup["latest_pending_remote_iteration_id"] == "iteration_001"

    preflight = assess_local_mission_remote_preflight(
        _mission_payload(),
        _task_payload(),
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
        run_root=run_root,
    )
    assert preflight["gate"]["status"] == "block"
    assert "prior_remote_collect_judge_incomplete" in preflight["gate"]["reason_codes"]


def test_execute_mission_remote_launch_once_marks_transport_failure_without_consuming_remote_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    monkeypatch.setattr(
        "solution_agent.orchestrator.core.run_remote",
        lambda *args, **kwargs: {
            "launch_bundle": {"copy": {"returncode": 255, "stderr": "scp denied"}},
            "run": {"returncode": 255, "stderr": "ssh denied"},
        },
    )

    result = execute_mission_remote_launch_once(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
        remote_task_id="B14-L74-remote-task",
    )

    assert result["status"] == "launch_failed"
    assert result["launch_record"]["status"] == "launch_failed"
    assert result["launch_record"]["executed"] is False
    assert result["budget_update"]["executed"] is False
    assert result["budget_update"]["launch_status"] == "launch_failed"
    assert result["budget_update"]["remote_tasks_total_used"] == 0
    assert result["budget_update"]["consecutive_proxy_or_diagnostic_tasks_used"] == 0
    assert result["budget_update"]["next_action"] == "launch_failed"

    summary = summarize_local_mission_remote_rehearsal(run_root)
    assert summary["latest_remote_collect_judge_pending"] is False
    assert summary["latest_pending_remote_iteration_id"] == ""
    assert summary["latest_iteration_id"] == "iteration_003"
    assert summary["latest_actionable_iteration_id"] == "iteration_002"
    assert summary["latest_next_action"] == "continue"
    assert summary["latest_followup_proposal_status"] == "ready"

    followup = build_local_mission_remote_followup_candidates(
        run_root,
        actor="mission_remote_loop",
    )
    assert followup["status"] == "ready"
    assert followup["latest_iteration_id"] == "iteration_003"
    assert followup["latest_actionable_iteration_id"] == "iteration_002"
    assert followup["proposal_run_id"].endswith("iteration_002")
    assert followup["candidates"][0]["candidate_id"].endswith("iteration_002__bounded_probe")
    assert followup["candidates"][0]["source_council_id"].endswith("iteration_002")
    assert followup["candidate_count"] == 1
    assert followup["reasons"] == []


def test_finalize_mission_remote_collect_judge_clears_pending_state_and_writes_remote_outcome(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    task = _task_payload()
    task["remote_task_id"] = "B14-L74-remote-task"

    monkeypatch.setattr(
        "solution_agent.orchestrator.core.run_remote",
        lambda *args, **kwargs: {"status": "running", "run_id": "remote_run_001"},
    )
    launch = execute_mission_remote_launch_once(
        _mission_payload(),
        task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    collect_dir = tmp_path / "collected_remote_output"
    collect_dir.mkdir(parents=True, exist_ok=True)
    (collect_dir / "stdout.log").write_text("remote stdout\n", encoding="utf-8")
    (collect_dir / "stderr.log").write_text("", encoding="utf-8")
    (collect_dir / "bbox_predictions.jsonl").write_text(
        json.dumps({"image_id": "img-001", "bbox": [1, 2, 3, 4]}) + "\n",
        encoding="utf-8",
    )
    (collect_dir / "metrics.json").write_text(
        json.dumps({"execution_path": 1, "bbox_proxy_quality": 0.4}) + "\n",
        encoding="utf-8",
    )
    (collect_dir / "evidence_payload.jsonl").write_text(
        json.dumps({"image_id": "img-001", "evidence": "sidecar-present"}) + "\n",
        encoding="utf-8",
    )
    (collect_dir / "decision_summary.json").write_text(
        json.dumps({"status": "claim_limited_complete"}) + "\n",
        encoding="utf-8",
    )
    (collect_dir / "action_requests.json").write_text("[]\n", encoding="utf-8")

    result = finalize_mission_remote_collect_judge(
        _mission_payload(),
        task,
        run_root=run_root,
        iteration_id=Path(launch["iteration_dir"]).name,
        collect_output_dir=collect_dir,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
        sync_pull_result={"status": "ok"},
    )

    assert result["status"] == "written"
    assert result["judge_report"]["status"] == "artifact_complete"
    assert result["continuation_decision"]["next_action"] == "continue"
    assert Path(result["checkpoint_path"]).exists()
    assert Path(result["factual_memory_path"]).exists()
    assert Path(result["kb_update_proposal_dir"]).exists()

    summary = summarize_local_mission_remote_rehearsal(run_root)
    assert summary["latest_remote_collect_judge_pending"] is False
    assert summary["latest_pending_remote_iteration_id"] == ""
    assert summary["latest_next_action"] == "continue"
    assert summary["latest_followup_proposal_status"] == "ready"

    history = json.loads(
        CliRunner().invoke(
            cli,
            [
                "night-researcher",
                "mission-remote-history",
                "--mission-id",
                _mission_payload()["mission_id"],
                "--limit",
                "1",
            ],
        ).output
    )
    assert history["history"][0]["execution_scope"] == "remote_collect_judge"
    assert history["history"][0]["collect_judge_completed"] is True


def test_sync_finalize_mission_remote_iteration_closes_first_real_remote_loop_after_two_local_rehearsals(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    task = _task_payload()
    task["remote_task_id"] = "B14-L74-remote-task"

    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )
    run_local_mission_remote_rehearsal(
        _mission_payload(),
        _task_payload(),
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    monkeypatch.setattr(
        "solution_agent.orchestrator.core.run_remote",
        lambda *args, **kwargs: {"status": "running", "run_id": "remote_run_003"},
    )
    launch = execute_mission_remote_launch_once(
        _mission_payload(),
        task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    synced_dir = tmp_path / "synced_remote_output"
    synced_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("stdout.log", "remote stdout\n"),
        ("stderr.log", ""),
        ("bbox_predictions.jsonl", json.dumps({"image_id": "img-001", "bbox": [1, 2, 3, 4]}) + "\n"),
        ("metrics.json", json.dumps({"execution_path": 1, "bbox_proxy_quality": 0.4}) + "\n"),
        ("evidence_payload.jsonl", json.dumps({"image_id": "img-001", "evidence": "sidecar-present"}) + "\n"),
        ("decision_summary.json", json.dumps({"status": "claim_limited_complete"}) + "\n"),
        ("action_requests.json", "[]\n"),
    ):
        (synced_dir / name).write_text(payload, encoding="utf-8")

    monkeypatch.setattr(
        "solution_agent.orchestrator.core.sync_pull_remote",
        lambda task_id, host, remote_root: {
            "task_id": task_id,
            "remote": f"{host}:{remote_root}/{task_id}/_worker_out",
            "remote_artifacts": f"{host}:{remote_root}/{task_id}/artifacts",
            "local": str(synced_dir),
            "result": {"returncode": 0},
            "artifacts_result": {"returncode": 0},
        },
    )

    result = sync_finalize_mission_remote_iteration(
        _mission_payload(),
        task,
        run_root=run_root,
        iteration_id=Path(launch["iteration_dir"]).name,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert result["status"] == "written"
    assert result["collect_output_dir"] == str(synced_dir)
    assert result["collect_output_dir_resolution"]["status"] == "resolved"
    assert result["collect_output_dir_resolution"]["source"] == "worker_out"
    assert result["sync_pull_result"]["task_id"] == "B14-L74-remote-task"
    finalized = result["finalize_result"]
    assert finalized["continuation_decision"]["next_action"] == "continue"

    summary = summarize_local_mission_remote_rehearsal(run_root)
    assert summary["latest_remote_collect_judge_pending"] is False
    assert summary["latest_pending_remote_iteration_id"] == ""
    assert summary["latest_iteration_id"] == "iteration_003"
    assert summary["latest_actionable_iteration_id"] == "iteration_003"
    assert summary["latest_consecutive_proxy_or_diagnostic_tasks_used"] == 1
    assert summary["latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 2
    assert summary["latest_followup_proposal_status"] == "ready"

    followup = build_local_mission_remote_followup_candidates(run_root, actor="mission_remote_loop")
    assert followup["status"] == "ready"
    assert followup["latest_iteration_id"] == "iteration_003"
    assert followup["latest_actionable_iteration_id"] == "iteration_003"
    assert followup["latest_consecutive_proxy_or_diagnostic_tasks_used"] == 1
    assert followup["latest_local_rehearsal_consecutive_proxy_or_diagnostic_tasks_used"] == 2


def test_sync_finalize_mission_remote_iteration_uses_artifacts_subdir_when_worker_out_root_lacks_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    task = _task_payload()
    task["remote_task_id"] = "B14-L74-remote-task"

    monkeypatch.setattr(
        "solution_agent.orchestrator.core.run_remote",
        lambda *args, **kwargs: {"status": "running", "run_id": "remote_run_004"},
    )
    launch = execute_mission_remote_launch_once(
        _mission_payload(),
        task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    synced_dir = tmp_path / "synced_remote_output_with_artifacts"
    artifacts_dir = synced_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("stdout.log", "remote stdout\n"),
        ("stderr.log", ""),
        ("bbox_predictions.jsonl", json.dumps({"image_id": "img-001", "bbox": [1, 2, 3, 4]}) + "\n"),
        ("metrics.json", json.dumps({"execution_path": 1, "bbox_proxy_quality": 0.4}) + "\n"),
        ("evidence_payload.jsonl", json.dumps({"image_id": "img-001", "evidence": "sidecar-present"}) + "\n"),
        ("decision_summary.json", json.dumps({"status": "claim_limited_complete"}) + "\n"),
        ("action_requests.json", "[]\n"),
    ):
        (artifacts_dir / name).write_text(payload, encoding="utf-8")

    monkeypatch.setattr(
        "solution_agent.orchestrator.core.sync_pull_remote",
        lambda task_id, host, remote_root: {
            "task_id": task_id,
            "remote": f"{host}:{remote_root}/{task_id}/_worker_out",
            "remote_artifacts": f"{host}:{remote_root}/{task_id}/artifacts",
            "local": str(synced_dir),
            "result": {"returncode": 0},
            "artifacts_result": {"returncode": 0},
        },
    )

    result = sync_finalize_mission_remote_iteration(
        _mission_payload(),
        task,
        run_root=run_root,
        iteration_id=Path(launch["iteration_dir"]).name,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    assert result["collect_output_dir"] == str(artifacts_dir)
    assert result["collect_output_dir_resolution"]["status"] == "resolved"
    assert result["collect_output_dir_resolution"]["source"] == "worker_out_artifacts"
    assert result["finalize_result"]["judge_report"]["status"] == "artifact_complete"


def test_mission_remote_sync_finalize_cli_uses_launched_iteration_profile_and_artifacts_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    mission_path = tmp_path / "mission.json"
    task_path = tmp_path / "task.json"
    project_path = tmp_path / "project.json"
    discussion_path = tmp_path / "discussion.json"
    mission_path.write_text(json.dumps(_mission_payload(), indent=2), encoding="utf-8")
    task = _task_payload()
    task["remote_task_id"] = "B14-L74-remote-task"
    task_path.write_text(json.dumps(task, indent=2), encoding="utf-8")
    project_path.write_text(json.dumps(_project_knowledge(), indent=2), encoding="utf-8")
    discussion_path.write_text(json.dumps(_discussion_payload(), indent=2), encoding="utf-8")

    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    monkeypatch.setattr(
        "solution_agent.orchestrator.core.run_remote",
        lambda *args, **kwargs: {"status": "running", "run_id": "remote_run_006"},
    )
    launch = execute_mission_remote_launch_once(
        _mission_payload(),
        task,
        run_root=run_root,
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
    )

    synced_dir = tmp_path / "cli_synced_remote_output"
    artifacts_dir = synced_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in (
        ("stdout.log", "remote stdout\n"),
        ("stderr.log", ""),
        ("bbox_predictions.jsonl", json.dumps({"image_id": "img-cli", "bbox": [1, 2, 3, 4]}) + "\n"),
        ("metrics.json", json.dumps({"execution_path": 1, "bbox_proxy_quality": 0.4}) + "\n"),
        ("evidence_payload.jsonl", json.dumps({"image_id": "img-cli", "evidence": "sidecar-present"}) + "\n"),
        ("decision_summary.json", json.dumps({"status": "claim_limited_complete"}) + "\n"),
        ("action_requests.json", "[]\n"),
    ):
        (artifacts_dir / name).write_text(payload, encoding="utf-8")

    monkeypatch.setattr(
        "solution_agent.orchestrator.core.sync_pull_remote",
        lambda task_id, host, remote_root: {
            "task_id": task_id,
            "remote": f"{host}:{remote_root}/{task_id}/_worker_out",
            "remote_artifacts": f"{host}:{remote_root}/{task_id}/artifacts",
            "local": str(synced_dir),
            "result": {"returncode": 0},
            "artifacts_result": {"returncode": 0},
        },
    )

    result = CliRunner().invoke(
        cli,
        [
            "night-researcher",
            "mission-remote-sync-finalize",
            "--mission-path",
            str(mission_path),
            "--task-path",
            str(task_path),
            "--run-root",
            str(run_root),
            "--iteration-id",
            Path(launch["iteration_dir"]).name,
            "--project-knowledge-path",
            str(project_path),
            "--discussion-payload-path",
            str(discussion_path),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "written"
    assert payload["collect_output_dir"] == str(artifacts_dir)
    assert payload["collect_output_dir_resolution"]["source"] == "worker_out_artifacts"
    assert payload["sync_pull_result"]["task_id"] == "B14-L74-remote-task"
    assert payload["finalize_result"]["continuation_decision"]["next_action"] == "continue"


def test_mission_remote_preflight_blocks_third_real_remote_diagnostic_after_two_completed_remote_close_loops(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_root = tmp_path / "outputs" / "night_runs" / _mission_payload()["mission_id"]
    task = _task_payload()
    task["remote_task_id"] = "B14-L74-remote-task"

    monkeypatch.setattr(
        "solution_agent.orchestrator.core.run_remote",
        lambda *args, **kwargs: {"status": "running", "run_id": "remote_run_005"},
    )

    def _write_collect_contract(base: Path, suffix: str) -> Path:
        base.mkdir(parents=True, exist_ok=True)
        for name, payload in (
            ("stdout.log", f"remote stdout {suffix}\n"),
            ("stderr.log", ""),
            ("bbox_predictions.jsonl", json.dumps({"image_id": f"img-{suffix}", "bbox": [1, 2, 3, 4]}) + "\n"),
            ("metrics.json", json.dumps({"execution_path": 1, "bbox_proxy_quality": 0.4}) + "\n"),
            ("evidence_payload.jsonl", json.dumps({"image_id": f"img-{suffix}", "evidence": "sidecar-present"}) + "\n"),
            ("decision_summary.json", json.dumps({"status": "claim_limited_complete"}) + "\n"),
            ("action_requests.json", "[]\n"),
        ):
            (base / name).write_text(payload, encoding="utf-8")
        return base

    for index in (1, 2):
        launch = execute_mission_remote_launch_once(
            _mission_payload(),
            task,
            run_root=run_root,
            project_knowledge=_project_knowledge(),
            discussion_payload=_discussion_payload(),
        )
        collect_dir = _write_collect_contract(tmp_path / f"remote_collect_{index}", str(index))
        finalize_mission_remote_collect_judge(
            _mission_payload(),
            task,
            run_root=run_root,
            iteration_id=Path(launch["iteration_dir"]).name,
            collect_output_dir=collect_dir,
            project_knowledge=_project_knowledge(),
            discussion_payload=_discussion_payload(),
            sync_pull_result={"status": "ok", "cycle": index},
        )

    preflight = assess_local_mission_remote_preflight(
        _mission_payload(),
        _task_payload(),
        project_knowledge=_project_knowledge(),
        discussion_payload=_discussion_payload(),
        run_root=run_root,
    )

    assert preflight["gate"]["status"] == "block"
    assert "proxy_chain_budget_exhausted" in preflight["gate"]["reason_codes"]
    assert preflight["gate"]["prior_consecutive_proxy_or_diagnostic_tasks"] == 2
    assert preflight["gate"]["prior_local_rehearsal_consecutive_proxy_or_diagnostic_tasks"] == 0
