from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from solution_agent.context_index import build_context_index, query_context_index, write_context_documents
from solution_agent.continuation import project_dialogue, project_knowledge_ingestion


def _write_docs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text(
        "# B14 Agent Handoff\n\nCurrent Technical State: Do not treat P2-only bbox as the final bbox method.\n"
        "Recommended Next Step: bounded decoder module.\n",
        encoding="utf-8",
    )
    (root / "PROJECT_HANDOFF_FOR_NEXT_CHAT.md").write_text(
        "# Handoff\n\nUpdated: 2026-05-18\n\nRecommended Next Experiment\n\nRun L6.3-V1 first.\n",
        encoding="utf-8",
    )
    (root / "chronology.md").write_text(
        "# Chronology\n\nL6.3-V1 audit was completed. The current stable direction is "
        "mask/evidence -> calibrated bbox -> gated P2 fallback. The current bottleneck is "
        "real-negative false-detail evidence. Decoder P0 smoke is interface evidence only.\n",
        encoding="utf-8",
    )
    (root / "B14_L66_mask_to_bbox_calibration_core_grid.md").write_text(
        "# L66\n\nDo not use P2-only as final bbox output. M5 has recall but too many real boxes/image.\n",
        encoding="utf-8",
    )


def test_project_knowledge_ingestion_extracts_b14_state(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write_docs(docs)

    bundle = project_knowledge_ingestion.build_project_knowledge_from_docs_dry_run(
        docs,
        workplace_profile="EXAMPLE_WORKER_A_GenD",
        knowledge_root_ref="remote_handoff_package_docs",
    )

    assert bundle["project_id"] == "B14_spatial_grace_graph_utility"
    assert bundle["current_state"]["mainline"] == "mask/evidence -> calibrated bbox -> gated P2 fallback"
    assert any(item["item"] == "Run L6.3-V1 first" for item in bundle["completed_or_superseded"])
    assert any("real-negative" in item["issue"] for item in bundle["bottlenecks"])
    assert any("P2-only" in item["limit"] for item in bundle["claim_limits"])
    assert bundle["project_knowledge_binding"]["kind"] == "remote_project_kb_binding"


def test_ingested_bundle_feeds_project_dialogue(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write_docs(docs)
    bundle = project_knowledge_ingestion.build_project_knowledge_from_docs_dry_run(docs)

    answer = project_dialogue.build_project_understanding_answer_dry_run(
        bundle,
        question="我們在做什麼，難點在哪裡？",
    )

    assert answer["current_mainline"] == "mask/evidence -> calibrated bbox -> gated P2 fallback"
    assert any("P2-only" in json.dumps(item, ensure_ascii=False) for item in answer["claim_limits"])
    assert any(item["status"] == "superseded" for item in answer["completed_or_superseded"])


def test_write_project_knowledge_bundle_is_idempotent(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write_docs(docs)
    bundle = project_knowledge_ingestion.build_project_knowledge_from_docs_dry_run(docs)
    output = tmp_path / "out" / "project_knowledge.json"

    first = project_knowledge_ingestion.write_project_knowledge_bundle_dry_run(bundle, output)
    second = project_knowledge_ingestion.write_project_knowledge_bundle_dry_run(bundle, output)

    assert first["created"] is True
    assert second["replayed"] is True
    assert output.exists()


def test_ingestion_no_subprocess_or_network_calls(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write_docs(docs)
    with (
        patch("solution_agent.continuation.project_knowledge_ingestion.subprocess.run") as run_mock,
        patch("solution_agent.continuation.project_knowledge_ingestion.subprocess.check_output") as check_output_mock,
        patch("solution_agent.continuation.project_knowledge_ingestion.subprocess.Popen") as popen_mock,
        patch("solution_agent.continuation.project_knowledge_ingestion.socket.create_connection") as create_connection_mock,
    ):
        run_mock.side_effect = AssertionError("subprocess should not be called")
        check_output_mock.side_effect = AssertionError("subprocess should not be called")
        popen_mock.side_effect = AssertionError("subprocess should not be called")
        create_connection_mock.side_effect = AssertionError("network should not be called")

        project_knowledge_ingestion.build_project_knowledge_from_docs_dry_run(docs)

    assert not run_mock.called
    assert not check_output_mock.called
    assert not popen_mock.called
    assert not create_connection_mock.called


def test_project_knowledge_can_use_context_index_without_rescanning_docs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    session_dir = root / "continuation" / "project_dialogue" / "sessions" / "b14_state"
    session_dir.mkdir(parents=True)
    (session_dir / "project_understanding_answer.json").write_text(
        json.dumps(
            {
                "project_id": "B14_spatial_grace_graph_utility",
                "question": "我們在做什麼，做到什麼程度？難點在哪裡？",
                "state_summary": "Project is in a bounded decoder/interface validation stage.",
                "current_mainline": "mask/evidence -> calibrated bbox -> gated P2 fallback",
                "completed_or_superseded": [
                    {"item": "Run L6.3-V1 first", "status": "superseded"},
                    {"item": "P2-only final bbox method", "status": "rejected_as_final_method"},
                ],
                "bottlenecks": [{"issue": "real-negative false-detail evidence"}],
                "claim_limits": [
                    {"limit": "P2-only is a proposal baseline / gated fallback candidate, not the final bbox method."},
                    {"limit": "Decoder P0 smoke success is interface/pathway evidence only, not formal bbox quality."},
                ],
                "next_safe_action": {
                    "action": "Run bounded decoder adapter/audit against existing calibration artifacts.",
                    "why_safe": "It validates the interface without full training.",
                },
                "project_knowledge_binding": {
                    "workplace_profile": "EXAMPLE_WORKER_A_GenD",
                    "knowledge_root_ref": "remote_handoff_package_docs",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    write_context_documents(root)
    build_context_index(root)
    raw_query = query_context_index(root, "B14 calibrated bbox gated P2 real-negative", limit=3)
    bundle = project_knowledge_ingestion.build_project_knowledge_from_context_index_dry_run(
        root,
        "B14 calibrated bbox gated P2 real-negative",
        project_id="B14_spatial_grace_graph_utility",
        workplace_profile="EXAMPLE_WORKER_A_GenD",
    )

    assert raw_query["results"][0]["source_type"] == "project_dialogue"
    assert bundle["ingestion"]["mode"] == "context_index_dry_run"
    assert bundle["ingestion"]["side_effects"]["docs_dir_scan"] is False
    assert bundle["current_state"]["mainline"] == "mask/evidence -> calibrated bbox -> gated P2 fallback"
    assert any("real-negative" in item["issue"] for item in bundle["bottlenecks"])
    assert any("P2-only" in item["limit"] for item in bundle["claim_limits"])


def test_project_knowledge_can_use_artifact_index_without_rescanning_docs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    artifact_path = root / ".solution" / "context" / "artifact_index.jsonl"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task_id": "b14_l66_mask_to_bbox_calibration",
                        "kind": "collected_run",
                        "status": "completed",
                        "summary": "mask/evidence -> calibrated bbox -> gated P2 fallback; real-negative false-detail evidence remains; P2-only is not final bbox.",
                        "claim_scope": "Decoder P0 smoke is interface evidence only, not formal bbox quality.",
                        "result_path": "results/b14_l66/latest/result.yaml",
                        "produced_artifacts": ["results/b14_l66/latest/calibration_report.json"],
                        "tags": ["B14", "calibrated_bbox", "real-negative", "P2-only"],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "task_id": "b14_l70_large_image_coordinate_protocol_audit",
                        "kind": "collected_run",
                        "status": "partial",
                        "summary": "Large-image coordinate audit suggests bounded decoder smoke next.",
                        "result_path": "results/b14_l70/latest/result.yaml",
                        "tags": ["B14", "large-image", "decoder", "smoke"],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    bundle = project_knowledge_ingestion.build_project_knowledge_from_artifact_index_dry_run(
        root,
        "B14 calibrated bbox real-negative P2-only",
        project_id="B14_spatial_grace_graph_utility",
        workplace_profile="EXAMPLE_WORKER_A_GenD",
    )

    assert bundle["ingestion"]["mode"] == "artifact_index_dry_run"
    assert bundle["ingestion"]["side_effects"]["docs_dir_scan"] is False
    assert bundle["ingestion"]["side_effects"]["context_index_query"] is False
    assert bundle["current_state"]["mainline"] == "mask/evidence -> calibrated bbox -> gated P2 fallback"
    assert any("real-negative" in item["issue"] for item in bundle["bottlenecks"])
    assert any("P2-only" in item["limit"] for item in bundle["claim_limits"])
    assert bundle["project_knowledge_binding"]["kind"] == "remote_project_kb_binding"
