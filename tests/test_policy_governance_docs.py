from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from solution_agent.cli import cli


def _markdown_table_rows(text: str):
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


def test_policy_governance_matrix_documents_core_layers():
    path = Path("docs/AGENT_POLICY_GOVERNANCE_MATRIX_2026-05-13.md")
    text = path.read_text(encoding="utf-8")

    for layer in (
        "hard_contract",
        "scheduler_admission",
        "worker_admission",
        "execution_safety",
        "semantic_contract",
        "advisory_contract",
    ):
        assert layer in text

    for module in (
        "Contract / Result Governance",
        "Scheduler Governance",
        "Inbox Governance",
        "Execution / Collection Governance",
        "OpenCoder / Patch Governance",
        "Action Request Governance",
    ):
        assert module in text

    assert "183 passed" in text


def test_policy_governance_matrix_lints_blocking_rule_coverage():
    path = Path("docs/AGENT_POLICY_GOVERNANCE_MATRIX_2026-05-13.md")
    text = path.read_text(encoding="utf-8")
    rows = list(_markdown_table_rows(text))
    blocking_rows = [
        row
        for row in rows
        if row.get("Blocks", "").strip().lower() == "yes"
        or row.get("Blocks", "").strip().lower().startswith("yes ")
        or row.get("Blocks", "").strip().lower().startswith("yes for")
        or row.get("Blocks", "").strip().lower().startswith("yes unless")
    ]

    assert blocking_rows, "policy matrix should contain blocking rule rows"
    for row in blocking_rows:
        label = row.get("Rule ID") or row.get("Rule ID / Reason") or row.get("Rule") or str(row)
        assert row.get("Layer", "").strip(), f"{label} is missing a layer"
        assert row.get("Owner", "").strip(), f"{label} is missing an owner"
        assert row.get("Tests", "").strip(), f"{label} is missing test coverage notes"


def test_policy_governance_lint_cli_checks_current_matrix():
    result = CliRunner().invoke(cli, ["policy-governance-lint"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["rules_checked"] > 0


def test_policy_governance_lint_cli_checks_opencoder_matrix():
    result = CliRunner().invoke(
        cli,
        ["policy-governance-lint", "--path", "docs/OPENCODER_POLICY_GOVERNANCE_MATRIX_2026-05-13.md"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["rules_checked"] > 0


def test_policy_governance_lint_cli_fails_missing_blocking_owner(tmp_path: Path):
    matrix = tmp_path / "policy.md"
    matrix.write_text(
        "\n".join(
            [
                "| Rule | Layer | Severity | Owner | Blocks | Scope | Tests |",
                "|---|---|---|---|---:|---|---|",
                "| `bad_rule` | `hard_contract` | error |  | yes | Demo | replay |",
            ]
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["policy-governance-lint", "--path", str(matrix)])

    assert result.exit_code != 0
    payload = json.loads(result.output.split("Error:")[0])
    assert payload["ok"] is False
    assert payload["errors"][0]["field"] == "Owner"


def test_opencoder_governance_matrix_documents_authority_boundaries():
    path = Path("docs/OPENCODER_POLICY_GOVERNANCE_MATRIX_2026-05-13.md")
    text = path.read_text(encoding="utf-8")

    for layer in (
        "task_admission",
        "patch_intent_schema",
        "anchor_validation",
        "diff_validation",
        "advisory_contract",
        "codex_handoff",
    ):
        assert layer in text

    for phrase in (
        "OpenCoder may not",
        "codex_handoff_action_request",
        "P0",
        "P1",
        "P2",
        "P3",
        "Codex only",
        "source_or_multifile_patch_requires_codex",
        "codex_handoff_required",
        "long_running_experiment_command",
        "primary_rule_id",
        "57 passed",
        "policy-governance-lint",
    ):
        assert phrase in text


def test_workflow_profile_governance_documents_score_and_sync():
    path = Path("docs/WORKFLOW_PROFILE_GOVERNANCE_2026-05-14.md")
    text = path.read_text(encoding="utf-8")

    for phrase in (
        "Memory / workflow profile",
        "8.2",
        "state/workflow_profile.md",
        "remote context paths",
        "scheduler, OpenCoder, and inbox policy guides",
        "remote_context_required_files_missing",
        "one bounded low-score fix",
        "tests\\test_workflow_profile.py",
    ):
        assert phrase in text
