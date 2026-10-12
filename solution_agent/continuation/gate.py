from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

from pydantic import ValidationError

from .schema import ForbiddenAction, ProposalManifest, PromotionReport, ProposedTaskDraft, StagingDryRunManifest


_FORBIDDEN_PATTERNS = [
    (
        "direct_dispatch",
        re.compile(r"\b(dispatch|run-next|run-queue|schedule_remote|scheduler submit)\b", re.IGNORECASE),
    ),
    (
        "remote_path_guess",
        re.compile(r"(/ssd\d+|/raid/|/mnt/|\\\\[^\\]+\\[^\\]+|[A-Z]:\\)", re.IGNORECASE),
    ),
    (
        "source_edit",
        re.compile(r"\b(edit source|modify source|patch source|change code|apply patch)\b", re.IGNORECASE),
    ),
    (
        "registry_mutation",
        re.compile(r"\b(registry\.json|upsert_task|mutate registry|registry mutation)\b", re.IGNORECASE),
    ),
    (
        "cross_line_continuation",
        re.compile(r"\b(cross-line|line b|line_b|other line|different line)\b", re.IGNORECASE),
    ),
    (
        "auto_promotion",
        re.compile(r"\b(auto[- ]?promote|promote automatically|move .* to inbox)\b", re.IGNORECASE),
    ),
]

_NEGATION_CONTEXT = re.compile(r"\b(no|not|do not|don't|never|forbid|forbidden)\b", re.IGNORECASE)

_REQUIRED_DRAFT_FIELDS = {
    "source_task_id",
    "source_result_hash",
    "reason_for_continuation",
    "goal",
    "scope",
    "allowed_actions",
    "forbidden_actions",
    "expected_outputs",
    "claim_limitation",
}


def _strip_front_matter(text: str) -> str:
    """Strip YAML front matter before static phrase scanning.

    This keeps declarative metadata (for example forbidden_actions lists) from
    being interpreted as prose commands.
    """

    if not text.startswith("---\n"):
        return text
    parts = text.split("---", 2)
    if len(parts) == 3:
        return parts[2]
    return text


def _is_negated_context(text: str, start: int) -> bool:
    context = text[max(0, start - 40) : start]
    return bool(_NEGATION_CONTEXT.search(context))


def scan_static_draft_text(text: str) -> Dict[str, List[Dict[str, str]]]:
    """Return static gate checks for forbidden wording in draft text."""

    text_to_scan = _strip_front_matter(text)
    violations: List[Dict[str, str]] = []
    for rule_id, pattern in _FORBIDDEN_PATTERNS:
        match = pattern.search(text_to_scan)
        if match:
            if _is_negated_context(text_to_scan, match.start()):
                continue
            violations.append(
                {
                    "rule_id": rule_id,
                    "matched": match.group(0),
                    "severity": "error",
                }
            )
    checks = [
        {
            "name": "forbidden_static_patterns",
            "status": "failed" if violations else "passed",
        }
    ]
    return {"checks": checks, "violations": violations}


def review_plan_stub(plan_id: str, source_task_id: str, source_result_hash: str) -> PromotionReport:
    """P0 skeleton gate: manual review report with no promotion side effects."""

    return PromotionReport(
        report_id=f"{plan_id}__gate",
        proposed_task_id=f"{plan_id}__no_task",
        source_task_id=source_task_id,
        source_result_hash=source_result_hash,
        idempotency_key=f"{plan_id}:gate",
        gate_status="manual_review",
        checks=[{"name": "p0_skeleton", "status": "manual_review"}],
        violations=[],
        prompt_versions={},
        policy_version="0.1.0",
    )


def review_draft_text_stub(
    *,
    proposed_task_id: str,
    source_task_id: str,
    source_result_hash: str,
    draft_text: str,
) -> PromotionReport:
    """P0 static gate: inspect draft text and never promote it."""

    scan = scan_static_draft_text(draft_text)
    gate_status = "failed" if scan["violations"] else "manual_review"
    return PromotionReport(
        report_id=f"{proposed_task_id}__static_gate",
        proposed_task_id=proposed_task_id,
        source_task_id=source_task_id,
        source_result_hash=source_result_hash,
        idempotency_key=f"{proposed_task_id}:gate",
        gate_status=gate_status,
        checks=scan["checks"],
        violations=scan["violations"],
        prompt_versions={},
        policy_version="0.1.0",
    )


def review_staging_manifest_stub(manifest_path: Path) -> PromotionReport:
    """P0 static gate: validate a dry-run staging manifest and never promote it."""

    checks: List[Dict[str, str]] = []
    violations: List[Dict[str, str]] = []
    manifest_text = manifest_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(manifest_text)
    except (OSError, json.JSONDecodeError) as exc:
        checks.append({"name": "staging_manifest_schema", "status": "failed"})
        violations.append(
            {
                "rule_id": "invalid_staging_manifest",
                "matched": type(exc).__name__,
                "severity": "error",
            }
        )
        return PromotionReport(
            report_id=f"{manifest_path.stem}__manifest_gate",
            proposed_task_id=f"{manifest_path.stem}__no_task",
            source_task_id="unknown",
            source_result_hash="unknown",
            idempotency_key=f"{manifest_path}:gate",
            gate_status="failed",
            checks=checks,
            violations=violations,
            prompt_versions={},
            policy_version="0.1.0",
        )

    checks.append({"name": "staging_manifest_schema", "status": "passed"})
    try:
        manifest = ProposalManifest(**payload)
        report_payload_kind = "proposal_manifest"
        source_task_id = manifest.source_task_id
        source_result_hash = manifest.source_result_hash
    except ValidationError:
        try:
            manifest = StagingDryRunManifest(**payload)
            report_payload_kind = "staging_manifest"
            source_task_id = manifest.source_task_id
            source_result_hash = manifest.idempotency_key
        except ValidationError as exc:
            checks.append({"name": "staging_manifest_schema", "status": "failed"})
            violations.append(
                {
                    "rule_id": "invalid_staging_manifest",
                    "matched": type(exc).__name__,
                    "severity": "error",
                }
            )
            return PromotionReport(
                report_id=f"{manifest_path.stem}__manifest_gate",
                proposed_task_id=f"{manifest_path.stem}__no_task",
                source_task_id="unknown",
                source_result_hash="unknown",
                idempotency_key=f"{manifest_path}:gate",
                gate_status="failed",
                checks=checks,
                violations=violations,
                prompt_versions={},
                policy_version="0.1.0",
            )

    required_fields = ("manifest_id", "source_task_id", "source_result_hash", "plan_id", "proposal_path")
    for field in required_fields:
        if getattr(manifest, field, None) in (None, ""):
            violations.append(
                {
                    "rule_id": "manifest_missing_required_field",
                    "matched": field,
                    "severity": "error",
                }
            )
    checks.append(
        {
            "name": "manifest_required_fields",
            "status": "failed" if violations else "passed",
        }
    )

    forbidden_text = "\n".join(manifest.writes_forbidden)
    required_forbidden = ("tasks/inbox/", "registry")
    for required in required_forbidden:
        if required not in forbidden_text:
            violations.append(
                {
                    "rule_id": "manifest_missing_forbidden_write",
                    "matched": required,
                    "severity": "error",
                }
            )

    allowed_roots = manifest.writes_allowed or []
    if not any("tasks/inbox/_proposed/" in str(item) for item in allowed_roots):
        violations.append(
            {
                "rule_id": "manifest_missing_allowed_proposed_root",
                "matched": "tasks/inbox/_proposed/",
                "severity": "error",
            }
        )

    checks.append(
        {
            "name": "manifest_forbidden_writes",
            "status": "failed" if any(
                item["rule_id"].startswith("manifest_missing") for item in violations
            ) else "passed",
        }
    )
    checks.append({"name": f"{report_payload_kind}_schema", "status": "passed"})

    if (
        isinstance(manifest, ProposalManifest)
        and str(manifest.proposal_path).startswith("tasks/inbox/")
        and "tasks/inbox/_proposed/" not in str(manifest.proposal_path)
    ):
        violations.append(
            {
                "rule_id": "manifest_disallowed_proposal_path",
                "matched": manifest.proposal_path,
                "severity": "error",
            }
        )
    if (
        isinstance(manifest, ProposalManifest)
        and any("direct_dispatch" in str(path) for path in manifest.writes_allowed)
    ):
        violations.append(
            {
                "rule_id": "manifest_dispatch_allowed",
                "matched": "write target indicates dispatch",
                "severity": "error",
            }
        )

    gate_status = "failed" if violations else "manual_review"
    checks.append(
        {
            "name": "manifest_dispatch_and_dispatch_language",
            "status": "passed" if gate_status == "manual_review" else "failed",
        }
    )
    return PromotionReport(
        report_id=f"{manifest.manifest_id}__manifest_gate",
        proposed_task_id=f"{manifest.manifest_id}__no_task",
        source_task_id=source_task_id,
        source_result_hash=source_result_hash,
        idempotency_key=f"{manifest.manifest_id}:gate",
        gate_status=gate_status,
        checks=checks,
        violations=violations,
        prompt_versions={},
        policy_version="0.1.0",
    )


def review_draft_payload_stub(*, draft_payload: Dict[str, object]) -> PromotionReport:
    """P0 static gate check for required draft fields.

    Drafts must include all required semantic fields before being accepted as
    staging-ready artifacts, even in dry-run form.
    """

    violations: List[Dict[str, str]] = []
    checks: List[Dict[str, str]] = []
    source_task_id = str(draft_payload.get("source_task_id", "unknown"))
    source_result_hash = str(draft_payload.get("source_result_hash", "unknown"))
    draft_id = str(draft_payload.get("draft_id", "no_draft_id"))

    missing_fields = [field for field in sorted(_REQUIRED_DRAFT_FIELDS) if field not in draft_payload]
    if missing_fields:
        for field in missing_fields:
            violations.append(
                {
                    "rule_id": "draft_required_fields",
                    "matched": field,
                    "severity": "error",
                }
            )
    else:
        try:
            ProposedTaskDraft(
                draft_id=draft_id,
                source_task_id=source_task_id,
                source_result_hash=source_result_hash,
                reason_for_continuation=str(draft_payload["reason_for_continuation"]),
                goal=str(draft_payload["goal"]),
                scope=str(draft_payload["scope"]),
                allowed_actions=list(draft_payload["allowed_actions"]),
                forbidden_actions=[ForbiddenAction(item) for item in draft_payload["forbidden_actions"]],
                expected_outputs=list(draft_payload["expected_outputs"]),
                claim_limitation=str(draft_payload["claim_limitation"]),
                idempotency_key=str(draft_payload.get("idempotency_key", f"{source_task_id}:{source_result_hash}:0.1.0:manual")),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            violations.append(
                {
                    "rule_id": "draft_required_fields",
                    "matched": type(exc).__name__,
                    "severity": "error",
                }
            )

    checks.append(
        {
            "name": "draft_required_fields",
            "status": "failed" if violations else "passed",
        }
    )
    gate_status = "failed" if violations else "manual_review"
    return PromotionReport(
        report_id=f"{draft_id}__draft_gate",
        proposed_task_id=draft_id,
        source_task_id=source_task_id,
        source_result_hash=source_result_hash,
        idempotency_key=f"{draft_id}:gate",
        gate_status=gate_status,
        checks=checks,
        violations=violations,
        prompt_versions={},
        policy_version="0.1.0",
    )
