from __future__ import annotations

from solution_agent.continuation.remote_boundary_contract import (
    REMOTE_BOUNDARY_DISABLED_SIDE_EFFECTS,
    assess_disabled_side_effects,
    collect_required_text_fields,
)


def test_remote_boundary_contract_accepts_disabled_side_effects() -> None:
    reasons, check = assess_disabled_side_effects(
        {"side_effects": dict(REMOTE_BOUNDARY_DISABLED_SIDE_EFFECTS)},
        reason_code="side_effects_enabled",
    )

    assert reasons == []
    assert check["status"] == "passed"
    assert check["mismatches"] == []


def test_remote_boundary_contract_rejects_enabled_side_effect() -> None:
    side_effects = dict(REMOTE_BOUNDARY_DISABLED_SIDE_EFFECTS)
    side_effects["ssh"] = True

    reasons, check = assess_disabled_side_effects(
        {"side_effects": side_effects},
        reason_code="side_effects_enabled",
    )

    assert reasons == ["side_effects_enabled"]
    assert check["status"] == "failed"
    assert check["mismatches"] == ["ssh"]


def test_remote_boundary_contract_collects_required_text_fields() -> None:
    values, missing = collect_required_text_fields(
        {"host": " ncku ", "remote_root": "", "remote_workdir": None},
        ("host", "remote_root", "remote_workdir"),
    )

    assert values == {"host": "ncku", "remote_root": "", "remote_workdir": ""}
    assert missing == ["remote_root", "remote_workdir"]
