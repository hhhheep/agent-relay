from __future__ import annotations

import os
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

import click

from .agent.loop import patch_workspace_file, run_task, write_workspace_file
from .context_index import build_context_index, context_index_status, documents_path, query_context_index, snapshot_remote_workplace, write_context_documents
from .continuation import (
    bounded_validation_bucket,
    manual_launch,
    mission_remote_loop,
    night_auto_morning_brief,
    night_auto_local_execution,
    project_dialogue,
    project_knowledge_ingestion,
    research_deliberation,
    self_repair_rehearsal,
    source_repair,
    three_agent_flow,
)
from .continuation.night_researcher import load_research_mission
from .continuation.paths import continuation_night_auto_local_execution_root
from .orchestrator import core as lab
from .orchestrator.memory import ensure_local_memory_files
from .orchestrator.paths import codex_worker_profile_path, state_dir
from .remote_materializer.api_metrics import summarize_api_metrics
from .registry.db import init_registry_db
from .tools.registry_tool import register_tool_directory
from .tools.search_tools import probe_search_backends, safe_search
from .utils.paths import project_root
from .web import serve_dashboard


def _echo(message: str = "") -> None:
    click.echo(message)


def _governance_table_rows(text: str):
    lines = text.splitlines()
    current_header = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            current_header = []
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        if "Rule" in " ".join(cells) and "Tests" in cells:
            current_header = cells
            continue
        if current_header and len(cells) == len(current_header):
            yield dict(zip(current_header, cells))


def lint_policy_governance_matrix(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    rows = list(_governance_table_rows(text))
    blocking_rows = [
        row
        for row in rows
        if row.get("Blocks", "").strip().lower() == "yes"
        or row.get("Blocks", "").strip().lower().startswith("yes ")
        or row.get("Blocks", "").strip().lower().startswith("yes for")
        or row.get("Blocks", "").strip().lower().startswith("yes unless")
    ]
    errors = []
    for row in blocking_rows:
        label = row.get("Rule ID") or row.get("Rule ID / Reason") or row.get("Rule") or str(row)
        required_fields = ["Tests"]
        if "Layer" in row:
            required_fields.append("Layer")
        if "Owner" in row:
            required_fields.append("Owner")
        if "Severity" in row:
            required_fields.append("Severity")
        if "Reason" in row:
            required_fields.append("Reason")
        for field in required_fields:
            if not row.get(field, "").strip():
                errors.append({"rule": label, "field": field, "error": "missing_required_governance_field"})
    return {
        "schema_version": 1,
        "ok": not errors,
        "path": str(path),
        "rules_checked": len(blocking_rows),
        "errors": errors,
    }


@click.group()
def cli() -> None:
    """CLI entrypoint for solution-agent."""


@cli.command("init-db")
def init_db_command() -> None:
    db_path = init_registry_db()
    _echo(f"[solution-agent] registry db ready: {db_path}")


@cli.command("run")
@click.option("--task", required=True, type=str, help="Task description for this run.")
@click.option("--workspace", required=True, type=str, help="Workspace name under workspaces/.")
@click.option("--command", default=None, type=str, help="Optional initial command to run.")
@click.option("--report", "report_path", default=None, type=click.Path(), help="Optional Markdown report path.")
@click.option("--approve-risky", is_flag=True, help="Allow commands that are marked approval-required by policy.")
def run_command(
    task: str,
    workspace: str,
    command: Optional[str],
    report_path: Optional[str],
    approve_risky: bool,
) -> None:
    result = run_task(
        task=task,
        workspace_name=workspace,
        initial_command=command,
        report_path=report_path,
        approve_risky=approve_risky,
    )
    _echo(f"[solution-agent] run id: {result['run_id']}")
    _echo(f"[solution-agent] workspace: {result['workspace']}")
    _echo(f"[solution-agent] report: {result['report_path']}")
    _echo(f"[solution-agent] summary: {result['summary']}")


@cli.command("register-tool")
@click.argument("tool_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True))
def register_tool_command(tool_dir: str) -> None:
    record = register_tool_directory(Path(tool_dir))
    _echo(json.dumps(record, indent=2))


@cli.group("tools")
def tools_group() -> None:
    """Deterministic local tool wrappers."""


@tools_group.command("probe-search")
def tools_probe_search_command() -> None:
    result = probe_search_backends(project_root(), write_state=True)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@tools_group.command("search")
@click.argument("pattern")
@click.option("--root", "search_root", default=".", type=str, help="Root under the project to search.")
@click.option("--backend", default="auto", type=click.Choice(["auto", "rg", "git_grep", "powershell_select_string", "python_fallback"]))
@click.option("--max-results", default=None, type=int)
def tools_search_command(pattern: str, search_root: str, backend: str, max_results: Optional[int]) -> None:
    result = safe_search(project_root(), pattern, root=search_root, backend=backend, max_results=max_results)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.group("night-researcher")
def night_researcher_group() -> None:
    """Mission-bounded Night Researcher local helpers."""


@night_researcher_group.command("morning-brief")
@click.option("--mission-id", required=True, type=str, help="Mission id under outputs/night_runs/.")
@click.option("--print-md", is_flag=True, help="Print the generated Markdown brief after writing artifacts.")
def night_researcher_morning_brief_command(mission_id: str, print_md: bool) -> None:
    result = night_auto_morning_brief.write_morning_brief(mission_id=mission_id)
    _echo(json.dumps({key: value for key, value in result.items() if key != "summary"}, indent=2, ensure_ascii=False))
    if print_md:
        _echo("")
        _echo(Path(result["brief_path"]).read_text(encoding="utf-8"))


@night_researcher_group.command("b14-dry-run-pack")
@click.option("--mission-id", required=True, type=str, help="Mission id under outputs/night_runs/.")
@click.option("--proposal-run-id", required=True, type=str, help="Identifier for this dry-run rehearsal set.")
@click.option(
    "--cases-path",
    required=True,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a JSON list of B14 case payloads.",
)
@click.option("--actor", default="system", type=str, help="Actor to attribute to the rehearsal.")
@click.option("--write-summary/--no-write-summary", default=True, help="Write a rehearsal summary artifact.")
@click.option("--print-json", is_flag=True, help="Print the rehearsal result JSON payload.")
def night_researcher_b14_dry_run_pack_command(
    mission_id: str,
    proposal_run_id: str,
    cases_path: Path,
    actor: str,
    write_summary: bool,
    print_json: bool,
) -> None:
    mission = load_research_mission()
    if mission_id != mission.mission_id:
        raise click.ClickException("mission-id does not match fixture mission id")

    try:
        raw_cases = json.loads(cases_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"cases-path must contain valid JSON: {exc}") from exc

    if not isinstance(raw_cases, list):
        raise click.ClickException("cases-path must contain a JSON list")
    cases = [dict(item) for item in raw_cases if isinstance(item, dict)]
    if len(cases) != len(raw_cases):
        raise click.ClickException("cases-path list must contain only JSON objects")

    result = night_auto_local_execution.run_b14_local_execution_dry_run_pack(
        mission=mission,
        cases=cases,
        proposal_run_id=proposal_run_id,
        actor=actor,
        write_summary=write_summary,
    )
    if print_json:
        _echo(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("b14-remote-boundary-report")
@click.option("--mission-id", required=True, type=str, help="Mission id under outputs/night_runs/.")
@click.option("--proposal-run-id", required=True, type=str, help="Identifier for the B14 rehearsal set.")
@click.option(
    "--cases-path",
    required=False,
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Optional JSON list of B14 rehearsal case payloads.",
)
@click.option("--actor", default="system", type=str, help="Actor to attribute to the report.")
@click.option("--write-report/--no-write-report", default=True, help="Write the boundary report artifact.")
@click.option("--print-json", is_flag=True, help="Print the boundary report JSON payload.")
def night_researcher_b14_remote_boundary_report_command(
    mission_id: str,
    proposal_run_id: str,
    cases_path: Path | None,
    actor: str,
    write_report: bool,
    print_json: bool,
) -> None:
    mission = load_research_mission()
    if mission_id != mission.mission_id:
        raise click.ClickException("mission-id does not match fixture mission id")

    cases: list[dict] = []
    if cases_path is not None:
        try:
            raw_cases = json.loads(cases_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"cases-path must contain valid JSON: {exc}") from exc
        if not isinstance(raw_cases, list):
            raise click.ClickException("cases-path must contain a JSON list")
        cases = [dict(item) for item in raw_cases if isinstance(item, dict)]
        if len(cases) != len(raw_cases):
            raise click.ClickException("cases-path list must contain only JSON objects")
    else:
        summary = night_auto_local_execution.read_b14_pack_rehearsal_summary(
            mission_id=mission_id,
            proposal_run_id=proposal_run_id,
        )
        if not summary:
            raise click.ClickException(
                "no cases source available; provide --cases-path or run b14-dry-run-pack to generate a summary"
            )
        raw_cases = summary.get("cases")
        if not isinstance(raw_cases, list):
            raise click.ClickException("stored rehearsal summary missing cases list")
        cases = [dict(item) for item in raw_cases if isinstance(item, dict)]

    result = night_auto_local_execution.write_remote_boundary_readiness_report(
        mission_id=mission_id,
        proposal_run_id=proposal_run_id,
        cases=cases,
        actor=actor,
        write_report=write_report,
    )
    if print_json:
        _echo(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("manual-launch-preflight")
@click.option(
    "--launch-intent",
    "launch_intent_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a local launch intent JSON artifact.",
)
@click.option("--actor", required=True, type=str, help="Actor requesting the manual launch preflight.")
@click.option(
    "--operator-confirmed",
    is_flag=True,
    help="Confirm an operator reviewed the launch intent. This still does not run SSH.",
)
@click.option("--write/--no-write", default=True, help="Write local preflight and manual launch record artifacts.")
def night_researcher_manual_launch_preflight_command(
    launch_intent_path: Path,
    actor: str,
    operator_confirmed: bool,
    write: bool,
) -> None:
    if write:
        result = manual_launch.write_manual_launch_preflight(
            launch_intent_path,
            actor=actor,
            operator_confirmed=operator_confirmed,
        )
    else:
        result = manual_launch.assess_manual_launch_preflight(
            launch_intent_path,
            actor=actor,
            operator_confirmed=operator_confirmed,
        )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("manual-launch-command-packet")
@click.option(
    "--manual-launch-preflight",
    "manual_launch_preflight_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a local manual launch preflight JSON artifact.",
)
@click.option("--phase", "execution_phase", default="execute_run", type=click.Choice(["full", "prepare_code", "execute_run"]))
@click.option("--detach/--foreground", default=True, help="Render the future run-remote command with --detach.")
@click.option("--write/--no-write", default=True, help="Write the local command packet artifacts.")
def night_researcher_manual_launch_command_packet_command(
    manual_launch_preflight_path: Path,
    execution_phase: str,
    detach: bool,
    write: bool,
) -> None:
    if write:
        result = manual_launch.write_manual_launch_command_packet(
            manual_launch_preflight_path,
            detach=detach,
            execution_phase=execution_phase,
        )
    else:
        result = manual_launch.build_manual_launch_command_packet(
            manual_launch_preflight_path,
            detach=detach,
            execution_phase=execution_phase,
        )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("live-launch-guard")
@click.option(
    "--command-packet",
    "command_packet_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a local manual launch command packet JSON artifact.",
)
@click.option("--actor", required=True, type=str, help="Actor requesting the live launch guard.")
@click.option("--execute", is_flag=True, help="Request the live execution boundary. This command still does not execute.")
@click.option(
    "--approval-phrase",
    default="",
    type=str,
    help="Must equal RUN_REMOTE_MANUAL_LAUNCH to reach the sandbox escalation boundary.",
)
@click.option(
    "--mission-bounded",
    is_flag=True,
    help="Allow mission-bounded execution readiness when workplace scope, rate limit, and destructive-command guards pass.",
)
@click.option("--rate-limit-window-seconds", default=600, type=int, help="SSH/SCP rate limit window before live execution.")
@click.option("--max-transport-calls-per-window", default=2, type=int, help="Maximum recent SSH/SCP calls to allow per host.")
@click.option("--write/--no-write", default=True, help="Write the local live guard artifact.")
def night_researcher_live_launch_guard_command(
    command_packet_path: Path,
    actor: str,
    execute: bool,
    approval_phrase: str,
    mission_bounded: bool,
    rate_limit_window_seconds: int,
    max_transport_calls_per_window: int,
    write: bool,
) -> None:
    if write:
        result = manual_launch.write_live_launch_executor_guard(
            command_packet_path,
            actor=actor,
            execute_requested=execute,
            approval_phrase=approval_phrase,
            mission_bounded=mission_bounded,
            rate_limit_window_seconds=rate_limit_window_seconds,
            max_transport_calls_per_window=max_transport_calls_per_window,
        )
    else:
        result = manual_launch.assess_live_launch_executor_guard(
            command_packet_path,
            actor=actor,
            execute_requested=execute,
            approval_phrase=approval_phrase,
            mission_bounded=mission_bounded,
            rate_limit_window_seconds=rate_limit_window_seconds,
            max_transport_calls_per_window=max_transport_calls_per_window,
        )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("manual-launch-readiness-report")
@click.option(
    "--launch-intent",
    "launch_intent_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a local launch intent JSON artifact.",
)
@click.option("--actor", required=True, type=str, help="Actor requesting the readiness report.")
@click.option(
    "--operator-confirmed",
    is_flag=True,
    help="Confirm an operator reviewed the launch intent.",
)
@click.option(
    "--write/--no-write",
    default=True,
    help="Write local readiness report artifact.",
)
def night_researcher_manual_launch_readiness_report_command(
    launch_intent_path: Path,
    actor: str,
    operator_confirmed: bool,
    write: bool,
) -> None:
    if write:
        result = manual_launch.write_manual_launch_readiness_report(
            launch_intent_path,
            actor=actor,
            operator_confirmed=operator_confirmed,
        )
    else:
        result = manual_launch.assess_manual_launch_readiness_report(
            launch_intent_path,
            actor=actor,
            operator_confirmed=operator_confirmed,
        )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("b14-l74-artifact-readiness")
@click.option(
    "--output-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Result directory containing B14-L74 collect/judge artifacts.",
)
@click.option(
    "--write/--no-write",
    default=True,
    help="Write a local artifact readiness report into the result directory.",
)
def night_researcher_b14_l74_artifact_readiness_command(
    output_dir: Path,
    write: bool,
) -> None:
    if write:
        result = bounded_validation_bucket.write_b14_l74_artifact_readiness_report(output_dir)
    else:
        result = bounded_validation_bucket.assess_b14_l74_artifact_readiness(output_dir).model_dump(mode="json")
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("research-prerequisite-packet")
@click.option(
    "--project-knowledge",
    "project_knowledge_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a local project knowledge bundle JSON artifact.",
)
@click.option(
    "--discussion-payload",
    "discussion_payload_path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a local discussion payload JSON artifact.",
)
@click.option(
    "--output-dir",
    required=True,
    type=click.Path(path_type=Path),
    help="Output directory for research_prerequisite_packet.json and gate artifacts.",
)
def night_researcher_research_prerequisite_packet_command(
    project_knowledge_path: Path,
    discussion_payload_path: Optional[Path],
    output_dir: Path,
) -> None:
    project_knowledge = json.loads(project_knowledge_path.read_text(encoding="utf-8"))
    discussion_payload = (
        json.loads(discussion_payload_path.read_text(encoding="utf-8"))
        if discussion_payload_path
        else {}
    )
    result = three_agent_flow.build_research_prerequisite_packet(
        project_knowledge=project_knowledge,
        discussion_payload=discussion_payload,
        output_dir=output_dir,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("mission-remote-dry-run")
@click.option(
    "--mission-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote JSON payload.",
)
@click.option(
    "--task-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote task JSON payload.",
)
@click.option(
    "--run-root",
    required=True,
    type=click.Path(path_type=Path),
    help="Run root where iteration artifacts and budget_ledger.jsonl will be written.",
)
@click.option(
    "--project-knowledge-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a project knowledge JSON payload.",
)
@click.option(
    "--discussion-payload-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a discussion payload JSON artifact.",
)
def night_researcher_mission_remote_dry_run_command(
    mission_path: Path,
    task_path: Path,
    run_root: Path,
    project_knowledge_path: Optional[Path],
    discussion_payload_path: Optional[Path],
) -> None:
    result = mission_remote_loop.write_local_mission_remote_rehearsal(
        mission_path,
        task_path,
        run_root=run_root,
        project_knowledge_path=project_knowledge_path,
        discussion_payload_path=discussion_payload_path,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("mission-remote-preflight")
@click.option(
    "--mission-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote JSON payload.",
)
@click.option(
    "--task-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote task JSON payload.",
)
@click.option(
    "--output-dir",
    required=True,
    type=click.Path(path_type=Path),
    help="Output directory where mission gate and remote launch preflight artifacts will be written.",
)
@click.option(
    "--run-root",
    default=None,
    type=click.Path(path_type=Path),
    help="Optional mission remote rehearsal root used to read prior budget/checkpoint state.",
)
@click.option(
    "--project-knowledge-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a project knowledge JSON payload.",
)
@click.option(
    "--discussion-payload-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a discussion payload JSON artifact.",
)
def night_researcher_mission_remote_preflight_command(
    mission_path: Path,
    task_path: Path,
    output_dir: Path,
    run_root: Optional[Path],
    project_knowledge_path: Optional[Path],
    discussion_payload_path: Optional[Path],
) -> None:
    result = mission_remote_loop.write_local_mission_remote_preflight(
        mission_path,
        task_path,
        output_dir=output_dir,
        run_root=run_root,
        project_knowledge_path=project_knowledge_path,
        discussion_payload_path=discussion_payload_path,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("mission-remote-launch-packet")
@click.option(
    "--mission-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote JSON payload.",
)
@click.option(
    "--task-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote task JSON payload.",
)
@click.option(
    "--output-dir",
    required=True,
    type=click.Path(path_type=Path),
    help="Output directory where launch packet artifacts will be written.",
)
@click.option(
    "--run-root",
    default=None,
    type=click.Path(path_type=Path),
    help="Optional mission remote run root used to read prior budget/checkpoint state.",
)
@click.option(
    "--project-knowledge-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a project knowledge JSON payload.",
)
@click.option(
    "--discussion-payload-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a discussion payload JSON artifact.",
)
@click.option(
    "--phase",
    "execution_phase",
    default="execute_run",
    type=click.Choice(["full", "prepare_code", "execute_run"]),
    help="Render the future run-remote command for the chosen phase.",
)
@click.option(
    "--remote-task-id",
    default=None,
    type=str,
    help="Explicit orchestrator task id to bind into the dry-run command packet.",
)
@click.option("--detach/--foreground", default=True, help="Render the future run-remote command with --detach.")
def night_researcher_mission_remote_launch_packet_command(
    mission_path: Path,
    task_path: Path,
    output_dir: Path,
    run_root: Optional[Path],
    project_knowledge_path: Optional[Path],
    discussion_payload_path: Optional[Path],
    execution_phase: str,
    remote_task_id: Optional[str],
    detach: bool,
) -> None:
    result = mission_remote_loop.write_mission_remote_launch_packet(
        mission_path,
        task_path,
        output_dir=output_dir,
        run_root=run_root,
        project_knowledge_path=project_knowledge_path,
        discussion_payload_path=discussion_payload_path,
        remote_task_id=remote_task_id,
        phase=execution_phase,
        detach=detach,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("mission-remote-launch-once")
@click.option(
    "--mission-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote JSON payload.",
)
@click.option(
    "--task-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote task JSON payload.",
)
@click.option(
    "--run-root",
    required=True,
    type=click.Path(path_type=Path),
    help="Mission remote run root where iteration artifacts and budget state will be written.",
)
@click.option(
    "--project-knowledge-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a project knowledge JSON payload.",
)
@click.option(
    "--discussion-payload-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a discussion payload JSON artifact.",
)
@click.option(
    "--remote-task-id",
    default=None,
    type=str,
    help="Explicit orchestrator task id to pass to run-remote. Overrides task payload fields.",
)
@click.option(
    "--phase",
    "execution_phase",
    default="execute_run",
    type=click.Choice(["full", "prepare_code", "execute_run"]),
    help="Run the bounded remote launch with the chosen phase.",
)
@click.option("--detach/--foreground", default=True, help="Launch remotely in background when allowed.")
def night_researcher_mission_remote_launch_once_command(
    mission_path: Path,
    task_path: Path,
    run_root: Path,
    project_knowledge_path: Optional[Path],
    discussion_payload_path: Optional[Path],
    remote_task_id: Optional[str],
    execution_phase: str,
    detach: bool,
) -> None:
    mission_payload = json.loads(mission_path.read_text(encoding="utf-8"))
    task_payload = json.loads(task_path.read_text(encoding="utf-8"))
    project_knowledge = json.loads(project_knowledge_path.read_text(encoding="utf-8")) if project_knowledge_path else {}
    discussion_payload = json.loads(discussion_payload_path.read_text(encoding="utf-8")) if discussion_payload_path else {}
    result = mission_remote_loop.execute_mission_remote_launch_once(
        mission_payload,
        task_payload,
        run_root=run_root,
        project_knowledge=project_knowledge,
        discussion_payload=discussion_payload,
        remote_task_id=remote_task_id,
        phase=execution_phase,
        detach=detach,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("mission-remote-finalize")
@click.option(
    "--mission-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote JSON payload.",
)
@click.option(
    "--task-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote task JSON payload.",
)
@click.option(
    "--run-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Mission remote run root where the launched iteration artifacts already exist.",
)
@click.option(
    "--iteration-id",
    required=True,
    type=str,
    help="Iteration id to finalize, for example iteration_003.",
)
@click.option(
    "--collect-output-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Collected local output directory synced from the bounded remote run.",
)
@click.option(
    "--project-knowledge-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a project knowledge JSON payload.",
)
@click.option(
    "--discussion-payload-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a discussion payload JSON artifact.",
)
@click.option(
    "--sync-pull-result-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional JSON file containing the sync-pull-remote result for audit.",
)
def night_researcher_mission_remote_finalize_command(
    mission_path: Path,
    task_path: Path,
    run_root: Path,
    iteration_id: str,
    collect_output_dir: Path,
    project_knowledge_path: Optional[Path],
    discussion_payload_path: Optional[Path],
    sync_pull_result_path: Optional[Path],
) -> None:
    mission_payload = json.loads(mission_path.read_text(encoding="utf-8"))
    task_payload = json.loads(task_path.read_text(encoding="utf-8"))
    project_knowledge = json.loads(project_knowledge_path.read_text(encoding="utf-8")) if project_knowledge_path else {}
    discussion_payload = json.loads(discussion_payload_path.read_text(encoding="utf-8")) if discussion_payload_path else {}
    sync_pull_result = json.loads(sync_pull_result_path.read_text(encoding="utf-8")) if sync_pull_result_path else {}
    result = mission_remote_loop.finalize_mission_remote_collect_judge(
        mission_payload,
        task_payload,
        run_root=run_root,
        iteration_id=iteration_id,
        collect_output_dir=collect_output_dir,
        project_knowledge=project_knowledge,
        discussion_payload=discussion_payload,
        sync_pull_result=sync_pull_result,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("mission-remote-sync-finalize")
@click.option(
    "--mission-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote JSON payload.",
)
@click.option(
    "--task-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a mission remote task JSON payload.",
)
@click.option(
    "--run-root",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Mission remote run root where the launched iteration artifacts already exist.",
)
@click.option(
    "--iteration-id",
    required=True,
    type=str,
    help="Iteration id to sync and finalize, for example iteration_003.",
)
@click.option(
    "--project-knowledge-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a project knowledge JSON payload.",
)
@click.option(
    "--discussion-payload-path",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Optional path to a discussion payload JSON artifact.",
)
def night_researcher_mission_remote_sync_finalize_command(
    mission_path: Path,
    task_path: Path,
    run_root: Path,
    iteration_id: str,
    project_knowledge_path: Optional[Path],
    discussion_payload_path: Optional[Path],
) -> None:
    mission_payload = json.loads(mission_path.read_text(encoding="utf-8"))
    task_payload = json.loads(task_path.read_text(encoding="utf-8"))
    project_knowledge = json.loads(project_knowledge_path.read_text(encoding="utf-8")) if project_knowledge_path else {}
    discussion_payload = json.loads(discussion_payload_path.read_text(encoding="utf-8")) if discussion_payload_path else {}
    result = mission_remote_loop.sync_finalize_mission_remote_iteration(
        mission_payload,
        task_payload,
        run_root=run_root,
        iteration_id=iteration_id,
        project_knowledge=project_knowledge,
        discussion_payload=discussion_payload,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("mission-remote-status")
@click.option(
    "--mission-id",
    required=True,
    type=str,
    help="Mission id under outputs/night_runs/.",
)
@click.option(
    "--run-root",
    default=None,
    type=click.Path(path_type=Path),
    help="Optional mission remote rehearsal root. Defaults to outputs/night_runs/<mission_id>/.",
)
def night_researcher_mission_remote_status_command(
    mission_id: str,
    run_root: Optional[Path],
) -> None:
    status_root = run_root or continuation_night_auto_local_execution_root(mission_id).parent
    result = mission_remote_loop.summarize_local_mission_remote_rehearsal(status_root)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("mission-remote-history")
@click.option(
    "--mission-id",
    required=True,
    type=str,
    help="Mission id under outputs/night_runs/.",
)
@click.option(
    "--run-root",
    default=None,
    type=click.Path(path_type=Path),
    help="Optional mission remote rehearsal root. Defaults to outputs/night_runs/<mission_id>/.",
)
@click.option("--limit", default=10, type=int, help="Maximum number of latest iterations to include.")
def night_researcher_mission_remote_history_command(
    mission_id: str,
    run_root: Optional[Path],
    limit: int,
) -> None:
    history_root = run_root or continuation_night_auto_local_execution_root(mission_id).parent
    result = mission_remote_loop.read_local_mission_remote_history(history_root, limit=limit)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("mission-remote-followup-proposal")
@click.option(
    "--mission-id",
    required=True,
    type=str,
    help="Mission id under outputs/night_runs/.",
)
@click.option(
    "--run-root",
    default=None,
    type=click.Path(path_type=Path),
    help="Optional mission remote rehearsal root. Defaults to outputs/night_runs/<mission_id>/.",
)
@click.option(
    "--actor",
    default="system",
    type=str,
    help="Actor to attribute to the advisory proposal candidate write.",
)
@click.option(
    "--write/--no-write",
    default=True,
    help="Write advisory proposal-candidate artifacts when the latest mission continuation decision allows it.",
)
def night_researcher_mission_remote_followup_proposal_command(
    mission_id: str,
    run_root: Optional[Path],
    actor: str,
    write: bool,
) -> None:
    proposal_root = run_root or continuation_night_auto_local_execution_root(mission_id).parent
    if write:
        result = mission_remote_loop.write_local_mission_remote_followup_candidates(
            proposal_root,
            actor=actor,
        )
    else:
        result = mission_remote_loop.build_local_mission_remote_followup_candidates(
            proposal_root,
            actor=actor,
        )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("context-index-prerequisite-packet")
@click.option("--query", required=True, type=str, help="Context-index retrieval query.")
@click.option("--repo-root", default=None, type=click.Path(path_type=Path), help="Repository root for context index.")
@click.option("--discussion-payload", "discussion_payload_path", default=None, type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Optional path to discussion payload JSON.")
@click.option("--project-id", default=None, type=str, help="Optional project id override.")
@click.option("--workplace-profile", default=None, type=str, help="Optional workplace profile metadata.")
@click.option("--knowledge-root-ref", default=None, type=str, help="Optional knowledge root ref.")
@click.option("--limit", default=8, type=int, help="Maximum context-index results to use.")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path), help="Output directory for bundle and prerequisite packet artifacts.")
def night_researcher_context_index_prerequisite_packet_command(
    query: str,
    repo_root: Optional[Path],
    discussion_payload_path: Optional[Path],
    project_id: Optional[str],
    workplace_profile: Optional[str],
    knowledge_root_ref: Optional[str],
    limit: int,
    output_dir: Path,
) -> None:
    discussion_payload = (
        json.loads(discussion_payload_path.read_text(encoding="utf-8"))
        if discussion_payload_path
        else {}
    )
    result = three_agent_flow.build_context_index_research_prerequisite_packet(
        repo_root=repo_root or project_root(),
        query=query,
        output_dir=output_dir,
        discussion_payload=discussion_payload,
        project_id=project_id,
        workplace_profile=workplace_profile,
        knowledge_root_ref=knowledge_root_ref,
        limit=limit,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@night_researcher_group.command("artifact-index-prerequisite-packet")
@click.option("--query", required=True, type=str, help="Artifact-index retrieval query.")
@click.option("--repo-root", default=None, type=click.Path(path_type=Path), help="Repository root for artifact index.")
@click.option("--discussion-payload", "discussion_payload_path", default=None, type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Optional path to discussion payload JSON.")
@click.option("--project-id", default=None, type=str, help="Optional project id override.")
@click.option("--workplace-profile", default=None, type=str, help="Optional workplace profile metadata.")
@click.option("--knowledge-root-ref", default=None, type=str, help="Optional knowledge root ref.")
@click.option("--limit", default=8, type=int, help="Maximum artifact-index results to use.")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path), help="Output directory for bundle and prerequisite packet artifacts.")
def night_researcher_artifact_index_prerequisite_packet_command(
    query: str,
    repo_root: Optional[Path],
    discussion_payload_path: Optional[Path],
    project_id: Optional[str],
    workplace_profile: Optional[str],
    knowledge_root_ref: Optional[str],
    limit: int,
    output_dir: Path,
) -> None:
    discussion_payload = (
        json.loads(discussion_payload_path.read_text(encoding="utf-8"))
        if discussion_payload_path
        else {}
    )
    result = three_agent_flow.build_artifact_index_research_prerequisite_packet(
        repo_root=repo_root or project_root(),
        query=query,
        output_dir=output_dir,
        discussion_payload=discussion_payload,
        project_id=project_id,
        workplace_profile=workplace_profile,
        knowledge_root_ref=knowledge_root_ref,
        limit=limit,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.group("research-deliberation")
def research_deliberation_group() -> None:
    """Local research deliberation session helpers."""


@research_deliberation_group.command("mock-session")
@click.option("--question", required=True, type=str, help="Single research question to deliberate.")
@click.option("--session-id", default=None, type=str, help="Optional deterministic session id.")
@click.option("--line", default=None, type=str, help="Optional line/scope label.")
@click.option("--source-ref", multiple=True, type=str, help="Source context reference. Can be repeated.")
@click.option("--print-transcript", is_flag=True, help="Print the generated transcript after writing artifacts.")
def research_deliberation_mock_session_command(
    question: str,
    session_id: Optional[str],
    line: Optional[str],
    source_ref: tuple[str, ...],
    print_transcript: bool,
) -> None:
    brief = {
        "session_id": session_id or "",
        "research_question": question,
        "source_context_refs": list(source_ref),
        "allowed_scope": {"line": line} if line else {},
        "forbidden_actions": [
            "write_inbox",
            "promote",
            "dispatch",
            "scheduler_submit",
            "registry_mutation",
            "remote_execution",
            "source_edit",
            "path_guessing",
            "active_inbox_write",
            "direct_task_generation",
        ],
        "knowledge_pack_excerpt": {
            "mode": "mock_cli_brief",
            "note": "No live API, remote execution, proposal staging, or scheduler dispatch was used.",
        },
    }
    session = research_deliberation.build_mock_research_deliberation_session(brief)
    result = research_deliberation.write_research_deliberation_session_artifacts(session)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if print_transcript:
        transcript_path = Path(result["artifacts"]["council_transcript"])
        _echo("")
        _echo(transcript_path.read_text(encoding="utf-8"))


@cli.group("project-dialogue")
def project_dialogue_group() -> None:
    """Project-facing dialogue helpers backed by project knowledge bundles."""


@project_dialogue_group.command("answer")
@click.option("--knowledge-json", required=True, type=click.Path(exists=True, path_type=Path), help="Project knowledge bundle JSON.")
@click.option("--question", required=True, type=str, help="Project-facing question to answer.")
@click.option("--session-id", default=None, type=str, help="Optional deterministic session id.")
@click.option("--lane", default="interactive_alignment", type=click.Choice(["autonomous_inspection", "interactive_alignment"]))
@click.option("--write/--no-write", default=True, help="Write local session artifacts under continuation/project_dialogue.")
def project_dialogue_answer_command(
    knowledge_json: Path,
    question: str,
    session_id: Optional[str],
    lane: str,
    write: bool,
) -> None:
    payload = json.loads(knowledge_json.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise click.ClickException("knowledge-json must contain a JSON object")
    session = project_dialogue.build_project_dialogue_session_dry_run(
        payload,
        question=question,
        session_id=session_id,
        lane=lane,
    )
    if write:
        result = project_dialogue.write_project_dialogue_session_dry_run(session)
        _echo(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _echo(json.dumps(session["current_summary"], indent=2, ensure_ascii=False))


@project_dialogue_group.command("ingest-answer")
@click.option("--docs-dir", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path), help="Local project docs directory.")
@click.option("--question", required=True, type=str, help="Project-facing question to answer.")
@click.option("--project-id", default=None, type=str, help="Optional project id override.")
@click.option("--workplace-profile", default=None, type=str, help="Optional workplace profile metadata.")
@click.option("--knowledge-root-ref", default=None, type=str, help="Optional knowledge-root reference label.")
@click.option("--session-id", default=None, type=str, help="Optional deterministic dialogue session id.")
@click.option("--output-json", default=None, type=click.Path(path_type=Path), help="Optional output path for project_knowledge.json.")
@click.option("--write/--no-write", default=True, help="Write local session artifacts under continuation/project_dialogue.")
def project_dialogue_ingest_answer_command(
    docs_dir: Path,
    question: str,
    project_id: Optional[str],
    workplace_profile: Optional[str],
    knowledge_root_ref: Optional[str],
    session_id: Optional[str],
    output_json: Optional[Path],
    write: bool,
) -> None:
    bundle = project_knowledge_ingestion.build_project_knowledge_from_docs_dry_run(
        docs_dir,
        project_id=project_id,
        workplace_profile=workplace_profile,
        knowledge_root_ref=knowledge_root_ref,
    )
    bundle_write = None
    if output_json is not None:
        bundle_write = project_knowledge_ingestion.write_project_knowledge_bundle_dry_run(bundle, output_json)
    session = project_dialogue.build_project_dialogue_session_dry_run(
        bundle,
        question=question,
        session_id=session_id,
    )
    if write:
        result = project_dialogue.write_project_dialogue_session_dry_run(session)
        result["project_knowledge_output"] = bundle_write
        _echo(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        payload = {
            "project_knowledge_output": bundle_write,
            "answer": session["current_summary"],
        }
        _echo(json.dumps(payload, indent=2, ensure_ascii=False))


@project_dialogue_group.command("index-answer")
@click.option("--repo-root", default=None, type=click.Path(path_type=Path), help="Repository root containing .solution/context/context_index.sqlite.")
@click.option("--query", required=True, type=str, help="Context-index retrieval query.")
@click.option("--question", required=True, type=str, help="Project-facing question to answer.")
@click.option("--project-id", default=None, type=str, help="Optional project id override.")
@click.option("--workplace-profile", default=None, type=str, help="Optional workplace profile metadata.")
@click.option("--knowledge-root-ref", default=None, type=str, help="Optional knowledge-root reference label.")
@click.option("--limit", default=8, type=int, help="Maximum context-index results to use.")
@click.option("--session-id", default=None, type=str, help="Optional deterministic dialogue session id.")
@click.option("--output-json", default=None, type=click.Path(path_type=Path), help="Optional output path for project_knowledge.json.")
@click.option("--write/--no-write", default=True, help="Write local session artifacts under continuation/project_dialogue.")
def project_dialogue_index_answer_command(
    repo_root: Optional[Path],
    query: str,
    question: str,
    project_id: Optional[str],
    workplace_profile: Optional[str],
    knowledge_root_ref: Optional[str],
    limit: int,
    session_id: Optional[str],
    output_json: Optional[Path],
    write: bool,
) -> None:
    bundle = project_knowledge_ingestion.build_project_knowledge_from_context_index_dry_run(
        repo_root or project_root(),
        query,
        project_id=project_id,
        workplace_profile=workplace_profile,
        knowledge_root_ref=knowledge_root_ref,
        limit=limit,
    )
    bundle_write = None
    if output_json is not None:
        bundle_write = project_knowledge_ingestion.write_project_knowledge_bundle_dry_run(bundle, output_json)
    session = project_dialogue.build_project_dialogue_session_dry_run(
        bundle,
        question=question,
        session_id=session_id,
    )
    if write:
        result = project_dialogue.write_project_dialogue_session_dry_run(session)
        result["project_knowledge_output"] = bundle_write
        _echo(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        payload = {
            "project_knowledge_output": bundle_write,
            "answer": session["current_summary"],
        }
        _echo(json.dumps(payload, indent=2, ensure_ascii=False))


@cli.group("source-repair")
def source_repair_group() -> None:
    """Local source-repair planning helpers for partial/missing capability results."""


@source_repair_group.command("plan")
@click.option("--result-dir", required=True, type=click.Path(exists=True, path_type=Path), help="Result artifact directory or latest/ directory.")
@click.option("--source-task-id", default=None, type=str, help="Source task id for the partial result.")
@click.option("--downstream-task-id", default=None, type=str, help="Task to rerun after a Codex repair lands.")
@click.option("--blocker", "blockers", multiple=True, type=str, help="Known blocker to record, e.g. remote_codex_unavailable.")
@click.option("--write/--no-write", default=False, help="Write plan artifacts under continuation/source_repair.")
def source_repair_plan_command(
    result_dir: Path,
    source_task_id: Optional[str],
    downstream_task_id: Optional[str],
    blockers: tuple[str, ...],
    write: bool,
) -> None:
    plan = source_repair.build_source_repair_plan(
        result_dir,
        source_task_id=source_task_id,
        downstream_task_id=downstream_task_id,
        extra_blockers=list(blockers),
    )
    if write:
        output = source_repair.write_source_repair_plan(plan)
        output["plan"] = plan
        _echo(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        _echo(json.dumps(plan, indent=2, ensure_ascii=False))


@source_repair_group.command("plan-task")
@click.option("--task-path", required=True, type=click.Path(exists=True, path_type=Path), help="Task book to turn into a bounded source-repair plan.")
@click.option("--downstream-task-id", default=None, type=str, help="Task to rerun after the repair lands.")
@click.option("--blocker", "blockers", multiple=True, type=str, help="Known blocker to record.")
@click.option("--write/--no-write", default=False, help="Write plan artifacts under continuation/source_repair.")
def source_repair_plan_task_command(
    task_path: Path,
    downstream_task_id: Optional[str],
    blockers: tuple[str, ...],
    write: bool,
) -> None:
    plan = source_repair.build_source_repair_plan_from_taskbook(
        task_path,
        downstream_task_id=downstream_task_id,
        extra_blockers=list(blockers),
    )
    if write:
        output = source_repair.write_source_repair_plan(plan)
        output["plan"] = plan
        _echo(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        _echo(json.dumps(plan, indent=2, ensure_ascii=False))


@source_repair_group.command("plan-action-request")
@click.option("--action-request-path", required=True, type=click.Path(exists=True, path_type=Path), help="Pending action_request JSON to turn into a source-repair plan.")
@click.option("--downstream-task-id", default=None, type=str, help="Task to rerun after the repair lands.")
@click.option("--blocker", "blockers", multiple=True, type=str, help="Known blocker to record.")
@click.option("--write/--no-write", default=False, help="Write plan artifacts under continuation/source_repair.")
def source_repair_plan_action_request_command(
    action_request_path: Path,
    downstream_task_id: Optional[str],
    blockers: tuple[str, ...],
    write: bool,
) -> None:
    plan = source_repair.build_source_repair_plan_from_action_request(
        action_request_path,
        downstream_task_id=downstream_task_id,
        extra_blockers=list(blockers),
    )
    if write:
        output = source_repair.write_source_repair_plan(plan)
        output["plan"] = plan
        _echo(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        _echo(json.dumps(plan, indent=2, ensure_ascii=False))


@source_repair_group.command("rehearse")
@click.option("--result-dir", required=True, type=click.Path(exists=True, path_type=Path), help="Result artifact directory or latest/ directory.")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path), help="Local rehearsal output directory.")
@click.option("--source-task-id", default="b14_l72_large_image_bbox_sidecar_smoke", type=str, help="Source task id.")
@click.option("--downstream-task-id", default=None, type=str, help="Task to rerun after repair.")
@click.option("--repair-type", default=None, type=str, help="Optional explicit repair type override.")
@click.option("--available-test", "available_tests", multiple=True, type=str, help="Available validation test id. Can be repeated.")
def source_repair_rehearse_command(
    result_dir: Path,
    output_dir: Path,
    source_task_id: str,
    downstream_task_id: Optional[str],
    repair_type: Optional[str],
    available_tests: tuple[str, ...],
) -> None:
    result = self_repair_rehearsal.run_self_repair_rehearsal(
        result_dir=result_dir,
        output_dir=output_dir,
        source_task_id=source_task_id,
        downstream_task_id=downstream_task_id,
        repair_type=repair_type,
        available_tests=list(available_tests),
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("materialize-patch")
@click.option("--plan-path", required=True, type=click.Path(exists=True, path_type=Path), help="Source repair plan JSON.")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path), help="Output directory for patch.diff and quality report.")
def source_repair_materialize_patch_command(plan_path: Path, output_dir: Path) -> None:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result = source_repair.materialize_source_repair_patch(plan, output_dir=output_dir)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("design-packet")
@click.option("--plan-path", required=True, type=click.Path(exists=True, path_type=Path), help="Source repair plan JSON.")
@click.option("--output-dir", default=None, type=click.Path(path_type=Path), help="Optional output directory. Defaults to the source-repair plan directory.")
@click.option("--write/--no-write", default=False, help="Write repair_design_packet.json.")
def source_repair_design_packet_command(plan_path: Path, output_dir: Optional[Path], write: bool) -> None:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if write:
        result = source_repair.write_repair_design_packet(plan, output_dir=output_dir)
        _echo(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        result = source_repair.build_repair_design_packet(plan)
        _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("validate-design")
@click.option("--design-packet-path", required=True, type=click.Path(exists=True, path_type=Path), help="Repair design packet JSON.")
@click.option("--output-dir", default=None, type=click.Path(path_type=Path), help="Optional output directory. Defaults to the source-repair design directory.")
@click.option("--repo-root", default=None, type=click.Path(exists=True, file_okay=False, path_type=Path), help="Repository root to validate against.")
@click.option("--write/--no-write", default=False, help="Write validation_report.json and claim_boundary_report.json.")
def source_repair_validate_design_command(
    design_packet_path: Path,
    output_dir: Optional[Path],
    repo_root: Optional[Path],
    write: bool,
) -> None:
    design = json.loads(design_packet_path.read_text(encoding="utf-8"))
    target_dir = output_dir or design_packet_path.parent
    if write:
        result = source_repair.write_repair_design_validation(design, output_dir=target_dir, repo_root=repo_root)
    else:
        validation = source_repair.build_repair_design_validation_report(design, repo_root=repo_root)
        result = {
            "validation": validation,
            "claim_boundary": source_repair.build_claim_boundary_report(design, validation),
            "rerun_intent": source_repair.build_rerun_intent_from_repair_design_validation(design, validation),
            "side_effects": dict(source_repair.DEFAULT_SIDE_EFFECTS),
        }
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("three-agent-rehearse")
@click.option("--mission-id", required=True, type=str, help="Mission id for the rehearsal.")
@click.option("--proposal-run-id", required=True, type=str, help="Proposal run id for pending scheduler dry-run.")
@click.option("--candidate-id", required=True, type=str, help="Candidate id for pending scheduler dry-run.")
@click.option("--source-task-id", required=True, type=str, help="Source/result task id.")
@click.option("--result-dir", required=True, type=click.Path(exists=True, path_type=Path), help="Result artifact dir containing decision_summary/action_requests.")
@click.option("--source-task-path", required=True, type=click.Path(exists=True, path_type=Path), help="Taskbook to use for repair planning.")
@click.option("--downstream-task-id", required=True, type=str, help="Downstream task to rerun after repair.")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path), help="Output directory for three-agent rehearsal artifacts.")
@click.option("--available-test", "available_tests", multiple=True, type=str, help="Available validation test id. Can be repeated.")
def source_repair_three_agent_rehearse_command(
    mission_id: str,
    proposal_run_id: str,
    candidate_id: str,
    source_task_id: str,
    result_dir: Path,
    source_task_path: Path,
    downstream_task_id: str,
    output_dir: Path,
    available_tests: tuple[str, ...],
) -> None:
    result = three_agent_flow.rehearse_three_agent_flow(
        mission_id=mission_id,
        proposal_run_id=proposal_run_id,
        candidate_id=candidate_id,
        source_task_id=source_task_id,
        result_dir=result_dir,
        source_task_path=source_task_path,
        downstream_task_id=downstream_task_id,
        output_dir=output_dir,
        available_tests=list(available_tests) if available_tests else None,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("repair-scheduler-gate")
@click.option("--summary-path", required=True, type=click.Path(exists=True, path_type=Path), help="Three-agent flow summary JSON.")
@click.option("--validation-report", default=None, type=click.Path(exists=True, path_type=Path), help="Optional validation report JSON.")
@click.option("--output-path", default=None, type=click.Path(path_type=Path), help="Optional path to write gate report.")
def source_repair_scheduler_gate_command(
    summary_path: Path,
    validation_report: Optional[Path],
    output_path: Optional[Path],
) -> None:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    validation = json.loads(validation_report.read_text(encoding="utf-8")) if validation_report else None
    result = three_agent_flow.assess_repair_to_scheduler_gate_from_summary(
        summary,
        validation_report=validation,
    )
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("validate-workplace-patch")
@click.option("--summary-path", required=True, type=click.Path(exists=True, path_type=Path), help="Three-agent flow summary JSON.")
@click.option("--workplace-root", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path), help="Local workplace root where the patch was applied.")
@click.option("--output-dir", default=None, type=click.Path(path_type=Path), help="Optional directory for validation_report.json.")
def source_repair_validate_workplace_patch_command(
    summary_path: Path,
    workplace_root: Path,
    output_dir: Optional[Path],
) -> None:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    result = three_agent_flow.validate_workplace_patch_adapter(
        summary,
        workplace_root=workplace_root,
        output_dir=output_dir,
        summary_path=summary_path,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("autonomy-assess")
@click.option("--summary-path", required=True, type=click.Path(exists=True, path_type=Path), help="Three-agent flow summary JSON.")
@click.option("--repair-scheduler-gate", default=None, type=click.Path(exists=True, path_type=Path), help="Optional repair-to-scheduler gate JSON.")
@click.option("--output-path", default=None, type=click.Path(path_type=Path), help="Optional path to write autonomy assessment JSON.")
def source_repair_autonomy_assess_command(
    summary_path: Path,
    repair_scheduler_gate: Optional[Path],
    output_path: Optional[Path],
) -> None:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    gate = json.loads(repair_scheduler_gate.read_text(encoding="utf-8")) if repair_scheduler_gate else None
    result = three_agent_flow.assess_three_agent_autonomy(
        summary,
        repair_scheduler_gate=gate,
    )
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("scheduler-admission-rehearse")
@click.option("--summary-path", required=True, type=click.Path(exists=True, path_type=Path), help="Three-agent flow summary JSON.")
@click.option("--repair-scheduler-gate", default=None, type=click.Path(exists=True, path_type=Path), help="Optional repair-to-scheduler gate JSON.")
@click.option("--output-dir", default=None, type=click.Path(path_type=Path), help="Optional directory for scheduler_admission_rehearsal.json.")
def source_repair_scheduler_admission_rehearse_command(
    summary_path: Path,
    repair_scheduler_gate: Optional[Path],
    output_dir: Optional[Path],
) -> None:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    gate = json.loads(repair_scheduler_gate.read_text(encoding="utf-8")) if repair_scheduler_gate else None
    result = three_agent_flow.rehearse_scheduler_admission_after_repair(
        summary,
        repair_scheduler_gate=gate,
        output_dir=output_dir,
        summary_path=summary_path,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("l72-admission-packet")
@click.option("--admission-rehearsal", required=True, type=click.Path(exists=True, path_type=Path), help="Scheduler admission rehearsal JSON.")
@click.option("--task-path", required=True, type=click.Path(exists=True, path_type=Path), help="L72 task book path.")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path), help="Output directory for packet and task draft copy.")
def source_repair_l72_admission_packet_command(
    admission_rehearsal: Path,
    task_path: Path,
    output_dir: Path,
) -> None:
    admission = json.loads(admission_rehearsal.read_text(encoding="utf-8"))
    result = three_agent_flow.build_l72_task_admission_packet(
        admission_rehearsal=admission,
        task_path=task_path,
        output_dir=output_dir,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("l72-draft-dry-run")
@click.option("--admission-packet", required=True, type=click.Path(exists=True, path_type=Path), help="L72 task admission packet JSON.")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path), help="Output directory for dry-run report.")
def source_repair_l72_draft_dry_run_command(
    admission_packet: Path,
    output_dir: Path,
) -> None:
    packet = json.loads(admission_packet.read_text(encoding="utf-8"))
    result = three_agent_flow.dry_run_l72_task_draft_scheduler_admission(
        admission_packet=packet,
        output_dir=output_dir,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("l72-stage-candidate")
@click.option("--admission-packet", required=True, type=click.Path(exists=True, path_type=Path), help="L72 task admission packet JSON.")
@click.option("--draft-dry-run", required=True, type=click.Path(exists=True, path_type=Path), help="L72 task draft scheduler dry-run JSON.")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path), help="Output directory for L72 staging candidate report.")
@click.option("--proposal-run-id", default="b14_l72_task_draft_stage", type=str, help="Proposal run id for local staging artifacts.")
@click.option("--actor", default="three_agent_flow", type=str, help="Actor to attribute to the staging bridge.")
def source_repair_l72_stage_candidate_command(
    admission_packet: Path,
    draft_dry_run: Path,
    output_dir: Path,
    proposal_run_id: str,
    actor: str,
) -> None:
    packet = json.loads(admission_packet.read_text(encoding="utf-8"))
    dry_run = json.loads(draft_dry_run.read_text(encoding="utf-8"))
    result = three_agent_flow.stage_l72_task_draft_candidate(
        admission_packet=packet,
        draft_dry_run=dry_run,
        output_dir=output_dir,
        proposal_run_id=proposal_run_id,
        actor=actor,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@source_repair_group.command("l72-manual-launch-readiness")
@click.option("--admission-packet", required=True, type=click.Path(exists=True, path_type=Path), help="L72 task admission packet JSON.")
@click.option("--draft-dry-run", required=True, type=click.Path(exists=True, path_type=Path), help="L72 task draft scheduler dry-run JSON.")
@click.option("--staging-candidate-report", required=True, type=click.Path(exists=True, path_type=Path), help="L72 staging candidate report JSON.")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path), help="Output directory for L72 manual launch readiness artifacts.")
@click.option("--actor", default="three_agent_flow", type=str, help="Actor to attribute to the local readiness bridge.")
@click.option("--approve-remote-launch/--no-approve-remote-launch", default=True, help="Create launch intent with explicit approval marker. Still no SSH.")
@click.option("--operator-confirmed/--operator-not-confirmed", default=False, help="Mark local operator confirmation for readiness report. Still no SSH.")
def source_repair_l72_manual_launch_readiness_command(
    admission_packet: Path,
    draft_dry_run: Path,
    staging_candidate_report: Path,
    output_dir: Path,
    actor: str,
    approve_remote_launch: bool,
    operator_confirmed: bool,
) -> None:
    packet = json.loads(admission_packet.read_text(encoding="utf-8"))
    dry_run = json.loads(draft_dry_run.read_text(encoding="utf-8"))
    staging = json.loads(staging_candidate_report.read_text(encoding="utf-8"))
    result = three_agent_flow.build_l72_manual_launch_readiness_from_stage(
        admission_packet=packet,
        draft_dry_run=dry_run,
        staging_candidate_report=staging,
        output_dir=output_dir,
        actor=actor,
        approve_remote_launch=approve_remote_launch,
        operator_confirmed=operator_confirmed,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@research_deliberation_group.command("pre-live-check")
@click.option("--question", required=True, type=str, help="Single research question to deliberate.")
@click.option("--session-id", default=None, type=str, help="Optional deterministic session id.")
@click.option("--line", default=None, type=str, help="Optional line/scope label.")
@click.option("--source-ref", multiple=True, type=str, help="Source context reference. Can be repeated.")
@click.option("--explicit-live-flag", is_flag=True, help="Set the live-provider admission flag for preflight only.")
@click.option("--eval-gate-pass", is_flag=True, help="Use the built-in required eval-gate pass fixture for dry-run preflight.")
def research_deliberation_pre_live_check_command(
    question: str,
    session_id: Optional[str],
    line: Optional[str],
    source_ref: tuple[str, ...],
    explicit_live_flag: bool,
    eval_gate_pass: bool,
) -> None:
    brief = {
        "session_id": session_id or "",
        "research_question": question,
        "source_context_refs": list(source_ref),
        "allowed_scope": {"line": line} if line else {},
        "forbidden_actions": [
            "write_inbox",
            "promote",
            "dispatch",
            "scheduler_submit",
            "registry_mutation",
            "remote_execution",
            "source_edit",
            "path_guessing",
            "active_inbox_write",
            "direct_task_generation",
        ],
        "knowledge_pack_excerpt": {
            "mode": "pre_live_cli_brief",
            "note": "No live API call is made by this command.",
        },
    }
    session = research_deliberation.execute_research_deliberation_mock_transcript(brief)
    session_write = research_deliberation.write_research_deliberation_session_artifacts(session)
    redaction_report = {
        "status": "pass",
        "secret_like_material_found": False,
        "findings": [],
        "mode": "deterministic_request_redaction_check",
    }
    eval_gate_report = {
        "status": "pass" if eval_gate_pass else "blocked",
        "passed_cases": [
            "prompt_injection_artifact",
            "missing_artifact_path_guessing",
            "source_code_required_routing",
            "reward_hack_risk",
            "same_question_round_1",
            "cross_review_round_2",
            "synthesis_rejected_options",
        ]
        if eval_gate_pass
        else [],
    }
    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=explicit_live_flag,
        redaction_report=redaction_report,
        eval_gate_report=eval_gate_report,
    )
    preflight_write = research_deliberation.write_research_deliberation_live_preflight_artifacts(
        session_id=session["session_id"],
        requests=session["api_requests"],
        redaction_report=redaction_report,
        eval_gate_report=eval_gate_report,
        preflight=preflight,
    )
    _echo(
        json.dumps(
            {
                "status": preflight["status"],
                "session_id": session["session_id"],
                "session_artifacts": session_write,
                "live_preflight_artifacts": preflight_write,
                "preflight": preflight,
                "api_call_executed": False,
                "network_enabled": False,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@research_deliberation_group.command("prepare-live-calls")
@click.option("--question", required=True, type=str, help="Single research question to deliberate.")
@click.option("--session-id", default=None, type=str, help="Optional deterministic session id.")
@click.option("--line", default=None, type=str, help="Optional line/scope label.")
@click.option("--source-ref", multiple=True, type=str, help="Source context reference. Can be repeated.")
@click.option("--explicit-live-flag", is_flag=True, help="Set the live-provider admission flag for preflight only.")
@click.option("--eval-gate-pass", is_flag=True, help="Use the built-in required eval-gate pass fixture for dry-run preflight.")
@click.option("--timeout-seconds", default=90, type=int, help="Per-call timeout to record in the dry-run call plan.")
def research_deliberation_prepare_live_calls_command(
    question: str,
    session_id: Optional[str],
    line: Optional[str],
    source_ref: tuple[str, ...],
    explicit_live_flag: bool,
    eval_gate_pass: bool,
    timeout_seconds: int,
) -> None:
    brief = {
        "session_id": session_id or "",
        "research_question": question,
        "source_context_refs": list(source_ref),
        "allowed_scope": {"line": line} if line else {},
        "forbidden_actions": [
            "write_inbox",
            "promote",
            "dispatch",
            "scheduler_submit",
            "registry_mutation",
            "remote_execution",
            "source_edit",
            "path_guessing",
            "active_inbox_write",
            "direct_task_generation",
        ],
        "knowledge_pack_excerpt": {
            "mode": "prepare_live_calls_cli_brief",
            "note": "This command prepares quarantine files only; it does not call live providers.",
        },
    }
    session = research_deliberation.execute_research_deliberation_mock_transcript(brief)
    session_write = research_deliberation.write_research_deliberation_session_artifacts(session)
    redaction_report = {
        "status": "pass",
        "secret_like_material_found": False,
        "findings": [],
        "mode": "deterministic_request_redaction_check",
    }
    eval_gate_report = {
        "status": "pass" if eval_gate_pass else "blocked",
        "passed_cases": [
            "prompt_injection_artifact",
            "missing_artifact_path_guessing",
            "source_code_required_routing",
            "reward_hack_risk",
            "same_question_round_1",
            "cross_review_round_2",
            "synthesis_rejected_options",
        ]
        if eval_gate_pass
        else [],
    }
    preflight = research_deliberation.build_research_deliberation_live_provider_preflight_dry_run(
        session["api_requests"],
        explicit_live_flag=explicit_live_flag,
        redaction_report=redaction_report,
        eval_gate_report=eval_gate_report,
    )
    preflight_write = research_deliberation.write_research_deliberation_live_preflight_artifacts(
        session_id=session["session_id"],
        requests=session["api_requests"],
        redaction_report=redaction_report,
        eval_gate_report=eval_gate_report,
        preflight=preflight,
    )
    call_plan = research_deliberation.build_research_deliberation_provider_call_plan_dry_run(
        preflight,
        session["api_requests"],
        timeout_seconds=timeout_seconds,
    )
    quarantine_write = research_deliberation.write_research_deliberation_live_call_quarantine_skeleton(
        call_plan,
        session["api_requests"],
    )
    _echo(
        json.dumps(
            {
                "status": quarantine_write["status"],
                "session_id": session["session_id"],
                "session_artifacts": session_write,
                "live_preflight_artifacts": preflight_write,
                "preflight": preflight,
                "call_plan": call_plan,
                "live_call_quarantine": quarantine_write,
                "api_call_executed": False,
                "network_enabled": False,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@research_deliberation_group.command("validate-live-response")
@click.option("--call-dir", required=True, type=click.Path(path_type=Path), help="Quarantined live-call directory.")
def research_deliberation_validate_live_response_command(call_dir: Path) -> None:
    result = research_deliberation.validate_research_deliberation_live_response_quarantine(call_dir)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") in {"blocked", "rejected"}:
        raise click.ClickException("live response validation failed")


@research_deliberation_group.command("live-call")
@click.option("--call-dir", required=True, type=click.Path(path_type=Path), help="Quarantined live-call directory.")
@click.option("--execute-live", is_flag=True, help="Actually call the configured provider endpoint.")
def research_deliberation_live_call_command(call_dir: Path, execute_live: bool) -> None:
    result = research_deliberation.execute_research_deliberation_live_call(
        call_dir,
        execute_live=execute_live,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") in {"blocked", "live_call_response_rejected"}:
        raise click.ClickException("live call did not produce a valid deliberation response")


@research_deliberation_group.command("provider-smoke")
@click.option("--provider-model", required=True, type=str, help="Allowlisted provider model alias.")
@click.option("--smoke-id", default=None, type=str, help="Optional smoke run id.")
@click.option("--execute-live", is_flag=True, help="Actually call the configured provider endpoint with synthetic content.")
def research_deliberation_provider_smoke_command(
    provider_model: str,
    smoke_id: Optional[str],
    execute_live: bool,
) -> None:
    result = research_deliberation.execute_research_provider_connectivity_smoke(
        provider_model=provider_model,
        smoke_id=smoke_id,
        execute_live=execute_live,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") in {"blocked", "smoke_response_rejected"}:
        raise click.ClickException("provider smoke did not complete")


@research_deliberation_group.command("external-safe-opinion")
@click.option("--provider-model", required=True, type=str, help="Allowlisted provider model alias.")
@click.option("--run-id", default=None, type=str, help="Optional external-safe run id.")
@click.option("--execute-live", is_flag=True, help="Actually call the configured provider endpoint with sanitized abstract context.")
def research_deliberation_external_safe_opinion_command(
    provider_model: str,
    run_id: Optional[str],
    execute_live: bool,
) -> None:
    result = research_deliberation.execute_external_safe_research_opinion_live(
        provider_model=provider_model,
        run_id=run_id,
        execute_live=execute_live,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("status") in {"blocked", "external_safe_opinion_rejected"}:
        raise click.ClickException("external-safe opinion did not complete")


@cli.command("write-file")
@click.option("--workspace", required=True, type=str, help="Workspace name under workspaces/.")
@click.option("--path", "relative_path", required=True, type=str, help="Relative path inside the workspace.")
@click.option("--content", required=True, type=str, help="File content to write.")
def write_file_command(workspace: str, relative_path: str, content: str) -> None:
    result = write_workspace_file(workspace, relative_path, content)
    _echo(json.dumps(result, indent=2))


@cli.command("patch-file")
@click.option("--workspace", required=True, type=str, help="Workspace name under workspaces/.")
@click.option("--path", "relative_path", required=True, type=str, help="Relative path inside the workspace.")
@click.option("--find", "old", required=True, type=str, help="Text to replace.")
@click.option("--replace", "new", required=True, type=str, help="Replacement text.")
def patch_file_command(workspace: str, relative_path: str, old: str, new: str) -> None:
    result = patch_workspace_file(workspace, relative_path, old, new)
    _echo(json.dumps(result, indent=2))


@cli.command("paths")
def paths_command() -> None:
    root = project_root()
    _echo(f"project_root={root}")
    for name in ("policies", "registry", "reports", "runs", "workspaces"):
        _echo(f"{name}={root / name}")


@cli.command("init")
def lab_init_command() -> None:
    result = lab.init_workspace()
    _echo(f"[labctl] workspace ready")
    _echo(f"[labctl] registry: {result['registry']}")


@cli.command("local-init")
@click.option("--no-create-dirs", is_flag=True, help="Only inspect; do not create orchestrator folders/registry.")
def lab_local_init_command(no_create_dirs: bool) -> None:
    result = lab.local_environment_init(create_dirs=not no_create_dirs)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if not result.get("ok"):
        raise click.ClickException("local init found missing required items")


@cli.command("new")
@click.argument("task_id")
@click.option("--title", required=True, type=str)
@click.option("--worker", default="codex", type=str)
def lab_new_command(task_id: str, title: str, worker: str) -> None:
    path = lab.new_task(task_id, title, worker=worker)
    _echo(f"[labctl] created task: {path}")


@cli.command("status")
@click.argument("task_id", required=False)
def lab_status_command(task_id: Optional[str]) -> None:
    _echo(json.dumps(lab.status(task_id), indent=2, ensure_ascii=False))


@cli.command("ingest")
@click.argument("source", type=click.Path(exists=True))
@click.option("--worker", default="codex", type=str)
@click.option("--move", is_flag=True, help="Archive source md files after import. Files under tasks/inbox are archived by default.")
def lab_ingest_command(source: str, worker: str, move: bool) -> None:
    result = lab.ingest(Path(source), worker=worker, move=move)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("archive-inbox")
def lab_archive_inbox_command() -> None:
    result = lab.archive_processed_inbox()
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("inbox-lint")
@click.argument("source", required=False, type=click.Path(path_type=Path))
def lab_inbox_lint_command(source: Optional[Path]) -> None:
    result = lab.inbox_lint(source)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if not result.get("ok"):
        raise click.ClickException("inbox lint found errors")


@cli.command("inbox-metabolize")
@click.argument("source", required=False, type=click.Path(path_type=Path))
@click.option("--write", is_flag=True, help="Write safe inferred front matter updates for folder inbox containers.")
@click.option("--default-worker", default="materialized_execution", type=str)
def lab_inbox_metabolize_command(source: Optional[Path], write: bool, default_worker: str) -> None:
    result = lab.inbox_metabolize(source, write=write, default_worker=default_worker)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if not result.get("ok"):
        raise click.ClickException("inbox metabolize found lint errors")


@cli.command("queue")
def lab_queue_command() -> None:
    _echo(json.dumps(lab.queue(), indent=2, ensure_ascii=False))


@cli.command("dag-plan")
@click.option("--line", default=None, type=str)
@click.option("--batch", default=None, type=str)
@click.option("--dag-root", default=None, type=str)
@click.option("--prefix", default=None, type=str)
def lab_dag_plan_command(line: Optional[str], batch: Optional[str], dag_root: Optional[str], prefix: Optional[str]) -> None:
    result = lab.dag_plan(line=line, batch=batch, dag_root=dag_root, prefix=prefix)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("dependency-reconcile")
@click.option("--task", "task_id", default=None, type=str, help="Only reconcile one pending task.")
@click.option("--apply-safe", is_flag=True, help="Promote tasks whose input requirements are already safely satisfied.")
def lab_dependency_reconcile_command(task_id: Optional[str], apply_safe: bool) -> None:
    result = lab.dependency_reconcile(task_id=task_id, apply_safe=apply_safe)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("registry-hygiene")
@click.option("--apply-safe", is_flag=True, help="Apply only mechanical stale-state cleanup fixes.")
def lab_registry_hygiene_command(apply_safe: bool) -> None:
    result = lab.registry_hygiene(apply_safe=apply_safe)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("v1-stability-report")
def lab_v1_stability_report_command() -> None:
    result = lab.v1_stability_report()
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("api-model-metrics")
@click.option("--path", "metrics_path", default=None, type=click.Path(), help="Optional api_model_calls.jsonl path.")
def lab_api_model_metrics_command(metrics_path: Optional[str]) -> None:
    result = summarize_api_metrics(Path(metrics_path) if metrics_path else None)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.group("context-index")
def lab_context_index_group() -> None:
    """Lightweight local retrieval index commands."""


@lab_context_index_group.command("build-docs")
@click.option("--repo-root", default=None, type=click.Path(path_type=Path), help="Repository root. Defaults to solution-agent root.")
@click.option("--max-docs", default=5000, type=int, help="Maximum bounded summary documents to write.")
@click.option("--max-doc-chars", default=4000, type=int, help="Maximum characters per document.")
def lab_context_index_build_docs_command(repo_root: Optional[Path], max_docs: int, max_doc_chars: int) -> None:
    result = write_context_documents(repo_root or project_root(), max_docs=max_docs, max_doc_chars=max_doc_chars)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@lab_context_index_group.command("build")
@click.option("--repo-root", default=None, type=click.Path(path_type=Path), help="Repository root. Defaults to solution-agent root.")
@click.option("--build-docs", is_flag=True, help="Rebuild context_documents.jsonl before building the SQLite index.")
@click.option("--max-docs", default=5000, type=int, help="Maximum bounded summary documents to write when --build-docs is used.")
@click.option("--max-doc-chars", default=4000, type=int, help="Maximum characters per document when --build-docs is used.")
def lab_context_index_build_command(repo_root: Optional[Path], build_docs: bool, max_docs: int, max_doc_chars: int) -> None:
    root = repo_root or project_root()
    docs_result = None
    status = context_index_status(root)
    stale = status.get("stale") if isinstance(status.get("stale"), dict) else {}
    if build_docs or not documents_path(root).exists() or bool(stale.get("documents_stale")):
        docs_result = write_context_documents(root, max_docs=max_docs, max_doc_chars=max_doc_chars)
    result = build_context_index(root)
    if docs_result:
        result["documents"] = docs_result
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@lab_context_index_group.command("query")
@click.argument("query", required=True)
@click.option("--repo-root", default=None, type=click.Path(path_type=Path), help="Repository root. Defaults to solution-agent root.")
@click.option("--limit", default=8, type=int)
def lab_context_index_query_command(query: str, repo_root: Optional[Path], limit: int) -> None:
    result = query_context_index(repo_root or project_root(), query, limit=limit)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@lab_context_index_group.command("status")
@click.option("--repo-root", default=None, type=click.Path(path_type=Path), help="Repository root. Defaults to solution-agent root.")
def lab_context_index_status_command(repo_root: Optional[Path]) -> None:
    result = context_index_status(repo_root or project_root())
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@lab_context_index_group.command("snapshot-remote")
@click.option("--repo-root", default=None, type=click.Path(path_type=Path), help="Repository root. Defaults to solution-agent root.")
@click.option("--host", required=True, type=str)
@click.option("--remote-root", default=None, type=str)
@click.option("--remote-workdir", default=None, type=str)
@click.option("--limit", default=120, type=int)
@click.option("--rebuild-index", is_flag=True, help="Rebuild context documents and SQLite index after writing the snapshot.")
def lab_context_index_snapshot_remote_command(
    repo_root: Optional[Path],
    host: str,
    remote_root: Optional[str],
    remote_workdir: Optional[str],
    limit: int,
    rebuild_index: bool,
) -> None:
    root = repo_root or project_root()
    result = snapshot_remote_workplace(root, host=host, remote_root=remote_root, remote_workdir=remote_workdir, limit=limit)
    if rebuild_index and result.get("ok"):
        result["documents"] = write_context_documents(root)
        result["index"] = build_context_index(root)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("local-maintenance-report")
def lab_local_maintenance_report_command() -> None:
    result = lab.local_maintenance_report()
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("policy-governance-lint")
@click.option(
    "--path",
    "matrix_path",
    default=Path("docs/AGENT_POLICY_GOVERNANCE_MATRIX_2026-05-13.md"),
    type=click.Path(path_type=Path),
)
def lab_policy_governance_lint_command(matrix_path: Path) -> None:
    result = lint_policy_governance_matrix(matrix_path)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if not result.get("ok"):
        raise click.ClickException("policy governance lint found errors")


@cli.command("partial-pending-audit")
@click.option("--write-report", is_flag=True, help="Write state/partial_pending_audit.json and reports/partial_pending_audit.md.")
def lab_partial_pending_audit_command(write_report: bool) -> None:
    result = lab.write_partial_pending_audit_report() if write_report else lab.partial_pending_audit()
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.group("audit")
def lab_audit_group() -> None:
    """Audit helper commands."""


@lab_audit_group.command("partial-pending")
@click.option("--list", "list_only", is_flag=True, help="List partial->pending unknown transitions and acknowledgment status.")
@click.option("--ack-current", is_flag=True, help="Acknowledge the current unknown transitions as reviewed legacy events.")
@click.option("--check", "check_only", is_flag=True, help="Check whether unacknowledged unknown transitions remain.")
@click.option("--reason", default="", type=str, help="Reason to store when acknowledging current events.")
def lab_audit_partial_pending_command(list_only: bool, ack_current: bool, check_only: bool, reason: str) -> None:
    if ack_current:
        result = lab.acknowledge_current_partial_pending_audit(
            reason=reason or "Reviewed legacy partial->pending transitions before maintenance restart",
            actor="manual_maintenance",
        )
    else:
        result = lab.partial_pending_audit_ack_status()
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if check_only and result.get("unacknowledged_count"):
        raise click.ClickException("unacknowledged partial->pending unknown transitions remain")


@cli.command("maintenance-mode")
@click.option("--enable", "enable", is_flag=True, help="Enable maintenance mode.")
@click.option("--disable", "disable", is_flag=True, help="Disable maintenance mode.")
@click.option("--reason", default="", type=str)
@click.option("--allow-action-requests", is_flag=True)
@click.option("--allow-scheduler-launch", is_flag=True)
def lab_maintenance_mode_command(enable: bool, disable: bool, reason: str, allow_action_requests: bool, allow_scheduler_launch: bool) -> None:
    if enable and disable:
        raise click.ClickException("choose only one of --enable or --disable")
    if enable:
        result = lab.set_maintenance_mode(
            True,
            reason=reason or "maintenance mode enabled",
            allow_dashboard_read=True,
            allow_action_requests=allow_action_requests,
            allow_scheduler_launch=allow_scheduler_launch,
        )
    elif disable:
        result = lab.clear_maintenance_mode()
    else:
        result = lab.load_maintenance_mode()
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("drain-mode")
@click.option("--enable", "enable", is_flag=True, help="Enable drain/no-new-launch mode.")
@click.option("--disable", "disable", is_flag=True, help="Disable drain mode.")
@click.option("--reason", default="", type=str)
def lab_drain_mode_command(enable: bool, disable: bool, reason: str) -> None:
    if enable and disable:
        raise click.ClickException("choose only one of --enable or --disable")
    if enable:
        result = lab.set_drain_mode(True, reason=reason or "drain mode enabled")
    elif disable:
        result = lab.set_drain_mode(False, reason=reason or "drain mode disabled")
    else:
        result = lab.load_drain_mode()
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("action-requests")
@click.option("--list", "list_only", is_flag=True, help="List action requests.")
@click.option("--status", default=None, type=str, help="Filter by pending/processing/done/rejected/failed.")
@click.option("--apply-safe", is_flag=True, help="Apply safe-after-validation pending requests.")
@click.option("--apply-selected", default=None, type=str, help="Apply one pending action_id.")
@click.option("--allow-launch", is_flag=True, help="Allow selected launch actions such as schedule_once.")
@click.option("--allow-dangerous", is_flag=True, help="Allow selected dangerous actions such as remote_stop or requeue.")
def lab_action_requests_command(list_only: bool, status: Optional[str], apply_safe: bool, apply_selected: Optional[str], allow_launch: bool, allow_dangerous: bool) -> None:
    if apply_safe or apply_selected:
        result = lab.process_action_requests(
            apply_safe=apply_safe,
            apply_selected=apply_selected,
            allow_launch=allow_launch,
            allow_dangerous=allow_dangerous,
        )
    else:
        result = lab.list_action_requests(status=status)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("startup-guard")
@click.option("--host", required=True, type=str)
@click.option("--remote-root", required=True, type=str)
@click.option("--remote-workdir", required=True, type=str)
@click.option("--max-starts", default=1, type=int)
@click.option("--max-running-total", default=8, type=int)
@click.option("--max-running-gpu", default=4, type=int)
@click.option("--max-running-cpu", default=4, type=int)
@click.option("--min-free-gpu-mem-gb", default=10.0, type=float)
@click.option("--allowed-gpu-ids", default="0,1,2,4", type=str)
@click.option("--max-tasks-per-gpu", default=2, type=int)
@click.option("--gpu-cache-seconds", default=120, type=int)
@click.option("--worker", default="materialized_execution", type=str)
def lab_startup_guard_command(
    host: str,
    remote_root: str,
    remote_workdir: str,
    max_starts: int,
    max_running_total: int,
    max_running_gpu: int,
    max_running_cpu: int,
    min_free_gpu_mem_gb: float,
    allowed_gpu_ids: str,
    max_tasks_per_gpu: int,
    gpu_cache_seconds: int,
    worker: str,
) -> None:
    result = lab.scheduler_startup_guard(
        host=host,
        remote_root=remote_root,
        remote_workdir=remote_workdir,
        max_starts=max_starts,
        max_running_total=max_running_total,
        max_running_gpu=max_running_gpu,
        max_running_cpu=max_running_cpu,
        min_free_gpu_mem_gb=min_free_gpu_mem_gb,
        allowed_gpu_ids=[item.strip() for item in allowed_gpu_ids.split(",") if item.strip()],
        max_tasks_per_gpu=max_tasks_per_gpu,
        gpu_snapshot_max_age_seconds=gpu_cache_seconds,
        worker=worker,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if not result.get("ok"):
        raise click.ClickException("startup guard failed")


@cli.command("bind-input")
@click.option("--task", "task_id", required=True, type=str, help="Child task to unblock.")
@click.option("--from-task", "from_task", required=True, type=str, help="Parent task whose run should be used.")
@click.option("--run-id", required=True, type=str, help="Pinned parent run id.")
def lab_bind_input_command(task_id: str, from_task: str, run_id: str) -> None:
    result = lab.bind_input(task_id=task_id, from_task=from_task, run_id=run_id)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("dashboard")
@click.option("--bind", default="127.0.0.1", type=str)
@click.option("--port", default=7860, type=int)
@click.option("--host", default="EXAMPLE_WORKER_B", type=str)
@click.option("--remote-root", default="/srv/example-project/outputs/lab-runs", type=str)
@click.option("--remote-workdir", default="/srv/example-project", type=str)
@click.option("--allowed-gpu-ids", default="0,1,2,4", type=str)
@click.option("--min-free-gpu-mem-gb", default=10.0, type=float)
@click.option("--max-starts-per-cycle", default=1, type=int)
@click.option("--max-running-total", default=8, type=int)
@click.option("--max-running-gpu", default=4, type=int)
@click.option("--max-running-cpu", default=4, type=int)
@click.option("--max-tasks-per-gpu", default=2, type=int)
@click.option("--interval-minutes", default=5, type=int)
@click.option("--model", default="qwen2.5-3b-local:latest", type=str)
@click.option("--open-browser", is_flag=True, help="Open the dashboard URL in the default browser after startup.")
@click.option("--shutdown-on-idle-seconds", default=0, type=int, help="If >0, stop dashboard after browser heartbeat is idle this long.")
def lab_dashboard_command(
    bind: str,
    port: int,
    host: str,
    remote_root: str,
    remote_workdir: str,
    allowed_gpu_ids: str,
    min_free_gpu_mem_gb: float,
    max_starts_per_cycle: int,
    max_running_total: int,
    max_running_gpu: int,
    max_running_cpu: int,
    max_tasks_per_gpu: int,
    interval_minutes: int,
    model: str,
    open_browser: bool,
    shutdown_on_idle_seconds: int,
) -> None:
    serve_dashboard(
        bind=bind,
        port=port,
        host=host,
        remote_root=remote_root,
        remote_workdir=remote_workdir,
        allowed_gpu_ids=allowed_gpu_ids,
        min_free_gpu_mem_gb=min_free_gpu_mem_gb,
        max_starts_per_cycle=max_starts_per_cycle,
        max_running_total=max_running_total,
        max_running_gpu=max_running_gpu,
        max_running_cpu=max_running_cpu,
        max_tasks_per_gpu=max_tasks_per_gpu,
        interval_minutes=interval_minutes,
        model=model,
        open_browser=open_browser,
        shutdown_on_idle_seconds=shutdown_on_idle_seconds,
    )


def _dashboard_url(bind: str, port: int) -> str:
    return f"http://{bind}:{port}/"


def _dashboard_alive(bind: str, port: int, timeout_seconds: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(_dashboard_url(bind, port), timeout=timeout_seconds) as response:
            return 200 <= int(response.status) < 500
    except Exception:
        return False


@cli.command("dashboard-open")
@click.option("--bind", default="127.0.0.1", type=str)
@click.option("--port", default=7860, type=int)
@click.option("--idle-seconds", default=45, type=int, help="Stop dashboard after this many seconds without page heartbeat.")
@click.option("--force-open", is_flag=True, help="Open a browser tab even if the dashboard is already running.")
@click.option("--no-open", is_flag=True, help="Start the dashboard server without opening a browser tab.")
def lab_dashboard_open_command(bind: str, port: int, idle_seconds: int, force_open: bool, no_open: bool) -> None:
    url = _dashboard_url(bind, port)
    was_alive = _dashboard_alive(bind, port)
    started = False
    if not was_alive:
        log_dir = state_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = log_dir / "dashboard_stdout.log"
        stderr_path = log_dir / "dashboard_stderr.log"
        shutdown_idle_seconds = 0 if no_open else max(15, idle_seconds)
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "cli.py"),
            "dashboard",
            "--bind",
            bind,
            "--port",
            str(port),
            "--shutdown-on-idle-seconds",
            str(shutdown_idle_seconds),
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(command, cwd=str(project_root()), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env, creationflags=creationflags)
        (state_dir() / "dashboard.pid").write_text(str(process.pid), encoding="utf-8")
        started = True
        deadline = time.time() + 8
        while time.time() < deadline and not _dashboard_alive(bind, port):
            time.sleep(0.25)
    opened = False
    if not no_open and (started or force_open):
        webbrowser.open(url)
        opened = True
    _echo(
        json.dumps(
            {
                "url": url,
                "opened": opened,
                "started": started,
                "already_running": was_alive and not started,
                "idle_seconds": 0 if no_open else idle_seconds,
                "no_open": no_open,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@cli.command("dispatch")
@click.argument("task_id")
@click.option("--worker", default="codex", type=str)
@click.option("--no-submit", is_flag=True, help="Generate dispatch files without submitting to Pueue.")
@click.option("--force-worker-override", is_flag=True, help="Allow dispatch with a worker not declared by the task.")
@click.option("--reason", default="", type=str, help="Required when --force-worker-override is used.")
def lab_dispatch_command(task_id: str, worker: str, no_submit: bool, force_worker_override: bool, reason: str) -> None:
    result = lab.dispatch(
        task_id,
        worker=worker,
        submit=not no_submit,
        force_worker_override=force_worker_override,
        override_reason=reason,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if not result["pueue"].get("submitted"):
        _echo("[labctl] pueue not available; run manually:")
        _echo(f"python cli.py run-worker {task_id} --worker {worker}")


@cli.command("run-worker")
@click.argument("task_id")
@click.option("--worker", default="codex", type=str)
def lab_run_worker_command(task_id: str, worker: str) -> None:
    result = lab.run_worker(task_id, worker=worker)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("run-materialized-execution-task")
@click.option("--task-path", required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--out-dir", required=True, type=click.Path(path_type=Path))
@click.option("--workspace-root", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--materializer-root", default=None, type=click.Path(path_type=Path))
@click.option("--policy", "policy_path", default=None, type=click.Path(path_type=Path))
@click.option("--env-file", default=None, type=click.Path(path_type=Path))
def lab_run_materialized_execution_task_command(
    task_path: Path,
    out_dir: Path,
    workspace_root: Path,
    materializer_root: Optional[Path],
    policy_path: Optional[Path],
    env_file: Optional[Path],
) -> None:
    result = lab.run_materialized_execution_task(
        task_path=task_path.resolve(),
        out_dir=out_dir.resolve(),
        workspace_root=workspace_root.resolve(),
        materializer_root=(materializer_root or (out_dir / "materializer_runs")).resolve(),
        policy_path=policy_path.resolve() if policy_path else None,
        env_file=env_file.resolve() if env_file else None,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("run-next")
@click.option("--worker", default="codex", type=str)
@click.option("--no-collect", is_flag=True, help="Do not collect after worker exits.")
@click.option("--line", default=None, type=str)
@click.option("--batch", default=None, type=str)
@click.option("--dag-root", default=None, type=str)
@click.option("--prefix", default=None, type=str)
def lab_run_next_command(worker: str, no_collect: bool, line: Optional[str], batch: Optional[str], dag_root: Optional[str], prefix: Optional[str]) -> None:
    result = lab.run_next(worker=worker, collect_after=not no_collect, line=line, batch=batch, dag_root=dag_root, prefix=prefix)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("run-queue")
@click.option("--worker", default="codex", type=str)
@click.option("--limit", default=None, type=int)
@click.option("--stop-on-fail", is_flag=True)
@click.option("--line", default=None, type=str)
@click.option("--batch", default=None, type=str)
@click.option("--dag-root", default=None, type=str)
@click.option("--prefix", default=None, type=str)
def lab_run_queue_command(worker: str, limit: Optional[int], stop_on_fail: bool, line: Optional[str], batch: Optional[str], dag_root: Optional[str], prefix: Optional[str]) -> None:
    result = lab.run_queue(worker=worker, limit=limit, stop_on_fail=stop_on_fail, line=line, batch=batch, dag_root=dag_root, prefix=prefix)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("run-remote")
@click.argument("task_id")
@click.option("--host", required=True, type=str, help="SSH host, e.g. user@example.com.")
@click.option("--remote-root", required=True, type=str, help="Remote folder that will contain task output dirs.")
@click.option("--remote-workdir", default=None, type=str, help="Remote project directory where Codex should work.")
@click.option("--detach", is_flag=True, help="Start remote task in background and return immediately.")
@click.option("--worker", default="codex", type=str)
@click.option("--phase", "execution_phase", default="full", type=click.Choice(["full", "prepare_code", "execute_run"]), help="Worker action phase.")
@click.option("--force-worker-override", is_flag=True, help="Allow remote dispatch with a worker not declared by the task.")
@click.option("--reason", default="", type=str, help="Required when --force-worker-override is used.")
def lab_run_remote_command(task_id: str, host: str, remote_root: str, remote_workdir: Optional[str], detach: bool, worker: str, execution_phase: str, force_worker_override: bool, reason: str) -> None:
    result = lab.run_remote(
        task_id,
        host=host,
        remote_root=remote_root,
        worker=worker,
        remote_workdir=remote_workdir,
        detach=detach,
        execution_phase=execution_phase,
        force_worker_override=force_worker_override,
        override_reason=reason,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("run-next-remote")
@click.option("--host", required=True, type=str, help="SSH host, e.g. configured alias.")
@click.option("--remote-root", required=True, type=str, help="Remote folder that will contain task output dirs.")
@click.option("--remote-workdir", default=None, type=str, help="Remote project directory where Codex should work.")
@click.option("--foreground", is_flag=True, help="Run in foreground instead of nohup background mode.")
@click.option("--worker", default="codex", type=str)
@click.option("--line", default=None, type=str)
@click.option("--batch", default=None, type=str)
@click.option("--dag-root", default=None, type=str)
@click.option("--prefix", default=None, type=str)
def lab_run_next_remote_command(host: str, remote_root: str, remote_workdir: Optional[str], foreground: bool, worker: str, line: Optional[str], batch: Optional[str], dag_root: Optional[str], prefix: Optional[str]) -> None:
    result = lab.run_next_remote(
        host=host,
        remote_root=remote_root,
        worker=worker,
        remote_workdir=remote_workdir,
        detach=not foreground,
        line=line,
        batch=batch,
        dag_root=dag_root,
        prefix=prefix,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("schedule-remote")
@click.option("--host", required=True, type=str, help="SSH host, e.g. configured alias.")
@click.option("--remote-root", required=True, type=str, help="Remote folder that will contain task output dirs.")
@click.option("--remote-workdir", default=None, type=str, help="Remote project directory where Codex should work.")
@click.option("--max-starts", default=1, type=int, help="Maximum new tasks to launch in this scheduler pass.")
@click.option("--max-running-total", default=2, type=int, help="Maximum total running tasks after this pass.")
@click.option("--max-running-gpu", default=1, type=int, help="Maximum running GPU tasks after this pass.")
@click.option("--max-running-cpu", default=2, type=int, help="Maximum running CPU tasks after this pass.")
@click.option("--min-free-gpu-mem-gb", default=10.0, type=float, help="Minimum free GPU memory required before launching a GPU task.")
@click.option("--allowed-gpu-ids", default="0,1,2,4", type=str, help="Comma-separated nvidia-smi physical GPU ids allowed for scheduling.")
@click.option("--max-tasks-per-gpu", default=2, type=int, help="Maximum own tasks per GPU when there are no external processes on that GPU.")
@click.option("--gpu-cache-seconds", default=120, type=int, help="Use cached GPU snapshot for dry-runs if it is newer than this many seconds.")
@click.option("--execute", is_flag=True, help="Actually start planned tasks. Without this, only print the plan.")
@click.option("--worker", default="codex", type=str)
@click.option("--line", default=None, type=str)
@click.option("--batch", default=None, type=str)
@click.option("--dag-root", default=None, type=str)
@click.option("--prefix", default=None, type=str)
def lab_schedule_remote_command(
    host: str,
    remote_root: str,
    remote_workdir: Optional[str],
    max_starts: int,
    max_running_total: int,
    max_running_gpu: int,
    max_running_cpu: int,
    min_free_gpu_mem_gb: float,
    allowed_gpu_ids: str,
    max_tasks_per_gpu: int,
    gpu_cache_seconds: int,
    execute: bool,
    worker: str,
    line: Optional[str],
    batch: Optional[str],
    dag_root: Optional[str],
    prefix: Optional[str],
) -> None:
    allowed = [item.strip() for item in allowed_gpu_ids.split(",") if item.strip()]
    reconnect_reconcile = None
    if execute:
        guard = lab.scheduler_startup_guard(
            host=host,
            remote_root=remote_root,
            remote_workdir=remote_workdir,
            max_starts=max_starts,
            max_running_total=max_running_total,
            max_running_gpu=max_running_gpu,
            max_running_cpu=max_running_cpu,
            min_free_gpu_mem_gb=min_free_gpu_mem_gb,
            allowed_gpu_ids=allowed,
            max_tasks_per_gpu=max_tasks_per_gpu,
            gpu_snapshot_max_age_seconds=gpu_cache_seconds,
            worker=worker,
        )
        if not guard.get("ok"):
            _echo(json.dumps({"error": "startup guard failed", "startup_guard": guard}, indent=2, ensure_ascii=False))
            raise click.ClickException("startup guard failed")
        reconnect_reconcile = lab.remote_reconcile_running_tasks()
    result = lab.schedule_remote(
        host=host,
        remote_root=remote_root,
        worker=worker,
        remote_workdir=remote_workdir,
        max_starts=max_starts,
        max_running_total=max_running_total,
        max_running_gpu=max_running_gpu,
        max_running_cpu=max_running_cpu,
        min_free_gpu_mem_gb=min_free_gpu_mem_gb,
        allowed_gpu_ids=allowed,
        max_tasks_per_gpu=max_tasks_per_gpu,
        gpu_snapshot_max_age_seconds=gpu_cache_seconds if not execute else 0,
        dry_run=not execute,
        line=line,
        batch=batch,
        dag_root=dag_root,
        prefix=prefix,
    )
    if reconnect_reconcile is not None:
        result = {"reconnect_reconcile": reconnect_reconcile, "schedule": result}
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("remote-preflight")
@click.option("--host", required=True, type=str, help="SSH host alias.")
@click.option("--remote-root", required=True, type=str, help="Remote output root.")
@click.option("--remote-workdir", required=True, type=str, help="Remote project workdir.")
@click.option("--allowed-gpu-ids", default="0,1,2,4", type=str)
@click.option("--expected-uid", default=None, type=str)
def lab_remote_preflight_command(host: str, remote_root: str, remote_workdir: str, allowed_gpu_ids: str, expected_uid: Optional[str]) -> None:
    allowed = [item.strip() for item in allowed_gpu_ids.split(",") if item.strip()]
    result = lab.remote_target_preflight(
        host=host,
        remote_workdir=remote_workdir,
        remote_root=remote_root,
        allowed_gpu_ids=allowed,
        expected_uid=expected_uid,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if not result.get("ok"):
        raise click.ClickException("remote preflight failed")


@cli.command("workplace-init")
@click.option("--host", required=True, type=str, help="SSH host alias.")
@click.option("--remote-root", required=True, type=str, help="Remote output root.")
@click.option("--remote-workdir", required=True, type=str, help="Remote project workdir.")
@click.option("--allowed-gpu-ids", default="0,1,2,4", type=str)
@click.option("--expected-uid", default=None, type=str)
@click.option("--no-write-marker", is_flag=True, help="Do not write .solution_agent_project in the remote workdir.")
@click.option("--no-sync-profile", is_flag=True, help="Do not sync workplace_profile.json/report to the remote .codex-orchestrator folder.")
@click.option(
    "--sync-materializer-env/--no-sync-materializer-env",
    default=False,
    help="Provision remote ~/.remote_materializer/.env from local ~/.remote_materializer/.env and add the HMAC auth token. Default only checks the remote env.",
)
@click.option("--codex-smoke", is_flag=True, help="Run a tiny remote Codex smoke test. This may spend tokens.")
def lab_workplace_init_command(
    host: str,
    remote_root: str,
    remote_workdir: str,
    allowed_gpu_ids: str,
    expected_uid: Optional[str],
    no_write_marker: bool,
    no_sync_profile: bool,
    sync_materializer_env: bool,
    codex_smoke: bool,
) -> None:
    allowed = [item.strip() for item in allowed_gpu_ids.split(",") if item.strip()]
    result = lab.remote_workplace_init(
        host=host,
        remote_workdir=remote_workdir,
        remote_root=remote_root,
        allowed_gpu_ids=allowed,
        expected_uid=expected_uid,
        write_marker=not no_write_marker,
        sync_profile=not no_sync_profile,
        sync_materializer_env=sync_materializer_env,
        codex_smoke=codex_smoke,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))
    if not result.get("ok"):
        raise click.ClickException("workplace init found missing required items")


@cli.command("scheduler-advisor")
@click.option("--host", required=True, type=str, help="SSH host, e.g. configured alias.")
@click.option("--remote-root", required=True, type=str, help="Remote folder that will contain task output dirs.")
@click.option("--remote-workdir", default=None, type=str, help="Remote project directory where Codex should work.")
@click.option("--max-starts", default=4, type=int, help="Maximum candidate starts to consider.")
@click.option("--max-running-total", default=8, type=int)
@click.option("--max-running-gpu", default=4, type=int)
@click.option("--max-running-cpu", default=4, type=int)
@click.option("--min-free-gpu-mem-gb", default=10.0, type=float)
@click.option("--allowed-gpu-ids", default="0,1,2,4", type=str)
@click.option("--max-tasks-per-gpu", default=2, type=int)
@click.option("--gpu-cache-seconds", default=120, type=int, help="Use cached GPU snapshot if it is fresh enough.")
def lab_scheduler_advisor_command(
    host: str,
    remote_root: str,
    remote_workdir: Optional[str],
    max_starts: int,
    max_running_total: int,
    max_running_gpu: int,
    max_running_cpu: int,
    min_free_gpu_mem_gb: float,
    allowed_gpu_ids: str,
    max_tasks_per_gpu: int,
    gpu_cache_seconds: int,
) -> None:
    allowed = [item.strip() for item in allowed_gpu_ids.split(",") if item.strip()]
    plan = lab.schedule_remote(
        host=host,
        remote_root=remote_root,
        remote_workdir=remote_workdir,
        max_starts=max_starts,
        max_running_total=max_running_total,
        max_running_gpu=max_running_gpu,
        max_running_cpu=max_running_cpu,
        min_free_gpu_mem_gb=min_free_gpu_mem_gb,
        allowed_gpu_ids=allowed,
        max_tasks_per_gpu=max_tasks_per_gpu,
        gpu_snapshot_max_age_seconds=gpu_cache_seconds,
        dry_run=True,
    )
    result = lab.scheduler_advisory(plan)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("schedule-loop-remote")
@click.option("--host", required=True, type=str, help="SSH host, e.g. configured alias.")
@click.option("--remote-root", required=True, type=str, help="Remote folder that will contain task output dirs.")
@click.option("--remote-workdir", required=True, type=str, help="Remote project directory where Codex should work.")
@click.option("--max-starts-per-cycle", default=4, type=int, help="Maximum new tasks to launch per scheduler cycle.")
@click.option("--max-running-total", default=8, type=int, help="Maximum total running tasks.")
@click.option("--max-running-gpu", default=8, type=int, help="Maximum running GPU tasks.")
@click.option("--max-running-cpu", default=2, type=int, help="Maximum running CPU tasks.")
@click.option("--min-free-gpu-mem-gb", default=10.0, type=float, help="Minimum free GPU memory required before launching a GPU task.")
@click.option("--allowed-gpu-ids", default="0,1,2,4", type=str, help="Comma-separated nvidia-smi physical GPU ids allowed for scheduling.")
@click.option("--max-tasks-per-gpu", default=2, type=int, help="Maximum own tasks per GPU when there are no external processes on that GPU.")
@click.option("--interval-minutes", default=15, type=int, help="Minutes between scheduler cycles.")
@click.option("--max-cycles", default=0, type=int, help="0 means keep looping.")
@click.option("--model", default="qwen2.5-3b-local:latest", type=str, help="Local Ollama model for supervision.")
@click.option("--gpu-cache-seconds", default=120, type=int, help="Use watcher GPU cache if it is fresh enough before launching.")
@click.option("--worker", default="materialized_execution", type=str, help="Remote worker to schedule. Use materialized_execution by default.")
@click.option("--dry-run", is_flag=True, help="Only print collection/scheduling decisions once.")
def lab_schedule_loop_remote_command(
    host: str,
    remote_root: str,
    remote_workdir: str,
    max_starts_per_cycle: int,
    max_running_total: int,
    max_running_gpu: int,
    max_running_cpu: int,
    min_free_gpu_mem_gb: float,
    allowed_gpu_ids: str,
    max_tasks_per_gpu: int,
    interval_minutes: int,
    max_cycles: int,
    model: str,
    gpu_cache_seconds: int,
    worker: str,
    dry_run: bool,
) -> None:
    allowed = [item.strip() for item in allowed_gpu_ids.split(",") if item.strip()]
    lock = {"acquired": True}
    if not dry_run:
        guard = lab.scheduler_startup_guard(
            host=host,
            remote_root=remote_root,
            remote_workdir=remote_workdir,
            max_starts=max_starts_per_cycle,
            max_running_total=max_running_total,
            max_running_gpu=max_running_gpu,
            max_running_cpu=max_running_cpu,
            min_free_gpu_mem_gb=min_free_gpu_mem_gb,
            allowed_gpu_ids=allowed,
            max_tasks_per_gpu=max_tasks_per_gpu,
            gpu_snapshot_max_age_seconds=gpu_cache_seconds,
            worker=worker,
        )
        if not guard.get("ok"):
            _echo(json.dumps({"error": "startup guard failed", "startup_guard": guard}, indent=2, ensure_ascii=False))
            raise click.ClickException("startup guard failed")
        lock = lab.acquire_scheduler_lock("schedule-loop-remote")
        if not lock.get("acquired"):
            _echo(json.dumps({"error": "scheduler already running", "lock": lock}, indent=2, ensure_ascii=False))
            raise click.ClickException("scheduler lock is held by another process")
    reconnect_reconcile = None
    if not dry_run:
        reconnect_reconcile = lab.remote_reconcile_running_tasks()
    cycle = 0
    try:
        while True:
            lab.refresh_scheduler_lock()
            supervision = lab.supervise_running_remote_tasks(model=model, collect_finished=not dry_run)
            schedule = lab.schedule_remote(
                host=host,
                remote_root=remote_root,
                remote_workdir=remote_workdir,
                max_starts=max_starts_per_cycle,
                max_running_total=max_running_total,
                max_running_gpu=max_running_gpu,
                max_running_cpu=max_running_cpu,
                min_free_gpu_mem_gb=min_free_gpu_mem_gb,
                allowed_gpu_ids=allowed,
                max_tasks_per_gpu=max_tasks_per_gpu,
                gpu_snapshot_max_age_seconds=gpu_cache_seconds,
                worker=worker,
                dry_run=dry_run,
            )
            payload = {"cycle": cycle + 1, "lock": lock, "supervision": supervision, "schedule": schedule}
            if reconnect_reconcile is not None:
                payload["reconnect_reconcile"] = reconnect_reconcile
                reconnect_reconcile = None
            _echo(json.dumps(payload, indent=2, ensure_ascii=False))
            cycle += 1
            if dry_run or (max_cycles and cycle >= max_cycles):
                break
            sleep_until = time.time() + max(60, interval_minutes * 60)
            while time.time() < sleep_until:
                lab.refresh_scheduler_lock()
                time.sleep(min(60, max(1, sleep_until - time.time())))
    finally:
        if not dry_run:
            lab.release_scheduler_lock()


@cli.command("scheduler-start")
@click.option("--host", required=True, type=str, help="SSH host, e.g. configured alias.")
@click.option("--remote-root", required=True, type=str, help="Remote folder that will contain task output dirs.")
@click.option("--remote-workdir", required=True, type=str, help="Remote project directory where Codex should work.")
@click.option("--max-starts-per-cycle", default=4, type=int)
@click.option("--max-running-total", default=8, type=int)
@click.option("--max-running-gpu", default=8, type=int)
@click.option("--max-running-cpu", default=2, type=int)
@click.option("--min-free-gpu-mem-gb", default=10.0, type=float)
@click.option("--allowed-gpu-ids", default="0,1,2,4", type=str)
@click.option("--max-tasks-per-gpu", default=2, type=int)
@click.option("--interval-minutes", default=15, type=int)
@click.option("--max-cycles", default=0, type=int)
@click.option("--model", default="qwen2.5-3b-local:latest", type=str)
@click.option("--gpu-cache-seconds", default=120, type=int)
@click.option("--worker", default="materialized_execution", type=str)
@click.option("--skip-preflight", is_flag=True, help="Start scheduler without remote target preflight.")
@click.option("--skip-v1-stability-check", is_flag=True, help="Start scheduler even when the V1 stability report has issues.")
def lab_scheduler_start_command(
    host: str,
    remote_root: str,
    remote_workdir: str,
    max_starts_per_cycle: int,
    max_running_total: int,
    max_running_gpu: int,
    max_running_cpu: int,
    min_free_gpu_mem_gb: float,
    allowed_gpu_ids: str,
    max_tasks_per_gpu: int,
    interval_minutes: int,
    max_cycles: int,
    model: str,
    gpu_cache_seconds: int,
    worker: str,
    skip_preflight: bool,
    skip_v1_stability_check: bool,
) -> None:
    result = lab.scheduler_start(
        host=host,
        remote_root=remote_root,
        remote_workdir=remote_workdir,
        max_starts_per_cycle=max_starts_per_cycle,
        max_running_total=max_running_total,
        max_running_gpu=max_running_gpu,
        max_running_cpu=max_running_cpu,
        min_free_gpu_mem_gb=min_free_gpu_mem_gb,
        allowed_gpu_ids=allowed_gpu_ids,
        max_tasks_per_gpu=max_tasks_per_gpu,
        interval_minutes=interval_minutes,
        max_cycles=max_cycles,
        model=model,
        gpu_snapshot_cache_seconds=gpu_cache_seconds,
        worker=worker,
        preflight=not skip_preflight,
        v1_stability_check=not skip_v1_stability_check,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("scheduler-status")
def lab_scheduler_status_command() -> None:
    _echo(json.dumps(lab.scheduler_status(), indent=2, ensure_ascii=False))


@cli.command("remote-watch-loop")
@click.option("--host", required=True, type=str, help="SSH host alias.")
@click.option("--allowed-gpu-ids", default="0,1,2,4", type=str, help="Comma-separated nvidia-smi ids to keep in cache.")
@click.option("--interval-seconds", default=30, type=int, help="Seconds between streamed GPU frames.")
@click.option("--cooldown-seconds", default=600, type=int, help="Cooldown after repeated SSH stream failures.")
@click.option("--max-failures", default=3, type=int, help="Failures before cooldown.")
@click.option("--max-cycles", default=0, type=int, help="0 means keep streaming until stopped.")
def lab_remote_watch_loop_command(
    host: str,
    allowed_gpu_ids: str,
    interval_seconds: int,
    cooldown_seconds: int,
    max_failures: int,
    max_cycles: int,
) -> None:
    allowed = [item.strip() for item in allowed_gpu_ids.split(",") if item.strip()]
    result = lab.remote_watch_loop(
        host=host,
        allowed_gpu_ids=allowed,
        interval_seconds=interval_seconds,
        cooldown_seconds=cooldown_seconds,
        max_failures=max_failures,
        max_cycles=max_cycles,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("remote-watch-start")
@click.option("--host", required=True, type=str, help="SSH host alias.")
@click.option("--allowed-gpu-ids", default="0,1,2,4", type=str, help="Comma-separated nvidia-smi ids to keep in cache.")
@click.option("--interval-seconds", default=30, type=int, help="Seconds between streamed GPU frames.")
@click.option("--cooldown-seconds", default=600, type=int, help="Cooldown after repeated SSH stream failures.")
@click.option("--max-failures", default=3, type=int, help="Failures before cooldown.")
def lab_remote_watch_start_command(host: str, allowed_gpu_ids: str, interval_seconds: int, cooldown_seconds: int, max_failures: int) -> None:
    result = lab.remote_watch_start(
        host=host,
        allowed_gpu_ids=allowed_gpu_ids,
        interval_seconds=interval_seconds,
        cooldown_seconds=cooldown_seconds,
        max_failures=max_failures,
    )
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("remote-watch-status")
def lab_remote_watch_status_command() -> None:
    _echo(json.dumps(lab.remote_watch_status(), indent=2, ensure_ascii=False))


@cli.command("remote-watch-stop")
def lab_remote_watch_stop_command() -> None:
    _echo(json.dumps(lab.remote_watch_stop(), indent=2, ensure_ascii=False))


@cli.command("ssh-usage")
@click.option("--last", default=200, type=int, help="Number of recent remote calls to summarize.")
def lab_ssh_usage_command(last: int) -> None:
    path = state_dir() / "remote_call_log.jsonl"
    if not path.exists():
        _echo(json.dumps({"log": str(path), "count": 0, "note": "No SSH/SCP calls have been logged yet."}, indent=2, ensure_ascii=False))
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    rows = []
    for line in lines[-max(1, last):]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    by_caller: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_host: dict[str, int] = {}
    for item in rows:
        by_caller[str(item.get("caller") or "unknown")] = by_caller.get(str(item.get("caller") or "unknown"), 0) + 1
        by_kind[str(item.get("kind") or "unknown")] = by_kind.get(str(item.get("kind") or "unknown"), 0) + 1
        by_host[str(item.get("host") or "unknown")] = by_host.get(str(item.get("host") or "unknown"), 0) + 1
    payload = {
        "log": str(path),
        "total_logged": len(lines),
        "sampled": len(rows),
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_host": dict(sorted(by_host.items(), key=lambda kv: (-kv[1], kv[0]))),
        "top_callers": dict(sorted(by_caller.items(), key=lambda kv: (-kv[1], kv[0]))[:20]),
        "recent": rows[-10:],
    }
    _echo(json.dumps(payload, indent=2, ensure_ascii=False))


@cli.command("scheduler-stop")
def lab_scheduler_stop_command() -> None:
    _echo(json.dumps(lab.scheduler_stop(), indent=2, ensure_ascii=False))


@cli.command("resume-remote")
@click.option("--host", required=True, type=str, help="SSH host, e.g. configured alias.")
@click.option("--remote-root", required=True, type=str, help="Remote folder that contains task output dirs.")
@click.option("--remote-workdir", required=True, type=str, help="Remote project directory where Codex should work.")
@click.option("--max-starts-per-cycle", default=1, type=int)
@click.option("--max-running-total", default=8, type=int)
@click.option("--max-running-gpu", default=4, type=int)
@click.option("--max-running-cpu", default=4, type=int)
@click.option("--min-free-gpu-mem-gb", default=10.0, type=float)
@click.option("--allowed-gpu-ids", default="0,1,2,4", type=str)
@click.option("--max-tasks-per-gpu", default=2, type=int)
@click.option("--interval-minutes", default=5, type=int)
@click.option("--model", default="qwen2.5-3b-local:latest", type=str)
@click.option("--no-start", is_flag=True, help="Only reconcile current remote state; do not start the scheduler loop.")
def lab_resume_remote_command(
    host: str,
    remote_root: str,
    remote_workdir: str,
    max_starts_per_cycle: int,
    max_running_total: int,
    max_running_gpu: int,
    max_running_cpu: int,
    min_free_gpu_mem_gb: float,
    allowed_gpu_ids: str,
    max_tasks_per_gpu: int,
    interval_minutes: int,
    model: str,
    no_start: bool,
) -> None:
    reconnect_reconcile = lab.remote_reconcile_running_tasks()
    supervision = lab.supervise_running_remote_tasks(model=model, collect_finished=True)
    scheduler_before = lab.scheduler_status()
    scheduler = None
    if not no_start and not scheduler_before.get("running"):
        scheduler = lab.scheduler_start(
            host=host,
            remote_root=remote_root,
            remote_workdir=remote_workdir,
            max_starts_per_cycle=max_starts_per_cycle,
            max_running_total=max_running_total,
            max_running_gpu=max_running_gpu,
            max_running_cpu=max_running_cpu,
            min_free_gpu_mem_gb=min_free_gpu_mem_gb,
            allowed_gpu_ids=allowed_gpu_ids,
            max_tasks_per_gpu=max_tasks_per_gpu,
            interval_minutes=interval_minutes,
            max_cycles=0,
            model=model,
        )
    elif not no_start:
        scheduler = {"started": False, "reason": "scheduler already running", "status": scheduler_before}
    _echo(
        json.dumps(
            {
                "supervision": supervision,
                "reconnect_reconcile": reconnect_reconcile,
                "scheduler_before": scheduler_before,
                "scheduler": scheduler,
                "queue": lab.queue(),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


@cli.command("remote-bootstrap-context")
@click.option("--host", required=True, type=str, help="SSH host alias.")
@click.option("--remote-workdir", required=True, type=str, help="Remote project directory.")
def lab_remote_bootstrap_context_command(host: str, remote_workdir: str) -> None:
    result = lab.remote_bootstrap_context(host=host, remote_workdir=remote_workdir)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("profile-show")
def lab_profile_show_command() -> None:
    ensure_local_memory_files()
    _echo(codex_worker_profile_path().read_text(encoding="utf-8"))


@cli.command("profile-sync")
@click.option("--host", required=True, type=str, help="SSH host alias.")
@click.option("--remote-workdir", required=True, type=str, help="Remote project directory.")
def lab_profile_sync_command(host: str, remote_workdir: str) -> None:
    result = lab.remote_bootstrap_context(host=host, remote_workdir=remote_workdir)
    profile = result.get("copied", {}).get("codex_worker_profile") if isinstance(result, dict) else None
    _echo(json.dumps({"host": host, "remote_workdir": remote_workdir, "profile": profile, "bootstrap": result}, indent=2, ensure_ascii=False))


@cli.command("sync-pull-remote")
@click.argument("task_id")
@click.option("--host", required=True, type=str, help="SSH host, e.g. user@example.com.")
@click.option("--remote-root", required=True, type=str, help="Remote folder containing task dirs.")
def lab_sync_pull_remote_command(task_id: str, host: str, remote_root: str) -> None:
    result = lab.sync_pull_remote(task_id, host=host, remote_root=remote_root)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("remote-status")
@click.argument("task_id")
@click.option("--host", required=True, type=str, help="SSH host alias.")
@click.option("--remote-root", required=True, type=str, help="Remote folder containing task output dirs.")
def lab_remote_status_command(task_id: str, host: str, remote_root: str) -> None:
    result = lab.remote_status(task_id, host=host, remote_root=remote_root)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("remote-stop")
@click.argument("task_id")
@click.option("--host", required=True, type=str, help="SSH host alias.")
@click.option("--remote-root", required=True, type=str, help="Remote folder containing task output dirs.")
@click.option("--reason", default="人工暫停", type=str, help="Reason recorded in remote state and local registry.")
@click.option("--keep-partial", is_flag=True, help="Keep task in partial instead of moving it back to pending.")
def lab_remote_stop_command(task_id: str, host: str, remote_root: str, reason: str, keep_partial: bool) -> None:
    result = lab.remote_stop(task_id, host=host, remote_root=remote_root, reason=reason, reset_to_pending=not keep_partial)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("remote-monitor-once")
@click.argument("task_id")
@click.option("--host", required=True, type=str, help="SSH host alias.")
@click.option("--remote-root", required=True, type=str, help="Remote folder containing task output dirs.")
def lab_remote_monitor_once_command(task_id: str, host: str, remote_root: str) -> None:
    result = lab.remote_monitor_once(task_id, host=host, remote_root=remote_root)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("supervise-remote")
@click.argument("task_id")
@click.option("--host", required=True, type=str, help="SSH host alias.")
@click.option("--remote-root", required=True, type=str, help="Remote folder containing task output dirs.")
@click.option("--model", default="qwen2.5-3b-local:latest", type=str, help="Local Ollama model for supervision.")
def lab_supervise_remote_command(task_id: str, host: str, remote_root: str, model: str) -> None:
    result = lab.supervise_remote(task_id, host=host, remote_root=remote_root, model=model)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("supervise-running-remote")
@click.option("--model", default="qwen2.5-3b-local:latest", type=str, help="Local Ollama model for supervision.")
@click.option("--no-collect", is_flag=True, help="Only inspect running tasks; do not pull/collect finished tasks.")
def lab_supervise_running_remote_command(model: str, no_collect: bool) -> None:
    result = lab.supervise_running_remote_tasks(model=model, collect_finished=not no_collect)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("supervise-loop")
@click.argument("task_id")
@click.option("--host", required=True, type=str, help="SSH host alias.")
@click.option("--remote-root", required=True, type=str, help="Remote folder containing task output dirs.")
@click.option("--model", default="qwen2.5-3b-local:latest", type=str, help="Local Ollama model for supervision.")
@click.option("--max-checks", default=0, type=int, help="0 means run until collect/warn/stop/confirm is needed.")
def lab_supervise_loop_command(task_id: str, host: str, remote_root: str, model: str, max_checks: int) -> None:
    checks = 0
    while True:
        result = lab.supervise_remote(task_id, host=host, remote_root=remote_root, model=model)
        _echo(json.dumps(result, indent=2, ensure_ascii=False))
        decision = result.get("decision", {})
        action = decision.get("action")
        if action == "collect":
            pull = lab.sync_pull_remote(task_id, host=host, remote_root=remote_root)
            collected = lab.collect(task_id)
            lab.report_index()
            lab.report_latest()
            _echo(json.dumps({"pull": pull, "collect": collected}, indent=2, ensure_ascii=False))
            break
        if action in {"warn", "stop_recommended", "codex_confirm"}:
            break
        checks += 1
        if max_checks and checks >= max_checks:
            break
        minutes = int(decision.get("next_check_minutes") or 15)
        if action == "wait_gpu":
            minutes = max(minutes, 30)
        time.sleep(max(60, minutes * 60))


@cli.command("collect")
@click.argument("task_id")
def lab_collect_command(task_id: str) -> None:
    result = lab.collect(task_id)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("manual-pause")
@click.argument("task_id")
@click.option("--reason", default="人工暫停", type=str, help="Reason recorded in registry/run history.")
@click.option("--keep-partial", is_flag=True, help="Keep the task in partial instead of moving it back to pending.")
def lab_manual_pause_command(task_id: str, reason: str, keep_partial: bool) -> None:
    result = lab.manual_pause(task_id, reason=reason, reset_to_pending=not keep_partial, explicit_requeue=not keep_partial)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("requeue-completed")
@click.argument("task_id")
@click.option("--reason", default="Explicitly requeued completed task", type=str, help="Reason recorded in registry history.")
def lab_requeue_completed_command(task_id: str, reason: str) -> None:
    result = lab.requeue_completed(task_id, reason=reason)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("report")
@click.argument("task_id")
def lab_report_command(task_id: str) -> None:
    path = lab.report(task_id)
    _echo(f"[labctl] report: {path}")


@cli.command("report-index")
def lab_report_index_command() -> None:
    path = lab.report_index()
    _echo(f"[labctl] index: {path}")


@cli.command("report-latest")
def lab_report_latest_command() -> None:
    path = lab.report_latest()
    _echo(f"[labctl] latest: {path}")


@cli.command("export-gpt-bundle")
@click.option("--name", default=None, type=str, help="Optional export folder name under exports/.")
@click.option("--no-running", is_flag=True, help="Do not include running tasks.")
@click.option("--no-partial", is_flag=True, help="Do not include partial tasks.")
def lab_export_gpt_bundle_command(name: Optional[str], no_running: bool, no_partial: bool) -> None:
    result = lab.export_gpt_bundle(name=name, include_running=not no_running, include_partial=not no_partial)
    _echo(json.dumps(result, indent=2, ensure_ascii=False))


# ── Execution Port ────────────────────────────────────────────────────────────

@cli.command("register-execution")
@click.argument("spec_path", type=click.Path(exists=True, path_type=Path))
@click.option("--registry", "registry_path", default=None, type=click.Path(path_type=Path),
              help="Path to script_registry.yaml (defaults to built-in).")
@click.option("--executions-dir", default=None, type=click.Path(path_type=Path),
              help="Directory to store job dirs (defaults to ./executions).")
def register_execution_command(
    spec_path: Path,
    registry_path: Optional[Path],
    executions_dir: Optional[Path],
) -> None:
    """Validate and register an execution_spec.yaml. Prints job_id."""
    from .execution_port.registry import register_execution, _EXECUTIONS_DIR
    from .execution_port.spec import SpecValidationError
    try:
        job_id = register_execution(
            spec_path,
            registry_path=registry_path,
            executions_dir=executions_dir or _EXECUTIONS_DIR,
        )
        _echo(job_id)
    except SpecValidationError as e:
        click.echo(f"[error] {e}", err=True)
        sys.exit(1)


@cli.command("run-execution")
@click.option("--job-id", required=True, type=str, help="Job ID returned by register-execution.")
@click.option("--executions-dir", default=None, type=click.Path(path_type=Path),
              help="Directory that contains job dirs.")
def run_execution_command(job_id: str, executions_dir: Optional[Path]) -> None:
    """Launch the execution wrapper for a registered job."""
    from .execution_port.registry import _EXECUTIONS_DIR
    from .execution_port.safe_runner import run_execution
    base = executions_dir or _EXECUTIONS_DIR
    job_dir = base / job_id
    if not job_dir.exists():
        click.echo(f"[error] job dir not found: {job_dir}", err=True)
        sys.exit(1)
    run_execution(job_dir)
    _echo(f"[execution] launched: {job_id}")


@cli.command("monitor-execution")
@click.option("--job-id", required=True, type=str, help="Job ID to monitor.")
@click.option("--executions-dir", default=None, type=click.Path(path_type=Path),
              help="Directory that contains job dirs.")
@click.option("--once", "mode", flag_value="once", default=True, help="Single check (default).")
@click.option("--loop", "mode", flag_value="loop", help="Poll until terminal state.")
@click.option("--interval", default=5.0, type=float, show_default=True,
              help="Poll interval in seconds (--loop only).")
def monitor_execution_command(
    job_id: str,
    executions_dir: Optional[Path],
    mode: str,
    interval: float,
) -> None:
    """Check or poll job state. Prints current state JSON."""
    from .execution_port.registry import _EXECUTIONS_DIR
    from .execution_port.monitor import check_once, monitor_loop
    base = executions_dir or _EXECUTIONS_DIR
    job_dir = base / job_id
    if not job_dir.exists():
        click.echo(f"[error] job dir not found: {job_dir}", err=True)
        sys.exit(1)
    if mode == "loop":
        state = monitor_loop(job_dir, interval=interval)
    else:
        state = check_once(job_dir)
    _echo(json.dumps(state, indent=2, ensure_ascii=False))


@cli.command("collect-execution")
@click.option("--job-id", required=True, type=str, help="Job ID to collect.")
@click.option("--executions-dir", default=None, type=click.Path(path_type=Path),
              help="Directory that contains job dirs.")
@click.option("--review-packet", is_flag=True,
              help="Write a review_packet/ dir after collection (always for non-succeeded jobs).")
def collect_execution_command(job_id: str, executions_dir: Optional[Path],
                              review_packet: bool) -> None:
    """Evaluate job outcome and write final_result.yaml. Prints result JSON."""
    from .execution_port.registry import _EXECUTIONS_DIR
    from .execution_port.collector import collect
    from .execution_port.review_packet import build_review_packet
    base = executions_dir or _EXECUTIONS_DIR
    job_dir = base / job_id
    if not job_dir.exists():
        click.echo(f"[error] job dir not found: {job_dir}", err=True)
        sys.exit(1)
    try:
        result = collect(job_dir)
        _echo(json.dumps(result, indent=2, ensure_ascii=False))
        if review_packet or result.get("status") != "succeeded":
            packet_dir = build_review_packet(job_dir)
            _echo(f"[review_packet] {packet_dir}")
    except RuntimeError as e:
        click.echo(f"[error] {e}", err=True)
        sys.exit(1)


@cli.command("review-packet")
@click.option("--job-id", required=True, type=str, help="Job ID to package.")
@click.option("--executions-dir", default=None, type=click.Path(path_type=Path),
              help="Directory that contains job dirs.")
@click.option("--dest", default=None, type=click.Path(path_type=Path),
              help="Destination dir (default: job_dir/review_packet).")
def review_packet_command(job_id: str, executions_dir: Optional[Path],
                          dest: Optional[Path]) -> None:
    """Assemble a compact review packet from a finished job for human/Codex inspection."""
    from .execution_port.registry import _EXECUTIONS_DIR
    from .execution_port.review_packet import build_review_packet
    base = executions_dir or _EXECUTIONS_DIR
    job_dir = base / job_id
    if not job_dir.exists():
        click.echo(f"[error] job dir not found: {job_dir}", err=True)
        sys.exit(1)
    packet_dir = build_review_packet(job_dir, dest=dest)
    _echo(str(packet_dir))


if __name__ == "__main__":
    cli()
