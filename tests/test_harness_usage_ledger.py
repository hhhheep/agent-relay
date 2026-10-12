from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from solution_agent.harness.budget import evaluate_structural_budget
from solution_agent.harness.contracts import ProviderUsage
from solution_agent.harness.telemetry_contracts import (
    PhaseBudget,
    PortfolioBudget,
    QuotaWindowObservation,
    StructuralUsage,
    UsageEvent,
)
from solution_agent.harness.usage_ledger import (
    UsageLedger,
    UsageLedgerError,
    provider_usage_event,
    token_guard_receipt_event,
)
from solution_agent.harness.contracts import VerifiedReceipt, VerifiedStream


def test_usage_ledger_is_idempotent_and_keeps_missing_usage_explicit(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "run", run_id="run-1")
    measured = provider_usage_event(
        run_id="run-1",
        side="external_writer",
        phase="report",
        usage=ProviderUsage(
            provider="bailian",
            model="deepseek-v4.1-flash",
            input_tokens=800,
            cache_read_tokens=500,
            output_tokens=200,
            total_tokens=1000,
            cost_usd=0.002,
        ),
        requested_model="deepseek-v4.1-flash",
    )
    unavailable = UsageEvent(
        usage_event_id="unavailable-1",
        run_id="run-1",
        side="local_controller",
        phase="review",
        operation="model_request",
        provider="codex",
        requested_model="gpt-6-sol",
        reasoning_class="scientific_review",
        usage_source="unavailable",
        model_turn_delta=1,
    )
    deterministic = UsageEvent(
        usage_event_id="tool-1",
        run_id="run-1",
        side="remote_worker",
        phase="execute",
        operation="tool_call",
        usage_source="deterministic_counter",
        tool_call_delta=1,
        result_payload_bytes=240,
        raw_evidence_bytes=4096,
    )

    assert ledger.append(measured) is True
    assert ledger.append(measured) is False
    assert ledger.merge([unavailable, deterministic, deterministic]) == {
        "appended": 2,
        "duplicate": 1,
    }

    summary = ledger.summarize()
    assert summary["event_count"] == 3
    assert summary["totals"]["provider_usage_events"] == 1
    assert summary["totals"]["unavailable_usage_events"] == 1
    assert summary["totals"]["total_tokens"] == 1000
    assert summary["totals"]["input_tokens"] == 800
    assert summary["totals"]["cache_read_tokens"] == 500
    assert summary["totals"]["model_turns"] == 2
    assert summary["totals"]["tool_calls"] == 1
    assert summary["totals"]["model_facing_result_bytes"] == 240
    assert summary["totals"]["raw_evidence_bytes"] == 4096
    assert summary["by_side"]["external_writer"]["total_tokens"] == 1000
    assert summary["by_side"]["local_controller"]["unavailable_usage_events"] == 1
    assert ledger.summary_path.is_file()
    assert len(summary["events_sha256"]) == 64


def test_usage_ledger_rejects_conflicting_duplicate_and_wrong_run(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "run", run_id="run-1")
    first = UsageEvent(
        usage_event_id="same-id",
        run_id="run-1",
        side="local_controller",
        phase="plan",
        operation="deterministic_step",
        usage_source="deterministic_counter",
    )
    ledger.append(first)
    conflicting = first.model_copy(update={"tool_call_delta": 1})
    with pytest.raises(UsageLedgerError, match="conflicting duplicate"):
        ledger.append(conflicting)

    wrong_run = first.model_copy(update={"usage_event_id": "other", "run_id": "run-2"})
    with pytest.raises(UsageLedgerError, match="does not match"):
        ledger.append(wrong_run)


def test_usage_ledger_fails_closed_on_corrupt_line(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "run", run_id="run-1")
    ledger.usage_dir.mkdir(parents=True)
    ledger.events_path.write_text("{not-json}\n", encoding="utf-8")
    with pytest.raises(UsageLedgerError, match="line 1"):
        ledger.read()


def test_usage_event_contract_rejects_secret_or_body_fields_and_bad_measurements() -> None:
    base = {
        "run_id": "run-1",
        "side": "external_writer",
        "phase": "report",
        "operation": "model_request",
        "provider": "bailian",
        "usage_source": "provider",
        "input_tokens": 10,
        "output_tokens": 2,
    }
    with pytest.raises(ValidationError):
        UsageEvent.model_validate({**base, "prompt": "must never enter telemetry"})
    with pytest.raises(ValidationError, match="cache_read_tokens"):
        UsageEvent.model_validate({**base, "cache_read_tokens": 11})
    with pytest.raises(ValidationError, match="total_tokens"):
        UsageEvent.model_validate({**base, "total_tokens": 11})
    with pytest.raises(ValidationError, match="requires input_tokens"):
        UsageEvent.model_validate({**base, "input_tokens": None})


def test_quota_window_is_observation_not_fake_provider_tokens() -> None:
    event = UsageEvent(
        run_id="run-1",
        side="local_controller",
        phase="collect",
        operation="quota_snapshot",
        provider="codex-account",
        usage_source="account_window",
        quota_window=QuotaWindowObservation(
            window_kind="weekly",
            used_percent=30,
            remaining_percent=70,
            resolution_percent=1,
        ),
    )
    assert event.input_tokens is None
    with pytest.raises(ValidationError, match="equal 100"):
        QuotaWindowObservation(window_kind="weekly", used_percent=30, remaining_percent=60)


def test_structural_budget_enforces_wait_zero_and_payload_caps() -> None:
    wait_budget = PhaseBudget(max_model_requests=0, max_tool_calls=0)
    wait = evaluate_structural_budget(
        StructuralUsage(),
        wait_budget,
        proposed_model_requests=1,
    )
    assert wait.allowed is False
    assert wait.violations == ["model_requests_exceeded:1>0"]

    budget = PhaseBudget(
        max_model_requests=2,
        max_tool_calls=3,
        max_corrective_model_requests=1,
        max_missing_context_rounds=1,
        max_context_estimated_tokens_per_request=1000,
        max_tool_schema_bytes=5000,
        max_tool_result_bytes_per_call=800,
        max_handoff_bytes=1200,
    )
    allowed = evaluate_structural_budget(
        StructuralUsage(model_requests=1, tool_calls=1),
        budget,
        proposed_model_requests=1,
        proposed_tool_calls=2,
        next_context_estimated_tokens=1000,
        tool_schema_bytes=5000,
        next_tool_result_bytes=800,
        next_handoff_bytes=1200,
    )
    assert allowed.allowed is True

    denied = evaluate_structural_budget(
        StructuralUsage(
            model_requests=2,
            tool_calls=3,
            corrective_model_requests=1,
            missing_context_rounds=1,
        ),
        budget,
        proposed_model_requests=1,
        proposed_tool_calls=1,
        proposed_corrective_model_requests=1,
        proposed_missing_context_rounds=1,
        next_context_estimated_tokens=1001,
        tool_schema_bytes=5001,
        next_tool_result_bytes=801,
        next_handoff_bytes=1201,
    )
    assert denied.allowed is False
    assert len(denied.violations) == 8


def test_written_ledger_contains_only_contract_fields(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "run", run_id="run-1")
    ledger.append(
        UsageEvent(
            run_id="run-1",
            side="remote_controller",
            phase="dispatch",
            operation="deterministic_step",
            usage_source="deterministic_counter",
            included_evidence_sha256=("a" * 64,),
        )
    )
    payload = json.loads(ledger.events_path.read_text(encoding="utf-8"))
    assert "prompt" not in payload
    assert "response" not in payload
    assert "api_key" not in payload


def test_token_guard_receipt_adapter_is_stable_and_counts_exact_raw_bytes(tmp_path: Path) -> None:
    receipt = VerifiedReceipt(
        receipt_path=".token-guard/runs/tg-1/receipt.json",
        run_id="tg-1",
        command_sha256="c" * 64,
        exit_code=0,
        timed_out=False,
        streams=[
            VerifiedStream(name="stdout", path="stdout.log", bytes=120, sha256="a" * 64, verified=True),
            VerifiedStream(name="stderr", path="stderr.log", bytes=8, sha256="b" * 64, verified=True),
        ],
        verified=True,
    )
    first = token_guard_receipt_event(
        run_id="run-1",
        side="remote_worker",
        phase="execute",
        receipt=receipt,
        result_payload_bytes=300,
    )
    second = token_guard_receipt_event(
        run_id="run-1",
        side="remote_worker",
        phase="execute",
        receipt=receipt,
        result_payload_bytes=300,
    )
    assert first.usage_event_id == second.usage_event_id
    assert first.raw_evidence_bytes == 128
    assert first.included_evidence_sha256 == ("a" * 64, "b" * 64)
    ledger = UsageLedger(tmp_path / "run", run_id="run-1")
    assert ledger.merge([first, second]) == {"appended": 1, "duplicate": 1}


def test_portfolio_budget_preserves_weekly_reserve_and_progress_target() -> None:
    budget = PortfolioBudget()
    assert budget.minimum_progress_units == 14
    assert budget.target_progress_units == 21
    assert budget.reserve_percent == 25
    assert budget.routine_release_ceiling_percent == 5
    assert budget.complex_release_ceiling_percent == 10

    with pytest.raises(ValidationError, match="minimum_progress_units"):
        PortfolioBudget(minimum_progress_units=21, target_progress_units=14)
    with pytest.raises(ValidationError, match="routine release ceiling"):
        PortfolioBudget(routine_target_percent=6, routine_release_ceiling_percent=5)
