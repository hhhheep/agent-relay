from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import yaml

from solution_agent.continuation import knowledge_curator


def _patch_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.orchestrator.paths.root", lambda: tmp_path)
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)


def _evidence(evidence_type: str = "validated_report") -> list[dict]:
    return [
        {
            "evidence_id": "report_001",
            "evidence_type": evidence_type,
            "summary": "Validated report supports this update.",
            "authority": "current_report",
        }
    ]


def test_superseded_instruction_creates_patch_not_next_action(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    proposal = knowledge_curator.build_knowledge_update_proposal_dry_run(
        {
            "source_kind": "experiment_report",
            "source_id": "b14_l63_completion",
            "source_evidence": _evidence(),
            "observations": {
                "superseded_instruction": {
                    "old_instruction": "Run L6.3-V1 first",
                    "source": "PROJECT_HANDOFF_FOR_NEXT_CHAT.md",
                    "current_handling": "historical_context_only",
                }
            },
        }
    )

    assert proposal["status"] == "ready"
    assert "superseded_map" in proposal["patches"]
    entry = proposal["patches"]["superseded_map"]["entries"][0]
    assert entry["old_instruction"] == "Run L6.3-V1 first"
    assert "current_next_step" in entry["invalid_use"]
    assert "next_safe_actions" not in proposal["patches"]


def test_smoke_result_creates_claim_limits_not_formal_success(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    proposal = knowledge_curator.build_knowledge_update_proposal_dry_run(
        {
            "source_kind": "experiment_report",
            "source_id": "decoder_p0_smoke",
            "source_evidence": _evidence("smoke"),
            "observations": {
                "smoke_result": {
                    "subject": "bounded_decoder_p0",
                    "can_claim": ["interface_pathway_works"],
                }
            },
        }
    )

    assert proposal["status"] == "ready"
    claim_entry = proposal["patches"]["claim_limits"]["entries"][0]
    assert claim_entry["status"] == "smoke_only"
    assert "final_method_success" in claim_entry["cannot_claim"]
    assert proposal["patches"]["decision_ledger"]["claim_level"] == "interface_or_pathway_only"


def test_failed_branch_creates_rejected_path_entry(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    proposal = knowledge_curator.build_knowledge_update_proposal_dry_run(
        {
            "source_kind": "experiment_report",
            "source_id": "v2_refiner_report",
            "source_evidence": _evidence("diagnostic"),
            "observations": {
                "failed_branch": {
                    "branch": "V2/V2b/V2c local refiner",
                    "reason": "Failed due to real-negative detail leakage.",
                }
            },
        }
    )

    assert proposal["status"] == "ready"
    entry = proposal["patches"]["decision_ledger"]["entries"][0]
    assert entry["status"] == "failed_or_rejected_path"
    assert "real-negative detail leakage" in entry["decision"]


def test_current_mainline_creates_state_and_next_safe_action(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    proposal = knowledge_curator.build_knowledge_update_proposal_dry_run(
        {
            "source_kind": "experiment_report",
            "source_id": "l66_mainline",
            "source_evidence": _evidence(),
            "observations": {
                "current_mainline": {
                    "mainline": "mask/evidence -> calibrated bbox -> gated P2 fallback",
                    "known_bottleneck": "real-negative false-detail evidence",
                    "next_safe_action": "Run bounded decoder adapter/audit against existing L66 artifacts.",
                }
            },
        }
    )

    assert proposal["status"] == "ready"
    assert proposal["patches"]["current_state"]["entries"][0]["mainline"] == "mask/evidence -> calibrated bbox -> gated P2 fallback"
    assert "next_safe_actions" in proposal["patches"]


def test_missing_source_evidence_blocks_proposal(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    proposal = knowledge_curator.build_knowledge_update_proposal_dry_run(
        {
            "source_kind": "research_dialogue",
            "source_id": "opinion_only",
            "observations": {
                "current_mainline": {
                    "mainline": "some unsupported idea",
                    "next_safe_action": "do the unsupported thing",
                }
            },
        }
    )

    assert proposal["status"] == "blocked"
    assert "missing_source_evidence" in proposal["gate_report"]["reasons"]


def test_write_proposal_creates_only_proposed_artifacts(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    proposal = knowledge_curator.build_knowledge_update_proposal_dry_run(
        {
            "source_kind": "experiment_report",
            "source_id": "decoder_p0_smoke",
            "source_evidence": _evidence("smoke"),
            "observations": {"smoke_result": {"subject": "bounded_decoder_p0"}},
        }
    )
    written = knowledge_curator.write_knowledge_update_proposal_dry_run(proposal)

    proposal_dir = Path(written["proposal_dir"])
    assert written["created"] is True
    assert proposal_dir == tmp_path / "continuation" / "knowledge_updates" / "_proposed" / proposal["proposal_id"]
    assert (proposal_dir / "kb_update_manifest.yaml").exists()
    assert (proposal_dir / "claim_limits_patch.yaml").exists()
    assert not (tmp_path / ".solution" / "knowledge").exists()

    second = knowledge_curator.write_knowledge_update_proposal_dry_run(proposal)
    assert second["created"] is False
    assert second["replayed"] is True


def test_contract_files_and_ledger_stage_are_present() -> None:
    policy = yaml.safe_load(Path(".solution/continuation/knowledge_curator_policy.yaml").read_text(encoding="utf-8"))
    schema = yaml.safe_load(Path(".solution/continuation/knowledge_curator_schema.yaml").read_text(encoding="utf-8"))
    ledger = yaml.safe_load(Path(".solution/continuation/run_ledger_schema.yaml").read_text(encoding="utf-8"))

    assert "superseded_map" in policy["canonical_targets"]
    assert "claim_limits" in schema["allowed_patch_targets"]
    assert "knowledge_curator" in ledger["allowed_stages"]
    assert policy["promotion"]["auto_promote_allowed"] is False


def test_no_subprocess_or_network_calls(tmp_path: Path, monkeypatch) -> None:
    _patch_root(tmp_path, monkeypatch)
    with (
        patch("solution_agent.continuation.knowledge_curator.subprocess.run") as run_mock,
        patch("solution_agent.continuation.knowledge_curator.subprocess.check_output") as check_output_mock,
        patch("solution_agent.continuation.knowledge_curator.subprocess.Popen") as popen_mock,
        patch("solution_agent.continuation.knowledge_curator.socket.create_connection") as create_connection_mock,
    ):
        run_mock.side_effect = AssertionError("subprocess should not be called")
        check_output_mock.side_effect = AssertionError("subprocess should not be called")
        popen_mock.side_effect = AssertionError("subprocess should not be called")
        create_connection_mock.side_effect = AssertionError("network should not be called")

        proposal = knowledge_curator.build_knowledge_update_proposal_dry_run(
            {
                "source_kind": "experiment_report",
                "source_id": "decoder_p0_smoke",
                "source_evidence": _evidence("smoke"),
                "observations": {"smoke_result": {"subject": "bounded_decoder_p0"}},
            }
        )
        knowledge_curator.write_knowledge_update_proposal_dry_run(proposal)

    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called
