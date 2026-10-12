from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from solution_agent.continuation import api_adapter
from solution_agent.continuation import api_council
from solution_agent.continuation.knowledge_pack import (
    build_knowledge_pack_manifest_dry_run,
    select_remote_safe_knowledge_subset,
)
from solution_agent.continuation import paths as continuation_paths
from solution_agent.continuation import ledger as continuation_ledger


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
    source_root = Path(".solution/continuation")
    if source_root.exists():
        for name in [
            "planner_policy.yaml",
            "permission_matrix.yaml",
            "knowledge_pack_policy.yaml",
            "knowledge_pack_schema.yaml",
            "api_council_policy.yaml",
            "api_council_schema.yaml",
            "run_ledger_schema.yaml",
            "api_adapter_policy.yaml",
            "api_adapter_schema.yaml",
            "api_provider_allowlist.yaml",
        ]:
            source = source_root / name
            if source.exists():
                target = tmp_path / ".solution" / "continuation" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


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


def _build_council_request() -> dict:
    subset = _build_subset()
    return api_council.build_api_council_request_dry_run(
        subset,
        actor="system",
        source_task_id="task_001",
        line="L1",
        workplace_profile="line-a",
    )


def _default_mock_response(council_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "council_id": council_id,
        "status": "valid",
        "interpretation_votes": [{"model": "mock", "vote": "bounded_probe"}],
        "risk_flags": [],
        "next_step_candidates": [{"kind": "bounded_probe", "scope": "local"}],
        "disagreement_summary": {"text": "mock response"},
    }


def _build_valid_output(council_request: dict) -> dict:
    invocation = api_adapter.build_api_adapter_invocation_dry_run(
        council_request,
        provider_id="mock",
        actor="system",
    )
    return api_adapter.invoke_mock_api_provider(
        invocation,
        mock_response=_default_mock_response(council_request["council_id"]),
    )


def _build_paths(base: Path) -> tuple[Path, Path, Path]:
    return (
        base / "continuation" / "api_adapter",
        base / "tasks" / "inbox",
        base / "continuation" / "run_ledger",
    )


def test_api_adapter_contract_artifacts_and_allowlist_contract(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    policy_path = Path(".solution/continuation/api_adapter_policy.yaml")
    schema_path = Path(".solution/continuation/api_adapter_schema.yaml")
    allowlist_path = Path(".solution/continuation/api_provider_allowlist.yaml")
    docs_path = Path("docs/API_COUNCIL_ADAPTER_P4F.md")

    assert policy_path.exists()
    assert schema_path.exists()
    assert allowlist_path.exists()
    assert docs_path.exists()

    allowlist = yaml.safe_load(allowlist_path.read_text(encoding="utf-8"))
    providers = allowlist.get("allowed_providers")
    assert isinstance(providers, dict)
    assert "mock" in providers
    assert len(providers) == 1
    assert providers["mock"].get("mode") == "mock"


def test_build_invocation_from_council_request_sets_network_false(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    request = _build_council_request()
    invocation = api_adapter.build_api_adapter_invocation_dry_run(
        request,
        provider_id="mock",
        actor="system",
    )

    assert invocation["status"] == "invocation_created"
    assert invocation["network_enabled"] is False
    assert invocation["api_call_executed"] is False
    assert invocation["provider_mode"] == "mock"
    assert invocation["provider_id"] == "mock"
    assert invocation["source_council_id"] == request["council_id"]


def test_unknown_provider_is_blocked(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    request = _build_council_request()
    invocation = api_adapter.build_api_adapter_invocation_dry_run(
        request,
        provider_id="unknown_provider",
        actor="system",
    )

    assert invocation["status"] == "blocked"
    assert any("not_allowlisted" in reason for reason in invocation["reasons"])


def test_non_mock_provider_mode_is_blocked(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    allowlist_path = tmp_path / ".solution" / "continuation" / "api_provider_allowlist.yaml"
    allowlist = yaml.safe_load(allowlist_path.read_text(encoding="utf-8"))
    allowlist["allowed_providers"]["mock"]["mode"] = "replay"
    allowlist_path.write_text(
        yaml.safe_dump(allowlist, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    request = _build_council_request()
    invocation = api_adapter.build_api_adapter_invocation_dry_run(
        request,
        provider_id="mock",
        actor="system",
    )

    assert invocation["status"] == "blocked"
    assert any("not_mock" in reason for reason in invocation["reasons"])


def test_mock_provider_response_validates_to_accepted(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    request = _build_council_request()
    invocation = api_adapter.build_api_adapter_invocation_dry_run(
        request, provider_id="mock", actor="system"
    )
    output = api_adapter.invoke_mock_api_provider(
        invocation,
        mock_response=_default_mock_response(request["council_id"]),
    )
    validated = api_adapter.validate_api_adapter_output(
        output,
        council_request=request,
    )

    assert validated["status"] == "valid"
    assert validated["provider_response_validation"]["status"] == "valid"
    assert not validated["reasons"]


def test_mock_provider_response_with_forbidden_action_rejected(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    request = _build_council_request()
    response = _default_mock_response(request["council_id"])
    response["dispatch"] = True

    invocation = api_adapter.build_api_adapter_invocation_dry_run(request, provider_id="mock", actor="system")
    output = api_adapter.invoke_mock_api_provider(invocation, mock_response=response)
    validated = api_adapter.validate_api_adapter_output(output, council_request=request)

    assert validated["status"] == "rejected"
    assert any("forbidden_field" in reason for reason in validated["reasons"])
    assert validated["provider_response_validation"]["status"] == "rejected"


def test_credentials_like_fields_are_rejected_and_not_written(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    request = _build_council_request()
    response = _default_mock_response(request["council_id"])
    response["api_key"] = "top-secret-key"

    invocation = api_adapter.build_api_adapter_invocation_dry_run(request, provider_id="mock", actor="system")
    output = api_adapter.invoke_mock_api_provider(invocation, mock_response=response)
    written = api_adapter.write_api_adapter_artifacts_dry_run(output)

    assert not any("api_key" in reason for reason in written.get("reasons", []))
    assert "top-secret-key" not in _json_text(tmp_path / "continuation")
    if written["created"]:
        assert Path(written["invocation_path"]).exists()
        assert "api_key" not in json.loads(Path(written["mock_response_path"]).read_text(encoding="utf-8"))
    else:
        assert not Path(written["invocation_path"]).exists()


def _json_text(root: Path) -> str:
    parts = []
    if not root.exists():
        return ""
    for path in root.rglob("*.json"):
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_artifacts_are_written_only_under_api_adapter_dir(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    request = _build_council_request()
    output = _build_valid_output(request)
    written = api_adapter.write_api_adapter_artifacts_dry_run(output)

    invocation_dir = Path(written["invocation_path"]).parent
    assert invocation_dir == tmp_path / "continuation" / "api_adapter" / output["invocation_id"]
    assert Path(written["invocation_path"]).exists()
    assert Path(written["mock_response_path"]).exists()
    assert Path(written["validated_response_path"]).exists()

    assert str(written["invocation_path"]).startswith(str(tmp_path / "continuation" / "api_adapter"))
    assert str(written["mock_response_path"]).startswith(str(tmp_path / "continuation" / "api_adapter"))
    assert str(written["validated_response_path"]).startswith(str(tmp_path / "continuation" / "api_adapter"))
    assert Path(written["mock_response_path"]).name == "mock_response.json"
    assert Path(written["validated_response_path"]).name == "validated_response.json"


def test_same_invocation_output_replays_safely(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    request = _build_council_request()
    output = _build_valid_output(request)
    first = api_adapter.write_api_adapter_artifacts_dry_run(output)
    second = api_adapter.write_api_adapter_artifacts_dry_run(output)

    assert first["created"] is True
    assert first["replayed"] is False
    assert second["created"] is False
    assert second["replayed"] is True
    assert first["invocation_path"] == second["invocation_path"]


def test_same_invocation_id_with_different_payload_is_closed_on_collision(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    request = _build_council_request()
    first_output = _build_valid_output(request)
    first = api_adapter.write_api_adapter_artifacts_dry_run(first_output)
    assert first["created"] is True

    mutated = deepcopy(first_output)
    mutated["provider_response"]["risk_flags"] = ["changed"]

    second = api_adapter.write_api_adapter_artifacts_dry_run(mutated, invocation_id=first_output["invocation_id"])

    assert second["created"] is False
    assert second["replayed"] is False
    assert any("idempotency_collision" in reason for reason in second["reasons"])
    assert second["invocation_path"] == first["invocation_path"]


def test_no_network_or_subprocess_in_api_adapter_chain(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    request = _build_council_request()
    invocation = api_adapter.build_api_adapter_invocation_dry_run(request, provider_id="mock", actor="system")

    with (
        patch("solution_agent.continuation.api_adapter.subprocess.run") as run_mock,
        patch("solution_agent.continuation.api_adapter.subprocess.check_output") as check_output_mock,
        patch("solution_agent.continuation.api_adapter.subprocess.Popen") as popen_mock,
        patch("solution_agent.continuation.api_adapter.socket.create_connection") as create_connection_mock,
    ):
        run_mock.side_effect = AssertionError("subprocess should not be called")
        check_output_mock.side_effect = AssertionError("subprocess should not be called")
        popen_mock.side_effect = AssertionError("subprocess should not be called")
        create_connection_mock.side_effect = AssertionError("network should not be called")

        output = api_adapter.invoke_mock_api_provider(
            invocation,
            mock_response=_default_mock_response(request["council_id"]),
        )
        written = api_adapter.write_api_adapter_artifacts_dry_run(output)

    assert written["status"] == "valid"
    assert written["created"] is True
    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called


def test_optional_ledger_stage_support_for_api_adapter(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    run_schema = yaml.safe_load(Path(".solution/continuation/run_ledger_schema.yaml").read_text(encoding="utf-8"))
    assert "api_adapter" in set(run_schema.get("allowed_stages", []))

    ledger_result = continuation_ledger.append_continuation_run_event(
        stage="api_adapter",
        status="succeeded",
        actor="system",
        idempotency_key="api_adapter:01",
        run_id="api_adapter_stage_check",
    )
    assert ledger_result["written"] in (True, False)
    assert ledger_result["path"] == str(tmp_path / "continuation" / "run_ledger" / "api_adapter_stage_check.jsonl")

    ledger_path = Path(ledger_result["path"])
    if ledger_result["written"]:
        events = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert any(event.get("stage") == "api_adapter" for event in events)
