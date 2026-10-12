from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .audit import record_draft_write_audit_stub
from .paths import continuation_proposed_root
from .schema import (
    ContinuationPlan,
    DraftRenderInput,
    GateReport,
    ProposedBundle,
    ProposedNextStep,
    ProposedTaskDraft,
    ProposalManifest,
)


_ALLOWED_DRAFT_ACTIONS = {
    "bounded_probe": "bound confidence and collect additional local artifacts",
    "decision_synthesis": "synthesize a bounded continuation decision before execution",
    "missing_capability_request": "request missing capability before continuation",
    "codex_debug_request": "request Codex-assisted debugging for source-level work",
    "inventory_audit": "perform a constrained local inventory audit before re-planning",
    "metric_completion": "collect additional metric-completion evidence locally",
}


def _safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(value))


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _bundle_id(plan: ContinuationPlan) -> str:
    return f"{plan.source_task_id}__{plan.plan_id}"


def _source_result_hash_from_plan(plan: ContinuationPlan) -> str:
    parts = plan.idempotency_key.split(":")
    if len(parts) > 1:
        return parts[1]
    return ""


def _deterministic_bundle_timestamp() -> str:
    return "1970-01-01T00:00:00+00:00"


def _bundle_root(plan: ContinuationPlan) -> Path:
    return continuation_proposed_root(plan.source_task_id, plan.plan_id)


def _build_draft_inputs(plan: ContinuationPlan) -> List[DraftRenderInput]:
    source_result_hash = _source_result_hash_from_plan(plan)
    draft_inputs: List[DraftRenderInput] = []
    for step in plan.proposed_next_steps:
        draft_kind = getattr(step.kind, "value", str(step.kind))
        goal = _ALLOWED_DRAFT_ACTIONS.get(
            draft_kind,
            f"Handle continuation result for {plan.source_task_id}",
        )
        draft_inputs.append(
            DraftRenderInput(
                draft_id=_safe_id(step.step_id),
                source_task_id=plan.source_task_id,
                source_result_hash=source_result_hash,
                reason_for_continuation=step.reason,
                goal=goal,
                scope="local_staging_only",
                allowed_actions=["local_artifact_generation", "manual_review_only"],
                forbidden_actions=step.forbidden_actions,
                expected_outputs=step.expected_outputs,
                claim_limitation=step.claim_limitation
                or "Intent-only draft: staging-local artifact generation is allowed only.",
                idempotency_key=f"{plan.source_task_id}:{source_result_hash}:0.1.0:{step.step_id}:draft",
            )
        )
    return draft_inputs


def _render_draft_payload(draft_input: DraftRenderInput) -> ProposedTaskDraft:
    return ProposedTaskDraft(
        draft_id=draft_input.draft_id,
        source_task_id=draft_input.source_task_id,
        source_result_hash=draft_input.source_result_hash,
        reason_for_continuation=draft_input.reason_for_continuation,
        goal=draft_input.goal,
        scope=draft_input.scope,
        allowed_actions=draft_input.allowed_actions,
        forbidden_actions=draft_input.forbidden_actions,
        expected_outputs=draft_input.expected_outputs,
        claim_limitation=draft_input.claim_limitation,
        idempotency_key=draft_input.idempotency_key,
    )


def _draft_json_payload(draft: ProposedTaskDraft) -> str:
    return _dump(draft.model_dump(mode="json"))


def _proposal_markdown(
    draft: ProposedTaskDraft,
    plan_id: str,
    owner: str,
    draft_count: int,
    all_draft_ids: List[str],
) -> str:
    front_matter = {
        "task_id": draft.draft_id,
        "title": f"Continuation proposal draft: {draft.draft_id}",
        "status": "pending",
        "priority": "normal",
        "worker": "codex" if owner == "codex" else "continuation_planner",
        "created_at": _deterministic_bundle_timestamp(),
        "source_task_id": draft.source_task_id,
        "source_result_hash": draft.source_result_hash,
        "plan_id": plan_id,
        "proposal_id": f"{plan_id}__proposals",
        "bundle_id": _bundle_id(ContinuationPlan(
            schema_version=draft.schema_version,
            plan_id=plan_id,
            source_task_id=draft.source_task_id,
            interpretation_hash="",
            idempotency_key=draft.idempotency_key,
            proposed_next_steps=[],
        )),
        "proposal_count": draft_count,
        "outputs": draft.expected_outputs,
        "forbidden_actions": [str(item) for item in draft.forbidden_actions],
        "claim_limitation": draft.claim_limitation,
        "drafts_in_bundle": all_draft_ids,
    }
    body = [
        "# Continuation Proposal Draft",
        "",
        f"Reason: {draft.reason_for_continuation}",
        f"Goal: {draft.goal}",
        f"Scope: {draft.scope}",
        "Allowed actions:",
        *(f"- {item}" for item in draft.allowed_actions),
        "",
        "Expected outputs:",
        *(f"- {item}" for item in draft.expected_outputs),
        "",
        "Required draft fields (for metabolizer compatibility):",
        "- worker",
        "- outputs",
        "- claim_limitation",
        "- forbidden_actions",
    ]
    return "---\n" + yaml.safe_dump(front_matter, sort_keys=False) + "---\n\n" + "\n".join(body) + "\n"


def _proposal_bundle_markdown(
    plan: ContinuationPlan,
    draft_payloads: List[ProposedTaskDraft],
    output_root: str,
) -> str:
    front_matter = {
        "task_id": f"bundle_{plan.source_task_id}_{plan.plan_id}",
        "title": "Continuation proposal bundle",
        "status": "pending",
        "priority": "normal",
        "worker": "continuation_planner",
        "created_at": _deterministic_bundle_timestamp(),
        "source_task_id": plan.source_task_id,
        "source_result_hash": _source_result_hash_from_plan(plan),
        "plan_id": plan.plan_id,
        "proposal_id": f"{plan.plan_id}__proposals",
        "bundle_id": _bundle_id(plan),
        "forbidden_actions": [
            "direct_dispatch",
            "scheduler_submit",
            "registry_mutation",
            "source_edit",
            "manual_file_promotion",
            "remote_path_guess",
        ],
        "outputs": ["proposal.json", "proposal_dry_run_manifest.json", "gate_report.json"],
        "claim_limitation": "Continuations are proposal-only; no active promotion or scheduler dispatch.",
        "drafts_in_bundle": [
            {
                "task_id": payload.draft_id,
                "json_path": f"{payload.draft_id}.json",
                "markdown_path": f"{payload.draft_id}.md",
            }
            for payload in draft_payloads
        ],
    }

    if not draft_payloads:
        body = [
            "---",
            yaml.safe_dump(front_matter, sort_keys=False).strip(),
            "---",
            "",
            "# Continuation Proposal Bundle",
            "",
            "No draft proposals were generated for this deterministic result.",
            "",
            f"output_root: {output_root}",
            "",
            "## Manifest Summary",
            f"plan_id: {plan.plan_id}",
            f"source_task_id: {plan.source_task_id}",
            f"source_result_hash: {_source_result_hash_from_plan(plan)}",
            "proposal_count: 0",
        ]
        return "\n".join(body) + "\n"

    body = [
        "---",
        yaml.safe_dump(front_matter, sort_keys=False).strip(),
        "---",
        "",
        "# Continuation Proposal Bundle",
        "",
        f"plan_id: {plan.plan_id}",
        f"source_task_id: {plan.source_task_id}",
        f"source_result_hash: {_source_result_hash_from_plan(plan)}",
        f"proposal_count: {len(draft_payloads)}",
        f"output_root: {output_root}",
        "",
        "## Drafts",
    ]
    for payload in draft_payloads:
        body.extend(
            [
                f"- {payload.draft_id}",
                f"  - reason: {payload.reason_for_continuation}",
                f"  - scope: {payload.scope}",
                f"  - claim_limitation: {payload.claim_limitation}",
            ]
        )
    return "\n".join(body) + "\n"


def _proposal_json_payload(
    plan: ContinuationPlan,
    draft_payloads: List[ProposedTaskDraft],
    output_root: str,
) -> Dict[str, Any]:
    return {
        "bundle_id": _bundle_id(plan),
        "proposal_id": f"{plan.plan_id}__proposals",
        "source_task_id": plan.source_task_id,
        "source_result_hash": _source_result_hash_from_plan(plan),
        "plan_id": plan.plan_id,
        "idempotency_key": plan.idempotency_key,
        "output_root": output_root,
        "proposal_count": len(draft_payloads),
        "proposal_paths": [f"{payload.draft_id}.json" for payload in draft_payloads],
        "proposals": [payload.model_dump(mode="json") for payload in draft_payloads],
        "proposal_timestamp": _deterministic_bundle_timestamp(),
        "no_safe_continuation_reason": plan.no_safe_continuation_reason,
    }


def _build_owner_map(
    plan: ContinuationPlan,
    draft_inputs: List[DraftRenderInput],
) -> Dict[str, str]:
    owner_by_draft: Dict[str, str] = {}
    for step, draft_input in zip(plan.proposed_next_steps, draft_inputs, strict=False):
        owner_by_draft[draft_input.draft_id] = step.owner
    return owner_by_draft


def _write_text_if_changed(path: Path, payload: str) -> None:
    if not path.exists() or path.read_text(encoding="utf-8") != payload:
        path.write_text(payload, encoding="utf-8")


def _write_draft_file(
    draft: ProposedTaskDraft,
    path: Path,
    payload_content: str,
    markdown_content: str,
) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path = path.with_suffix(".md")

    if not path.exists():
        path.write_text(payload_content, encoding="utf-8")
        with markdown_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown_content)
        return {
            "draft_id": draft.draft_id,
            "path": str(path),
            "status": "written",
            "collision_rule": "skip_if_identical_hash_diff_if_changed",
            "outcome": "written",
            "reason": "new draft payload",
        }

    try:
        existing = ProposedTaskDraft(**json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return {
            "draft_id": draft.draft_id,
            "path": str(path),
            "status": "collision",
            "collision_rule": "skip_if_identical_hash_diff_if_changed",
            "outcome": "collision",
            "reason": "existing draft payload is not valid for replay-safe read",
        }

    if not markdown_path.exists():
        return {
            "draft_id": draft.draft_id,
            "path": str(path),
            "status": "collision",
            "collision_rule": "skip_if_identical_hash_diff_if_changed",
            "outcome": "collision",
            "reason": "existing draft bundle missing companion markdown",
        }

    existing_payload = _dump(existing.model_dump(mode="json"))
    existing_hash = hashlib.sha256(existing_payload.encode("utf-8")).hexdigest()
    new_hash = hashlib.sha256(payload_content.encode("utf-8")).hexdigest()
    markdown_hash_existing = hashlib.sha256(markdown_path.read_bytes()).hexdigest()
    markdown_hash_new = hashlib.sha256(markdown_content.encode("utf-8")).hexdigest()

    if (
        draft.idempotency_key == existing.idempotency_key
        and existing_hash == new_hash
        and markdown_hash_existing == markdown_hash_new
    ):
        return {
            "draft_id": draft.draft_id,
            "path": str(path),
            "status": "skipped",
            "collision_rule": "skip_if_identical_hash_diff_if_changed",
            "outcome": "skip",
            "reason": "same idempotency key and identical content",
        }

    return {
        "draft_id": draft.draft_id,
        "path": str(path),
        "status": "collision",
        "collision_rule": "skip_if_identical_hash_diff_if_changed",
        "outcome": "collision",
        "reason": "same draft id with different content",
    }


def _expected_bundle_artifacts(
    plan: ContinuationPlan,
    draft_payloads: List[ProposedTaskDraft],
    draft_markdowns: Dict[str, str],
    owner_by_draft: Dict[str, str],
    draft_results: Optional[List[Dict[str, Any]]] = None,
    has_existing_snapshot_collision: bool = False,
) -> Tuple[Dict[str, str], str, Dict[str, Any], int]:
    batch_dir = _bundle_root(plan)
    draft_count = len(draft_payloads)
    output_root = str(batch_dir)
    proposal_payloads = list(draft_payloads)

    expected_files: Dict[str, str] = {}
    for draft_payload in draft_payloads:
        expected_files[str(batch_dir / f"{draft_payload.draft_id}.json")] = _dump(draft_payload.model_dump(mode="json"))
        expected_files[str(batch_dir / f"{draft_payload.draft_id}.md")] = draft_markdowns[draft_payload.draft_id]

    proposal_payload = _proposal_json_payload(plan, proposal_payloads, output_root)
    expected_files[str(batch_dir / "proposal.json")] = _dump(proposal_payload)
    expected_files[str(batch_dir / "proposal.md")] = _proposal_bundle_markdown(
        plan,
        proposal_payloads,
        output_root,
    )

    manifest_payload = ProposalManifest(
        manifest_id=f"{plan.plan_id}__proposal_manifest",
        bundle_id=_bundle_id(plan),
        source_task_id=plan.source_task_id,
        source_result_hash=_source_result_hash_from_plan(plan),
        plan_id=plan.plan_id,
        proposal_path=str(batch_dir / "proposal.md"),
        draft_count=draft_count,
        proposed_paths=[
            str(Path(draft.draft_id).with_name(f"{draft.draft_id}.json"))
            for draft in proposal_payloads
        ]
        + [str(Path(draft.draft_id).with_name(f"{draft.draft_id}.md")) for draft in proposal_payloads]
        + [
            "proposal.json",
            "proposal.md",
            "manifest.json",
            "proposal_dry_run_manifest.json",
            "gate_report.json",
        ],
        writes_forbidden=[
            "tasks/inbox/",
            "tasks/inbox/*/",
            "registry.json",
            ".solution/task_registry.yaml",
            "solution_agent/**",
            "remote:**",
        ],
        writes_allowed=[
            f"tasks/inbox/_proposed/{_safe_id(plan.source_task_id)}/{_safe_id(plan.plan_id)}/",
            f"tasks/inbox/_proposed/{_safe_id(plan.source_task_id)}/{_safe_id(plan.plan_id)}/*.json",
            f"tasks/inbox/_proposed/{_safe_id(plan.source_task_id)}/{_safe_id(plan.plan_id)}/*.md",
        ],
        checks=[
            {"name": "required_bundle_files", "status": "passed"},
            {"name": "proposal_files", "status": "passed"},
        ],
        notes=[
            "P0c proposal bundle is local-only and non-promotional.",
            f"authoritative_owner_count={len(owner_by_draft)}",
            f"dry_run_root={PurePath('tasks') / 'inbox' / '_proposed'}",
        ],
        idempotency_key=plan.idempotency_key,
        dry_run=True,
    )
    manifest_payload_json = _dump(manifest_payload.model_dump(mode="json"))
    expected_files[str(batch_dir / "manifest.json")] = manifest_payload_json
    expected_files[str(batch_dir / "proposal_dry_run_manifest.json")] = manifest_payload_json

    draft_results = draft_results or []
    has_collision = has_existing_snapshot_collision or any(
        result["outcome"] == "collision" for result in draft_results
    )
    check_results = [
        {"name": "required_bundle_files", "status": "passed"},
        {"name": "proposal_files", "status": "passed"},
        {"name": "idempotency", "status": "failed" if has_collision else "passed"},
    ]
    violations: List[Dict[str, str]] = []
    status = "manual_review"
    if has_collision:
        if has_existing_snapshot_collision:
            violations.append(
                {
                    "rule_id": "incomplete_bundle_snapshot",
                    "matched": "partial_snapshot_detected",
                    "severity": "error",
                }
            )
        if any(result["outcome"] == "collision" for result in draft_results):
            violations.append(
                {
                    "rule_id": "idempotency_collision",
                    "matched": "proposal_bundle_draft_collision",
                    "severity": "error",
                }
            )
        status = "failed"

    report_payload = GateReport(
        report_id=f"{plan.plan_id}__proposal_gate",
        bundle_id=_bundle_id(plan),
        source_task_id=plan.source_task_id,
        source_result_hash=_source_result_hash_from_plan(plan),
        idempotency_key=plan.idempotency_key,
        gate_status=status,
        checks=check_results,
        violations=violations,
        prompt_versions={"continuation_planner": "0.1.0", "writer": "0.1.0"},
        policy_version="0.1.0",
    )
    gate_report_payload = report_payload.model_dump(mode="json")
    manifest_payload = _dump(manifest_payload.model_dump(mode="json"))
    expected_files[str(batch_dir / "gate_report.json")] = _dump(gate_report_payload)
    return (
        expected_files,
        manifest_payload,
        gate_report_payload,
        draft_count,
    )


def _snapshot_matches_existing(expected_files: Dict[str, str]) -> bool:
    for file_path, expected in expected_files.items():
        path = Path(file_path)
        if not path.exists():
            return False
        if path.read_text(encoding="utf-8") != expected:
            return False
    return True


def _has_partial_snapshot(expected_files: Dict[str, str]) -> bool:
    top_level_files = {
        "proposal.json",
        "proposal.md",
        "manifest.json",
        "proposal_dry_run_manifest.json",
        "gate_report.json",
    }
    exists = [Path(path).exists() for path in expected_files if Path(path).name in top_level_files]
    return any(exists) and not all(exists)


def stage_plan_stub(plan: ContinuationPlan) -> Dict[str, Any]:
    """Write deterministic continuation proposal bundles under tasks/inbox/_proposed."""

    batch_dir = _bundle_root(plan)
    batch_dir.mkdir(parents=True, exist_ok=True)
    draft_inputs = _build_draft_inputs(plan)
    owner_by_draft = _build_owner_map(plan, draft_inputs)

    draft_payloads: List[ProposedTaskDraft] = []
    draft_markdowns: Dict[str, str] = {}
    draft_ids = [item.draft_id for item in draft_inputs]

    for draft_input in draft_inputs:
        proposed_draft = _render_draft_payload(draft_input)
        draft_payloads.append(proposed_draft)
        draft_markdowns[proposed_draft.draft_id] = _proposal_markdown(
            proposed_draft,
            plan.plan_id,
            owner_by_draft.get(proposed_draft.draft_id, "continuation_planner"),
            len(draft_payloads),
            draft_ids,
        )

    expected_files, manifest_payload, gate_payload, draft_count = _expected_bundle_artifacts(
        plan,
        draft_payloads,
        draft_markdowns,
        owner_by_draft,
    )

    has_existing_snapshot_collision = _has_partial_snapshot(expected_files)
    draft_results: List[Dict[str, Any]] = []
    if has_existing_snapshot_collision:
        draft_results = [
            {
                "draft_id": payload.draft_id,
                "path": str(batch_dir / f"{payload.draft_id}.json"),
                "status": "collision",
                "collision_rule": "skip_if_identical_hash_diff_if_changed",
                "outcome": "collision",
                "reason": "existing partial snapshot detected; refusing overwrite for safety",
            }
            for payload in draft_payloads
        ]
    else:
        for payload in draft_payloads:
            draft_path = batch_dir / f"{payload.draft_id}.json"
            result = _write_draft_file(
                draft=payload,
                path=draft_path,
                payload_content=_dump(payload.model_dump(mode="json")),
                markdown_content=draft_markdowns[payload.draft_id],
            )
            draft_results.append(result)
            if result["outcome"] == "collision":
                break

    expected_files, manifest_payload, gate_payload, draft_count = _expected_bundle_artifacts(
        plan,
        draft_payloads,
        draft_markdowns,
        owner_by_draft,
        draft_results=draft_results,
        has_existing_snapshot_collision=has_existing_snapshot_collision,
    )
    has_collision = any(result["outcome"] == "collision" for result in draft_results)
    had_existing_snapshot = all(Path(path).exists() for path in expected_files.keys())

    if not has_collision and not has_existing_snapshot_collision:
        for target, payload in expected_files.items():
            _write_text_if_changed(Path(target), payload)

    # keep this on plan for collision-aware manifest decisions while avoiding
    # function arguments from the caller.
    plan._last_draft_results = draft_results
    reused = (
        not has_collision
        and not has_existing_snapshot_collision
        and all(result["outcome"] == "skip" for result in draft_results)
        and had_existing_snapshot
    )

    output_root = str(batch_dir)
    bundle = ProposedBundle(
        bundle_id=_bundle_id(plan),
        proposal_id=f"{plan.plan_id}__proposals",
        source_task_id=plan.source_task_id,
        source_result_hash=_source_result_hash_from_plan(plan),
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        output_root=output_root,
        proposal_json_path=str(batch_dir / "proposal.json"),
        proposal_markdown_path=str(batch_dir / "proposal.md"),
        manifest_path=str(batch_dir / "manifest.json"),
        gate_report_path=str(batch_dir / "gate_report.json"),
        draft_count=draft_count,
        proposal_count=draft_count,
        created_at=_deterministic_bundle_timestamp(),
        prompt_versions={"continuation_planner": "0.1.0", "writer": "0.1.0"},
    )
    audit = record_draft_write_audit_stub(plan, draft_results, target=str(batch_dir))

    manifest = json.loads(manifest_payload)
    return {
        "staged": not reused,
        "dry_run": True,
        "path": output_root,
        "proposed_task_count": len(draft_payloads),
        "draft_count": len(draft_payloads),
        "drafts": draft_results,
        "audit": audit,
        "manifest_path": str(batch_dir / "manifest.json"),
        "manifest": manifest,
        "manifest_written": not reused,
        "proposal_json_path": str(batch_dir / "proposal.json"),
        "proposal_markdown_path": str(batch_dir / "proposal.md"),
        "proposal_dry_run_manifest_path": str(batch_dir / "proposal_dry_run_manifest.json"),
        "gate_report_path": str(batch_dir / "gate_report.json"),
        "gate_report": gate_payload,
        "bundle": bundle.model_dump(mode="json"),
        "proposal_bundle_reused": reused,
    }
