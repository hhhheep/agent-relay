from __future__ import annotations

from pathlib import Path

from solution_agent.orchestrator.paths import root


def continuation_root() -> Path:
    return root() / "continuation"


def continuation_task_inbox_root() -> Path:
    return root() / "tasks" / "inbox"


def continuation_task_inbox_proposed_root() -> Path:
    return continuation_task_inbox_root() / "_proposed"


def continuation_staging_dir() -> Path:
    return continuation_root() / "staging"


def continuation_staging_proposed_task_drafts_dir() -> Path:
    return continuation_staging_dir() / "proposed_task_drafts"


def continuation_staging_proposed_bundle_dir() -> Path:
    return continuation_task_inbox_proposed_root()


def continuation_proposed_bundle_dir(source_task_id: str, plan_id: str, draft_id: str) -> Path:
    safe_source = str(source_task_id).replace("..", "_").replace("/", "_")
    safe_plan = str(plan_id).replace("..", "_").replace("/", "_")
    safe_draft = str(draft_id).replace("..", "_").replace("/", "_")
    return continuation_task_inbox_proposed_root() / safe_source / safe_plan / safe_draft


def continuation_proposed_root(source_task_id: str, plan_id: str) -> Path:
    safe_source = str(source_task_id).replace("..", "_").replace("/", "_")
    safe_plan = str(plan_id).replace("..", "_").replace("/", "_")
    return continuation_task_inbox_proposed_root() / safe_source / safe_plan


def is_active_inbox_path_forbidden(path: Path) -> bool:
    inbox_root = continuation_task_inbox_root().resolve()
    proposed_root = continuation_task_inbox_proposed_root().resolve()
    candidate = path.resolve()

    if candidate == inbox_root:
        return False

    inbox_prefix = str(inbox_root) + str(Path.sep)
    if not str(candidate).startswith(inbox_prefix):
        return False

    if candidate == proposed_root:
        return False
    return not str(candidate).startswith(str(proposed_root) + str(Path.sep))


def continuation_latest_dir() -> Path:
    return continuation_root() / "latest"


def continuation_audit_dir() -> Path:
    return continuation_root() / "audit"


def continuation_audit_events_path() -> Path:
    return continuation_audit_dir() / "events.jsonl"


def continuation_run_ledger_dir() -> Path:
    return continuation_root() / "run_ledger"


def continuation_run_ledger_path(run_id: str | None = None) -> Path:
    run_key = str((run_id or "default")).strip()
    if not run_key:
        run_key = "default"
    run_key = run_key.replace("..", "_").replace("/", "_").replace("\\", "_")
    if run_key == ".":
        run_key = "default"
    return continuation_run_ledger_dir() / f"{run_key}.jsonl"


def continuation_observations_dir() -> Path:
    return continuation_root() / "observations"


def continuation_observation_log_path(run_id: str | None = None) -> Path:
    run_key = str((run_id or "default")).strip()
    if not run_key:
        run_key = "default"
    run_key = run_key.replace("..", "_").replace("/", "_").replace("\\", "_")
    if run_key == ".":
        run_key = "default"
    return continuation_observations_dir() / f"{run_key}.jsonl"


def continuation_remote_execution_work_orders_dir() -> Path:
    return continuation_root() / "remote_execution_work_orders"


def continuation_launch_intents_dir() -> Path:
    return continuation_root() / "launch_intents"


def continuation_launch_preflight_dir() -> Path:
    return continuation_root() / "launch_preflight"


def continuation_manual_launch_records_dir() -> Path:
    return continuation_root() / "manual_launch_records"


def continuation_remote_collect_results_dir() -> Path:
    return continuation_root() / "remote_collect_results"


def continuation_remote_result_interpretations_dir() -> Path:
    return continuation_root() / "remote_result_interpretations"


def solution_continuation_dir() -> Path:
    return root() / ".solution" / "continuation"


def continuation_knowledge_pack_dir() -> Path:
    return continuation_root() / "knowledge_packs"


def continuation_knowledge_pack_manifest_dir() -> Path:
    return continuation_knowledge_pack_dir() / "manifests"


def continuation_knowledge_update_dir() -> Path:
    return continuation_root() / "knowledge_updates"


def continuation_knowledge_update_proposed_dir() -> Path:
    return continuation_knowledge_update_dir() / "_proposed"


def _safe_knowledge_update_id(value: str | int | None) -> str:
    return str(value or "unknown_kb_update").replace("..", "_").replace("/", "_").replace("\\", "_")


def continuation_knowledge_update_proposal_dir(proposal_id: str | int | None) -> Path:
    return continuation_knowledge_update_proposed_dir() / _safe_knowledge_update_id(proposal_id)


def continuation_api_council_dir() -> Path:
    return continuation_root() / "api_council"


def continuation_api_council_request_dir() -> Path:
    return continuation_api_council_dir() / "requests"


def continuation_api_council_response_dir() -> Path:
    return continuation_api_council_dir() / "responses"


def _safe_council_id(value: str | int) -> str:
    return str(value).replace("..", "_").replace("/", "_").replace("\\", "_")


def continuation_api_council_case_dir(council_id: str | None) -> Path:
    safe_id = _safe_council_id(str(council_id or "").strip()) or "unknown_council"
    return continuation_api_council_dir() / safe_id


def continuation_api_council_request_path(council_id: str | None) -> Path:
    return continuation_api_council_case_dir(council_id) / "request.json"


def continuation_api_council_response_path(council_id: str | None) -> Path:
    return continuation_api_council_case_dir(council_id) / "response.json"


def continuation_api_council_summary_path(council_id: str | None) -> Path:
    return continuation_api_council_case_dir(council_id) / "summary.json"


def continuation_api_adapter_dir() -> Path:
    return continuation_root() / "api_adapter"


def _safe_invocation_id(value: str | int) -> str:
    return str(value).replace("..", "_").replace("/", "_").replace("\\", "_")


def continuation_api_adapter_case_dir(invocation_id: str | None) -> Path:
    safe_invocation_id = _safe_invocation_id(invocation_id or "unknown_invocation")
    return continuation_api_adapter_dir() / safe_invocation_id


def continuation_api_adapter_invocation_path(invocation_id: str | None) -> Path:
    return continuation_api_adapter_case_dir(invocation_id) / "invocation.json"


def continuation_api_adapter_mock_response_path(invocation_id: str | None) -> Path:
    return continuation_api_adapter_case_dir(invocation_id) / "mock_response.json"


def continuation_api_adapter_validated_response_path(invocation_id: str | None) -> Path:
    return continuation_api_adapter_case_dir(invocation_id) / "validated_response.json"


def continuation_proposal_candidates_dir() -> Path:
    return continuation_root() / "proposal_candidates"


def _safe_proposal_run_id(value: str | int) -> str:
    return str(value).replace("..", "_").replace("/", "_").replace("\\", "_")


def continuation_proposal_candidates_run_dir(proposal_run_id: str | None) -> Path:
    safe_id = _safe_proposal_run_id(str(proposal_run_id or "").strip()) or "unknown_proposal"
    return continuation_proposal_candidates_dir() / safe_id


def continuation_proposal_candidates_path(proposal_run_id: str | None) -> Path:
    return continuation_proposal_candidates_run_dir(proposal_run_id) / "candidates.json"


def continuation_proposal_candidates_summary_path(proposal_run_id: str | None) -> Path:
    return continuation_proposal_candidates_run_dir(proposal_run_id) / "summary.json"


def continuation_staged_proposal_candidates_dir() -> Path:
    return continuation_root() / "staged_proposal_candidates"


def continuation_staged_proposal_candidates_run_dir(proposal_run_id: str | None) -> Path:
    safe_id = _safe_proposal_run_id(str(proposal_run_id or "").strip()) or "unknown_proposal"
    return continuation_staged_proposal_candidates_dir() / safe_id


def continuation_staged_proposal_candidate_dir(
    proposal_run_id: str | None,
    candidate_id: str | None,
) -> Path:
    safe_candidate_id = str(candidate_id or "unknown_candidate").replace("..", "_").replace("/", "_").replace("\\", "_")
    return continuation_staged_proposal_candidates_run_dir(proposal_run_id) / safe_candidate_id


def continuation_staged_proposal_candidate_report_path(
    proposal_run_id: str | None,
    candidate_id: str | None,
) -> Path:
    return continuation_staged_proposal_candidate_dir(
        proposal_run_id,
        candidate_id,
    ) / "staging_report.json"


def continuation_night_auto_dry_run_root(mission_id: str | None = None) -> Path:
    safe_mission = str(mission_id or "unknown_mission").replace("..", "_").replace("/", "_").replace("\\", "_")
    return root() / "outputs" / "night_runs" / safe_mission / "dry_run"


def continuation_night_auto_dry_run_dir(
    mission_id: str | None,
    proposal_run_id: str | None = None,
    candidate_id: str | None = None,
) -> Path:
    safe_proposal_run_id = str(proposal_run_id or "unknown_run").replace("..", "_").replace("/", "_").replace("\\", "_")
    safe_candidate_id = str(candidate_id or "unknown_candidate").replace("..", "_").replace("/", "_").replace("\\", "_")
    return continuation_night_auto_dry_run_root(mission_id) / safe_proposal_run_id / safe_candidate_id


def continuation_night_auto_work_order_path(
    mission_id: str | None,
    proposal_run_id: str | None,
    candidate_id: str | None,
    work_order_id: str | None = None,
) -> Path:
    suffix = str(work_order_id or "night_auto_work_order").replace("..", "_").replace("/", "_").replace("\\", "_")
    return continuation_night_auto_dry_run_dir(mission_id, proposal_run_id, candidate_id) / f"{suffix}.json"


def continuation_night_auto_pending_root(mission_id: str | None = None) -> Path:
    safe_mission = str(mission_id or "unknown_mission").replace("..", "_").replace("/", "_").replace("\\", "_")
    return root() / "outputs" / "night_runs" / safe_mission / "night_auto_pending"


def continuation_night_auto_pending_dir(
    mission_id: str | None,
    proposal_run_id: str | None = None,
    candidate_id: str | None = None,
) -> Path:
    safe_proposal_run_id = str(proposal_run_id or "unknown_run").replace("..", "_").replace("/", "_").replace("\\", "_")
    safe_candidate_id = str(candidate_id or "unknown_candidate").replace("..", "_").replace("/", "_").replace("\\", "_")
    return continuation_night_auto_pending_root(mission_id) / safe_proposal_run_id / safe_candidate_id


def continuation_night_auto_pending_artifact_path(
    mission_id: str | None,
    proposal_run_id: str | None,
    candidate_id: str | None,
    pending_artifact_id: str | None = None,
) -> Path:
    suffix = str(pending_artifact_id or "night_auto_pending_artifact").replace("..", "_").replace("/", "_").replace("\\", "_")
    return continuation_night_auto_pending_dir(mission_id, proposal_run_id, candidate_id) / f"{suffix}.json"


def continuation_night_auto_scheduler_dry_run_root(mission_id: str | None = None) -> Path:
    safe_mission = str(mission_id or "unknown_mission").replace("..", "_").replace("/", "_").replace("\\", "_")
    return root() / "outputs" / "night_runs" / safe_mission / "scheduler_dry_run"


def continuation_night_auto_scheduler_dry_run_report_path(mission_id: str | None = None) -> Path:
    return continuation_night_auto_scheduler_dry_run_root(mission_id) / "night_auto_scheduler_explain_report.json"


def continuation_night_auto_local_execution_root(mission_id: str | None = None) -> Path:
    safe_mission = str(mission_id or "unknown_mission").replace("..", "_").replace("/", "_").replace("\\", "_")
    return root() / "outputs" / "night_runs" / safe_mission / "local_execution"


def continuation_night_auto_local_execution_dir(
    mission_id: str | None,
    proposal_run_id: str | None = None,
    candidate_id: str | None = None,
    execution_task: str | None = None,
) -> Path:
    safe_proposal_run_id = str(proposal_run_id or "unknown_run").replace("..", "_").replace("/", "_").replace("\\", "_")
    safe_candidate_id = str(candidate_id or "unknown_candidate").replace("..", "_").replace("/", "_").replace("\\", "_")
    safe_task = str(execution_task or "unknown_task").replace("..", "_").replace("/", "_").replace("\\", "_")
    return continuation_night_auto_local_execution_root(mission_id) / safe_proposal_run_id / safe_candidate_id / safe_task


def continuation_night_auto_local_execution_report_path(
    mission_id: str | None,
    proposal_run_id: str | None,
    candidate_id: str | None,
    execution_task: str,
    report_name: str | None = None,
) -> Path:
    safe_name = str(report_name or "local_execution_report").replace("..", "_").replace("/", "_").replace("\\", "_")
    return continuation_night_auto_local_execution_dir(
        mission_id,
        proposal_run_id,
        candidate_id,
        execution_task,
    ) / f"{safe_name}.json"


def continuation_night_auto_morning_brief_dir(mission_id: str | None = None) -> Path:
    safe_mission = str(mission_id or "unknown_mission").replace("..", "_").replace("/", "_").replace("\\", "_")
    return root() / "outputs" / "night_runs" / safe_mission / "morning_brief"


def continuation_night_auto_morning_brief_path(
    mission_id: str | None = None,
    *,
    suffix: str = "md",
) -> Path:
    safe_suffix = str(suffix or "md").replace("..", "_").replace("/", "_").replace("\\", "_")
    return continuation_night_auto_morning_brief_dir(mission_id) / f"morning_brief.{safe_suffix}"


def continuation_research_council_dir() -> Path:
    return continuation_root() / "research_council"


def _safe_research_council_run_id(value: str | int | None) -> str:
    return str(value or "unknown_research_council_run").replace("..", "_").replace("/", "_").replace("\\", "_")


def continuation_research_council_run_dir(run_id: str | int | None) -> Path:
    return continuation_research_council_dir() / _safe_research_council_run_id(run_id)


def continuation_research_council_flow_path(run_id: str | int | None) -> Path:
    return continuation_research_council_run_dir(run_id) / "flow.json"


def continuation_research_council_run_path(run_id: str | int | None) -> Path:
    return continuation_research_council_run_dir(run_id) / "run.json"


def continuation_research_council_summary_path(run_id: str | int | None) -> Path:
    return continuation_research_council_run_dir(run_id) / "council_summary.json"


def continuation_research_sessions_dir() -> Path:
    return continuation_root() / "research_sessions"


def continuation_project_dialogue_dir() -> Path:
    return continuation_root() / "project_dialogue"


def _safe_project_dialogue_session_id(value: str | int | None) -> str:
    return str(value or "unknown_project_dialogue").replace("..", "_").replace("/", "_").replace("\\", "_")


def continuation_project_dialogue_session_dir(session_id: str | int | None) -> Path:
    return continuation_project_dialogue_dir() / "sessions" / _safe_project_dialogue_session_id(session_id)


def continuation_research_provider_smoke_dir() -> Path:
    return continuation_root() / "research_provider_smoke"


def continuation_research_provider_smoke_run_dir(smoke_id: str | int | None) -> Path:
    safe_id = str(smoke_id or "unknown_provider_smoke").replace("..", "_").replace("/", "_").replace("\\", "_")
    return continuation_research_provider_smoke_dir() / safe_id


def continuation_research_external_safe_dir() -> Path:
    return continuation_root() / "research_external_safe"


def continuation_research_external_safe_run_dir(run_id: str | int | None) -> Path:
    safe_id = str(run_id or "unknown_external_safe_run").replace("..", "_").replace("/", "_").replace("\\", "_")
    return continuation_research_external_safe_dir() / safe_id


def _safe_research_session_id(value: str | int | None) -> str:
    return str(value or "unknown_research_session").replace("..", "_").replace("/", "_").replace("\\", "_")


def continuation_research_session_dir(session_id: str | int | None) -> Path:
    return continuation_research_sessions_dir() / _safe_research_session_id(session_id)


def continuation_research_session_live_preflight_dir(session_id: str | int | None) -> Path:
    return continuation_research_session_dir(session_id) / "live_preflight"


def continuation_research_session_live_calls_dir(session_id: str | int | None) -> Path:
    return continuation_research_session_dir(session_id) / "live_calls"


def continuation_research_session_live_call_dir(
    session_id: str | int | None,
    request_id: str | int | None,
) -> Path:
    safe_request_id = str(request_id or "unknown_request").replace("..", "_").replace("/", "_").replace("\\", "_")
    return continuation_research_session_live_calls_dir(session_id) / safe_request_id


def continuation_source_repair_dir() -> Path:
    return continuation_root() / "source_repair"


def _safe_source_repair_plan_id(value: str | int | None) -> str:
    return str(value or "unknown_source_repair_plan").replace("..", "_").replace("/", "_").replace("\\", "_")


def continuation_source_repair_plan_dir(plan_id: str | int | None) -> Path:
    return continuation_source_repair_dir() / _safe_source_repair_plan_id(plan_id)
