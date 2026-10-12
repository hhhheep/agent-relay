from __future__ import annotations

from pathlib import Path

from solution_agent.continuation.validation_task_design_packet import (
    SCHEMA_PATH,
    build_b14_l74_validation_task_design_packet,
    load_validation_task_design_packet_schema,
    write_validation_task_design_packet,
)


def test_b14_l74_validation_task_design_packet_builder_uses_canonical_bucket_contract() -> None:
    schema = load_validation_task_design_packet_schema(SCHEMA_PATH)
    packet = build_b14_l74_validation_task_design_packet()
    payload = packet.model_dump(mode="json")

    for field in schema["required_fields"]:
        assert field in payload

    assert packet.packet_ready is True
    assert packet.bucket_id == "B14-L74"
    assert packet.baseline["path"] == "single_resize_448_current_path"
    assert packet.variant["path"] == "sidecar_crop_retoken_path"
    assert packet.task_contract["shared_bucket_required"] is True
    assert packet.task_contract["same_mask_inputs_if_available"] is True
    assert packet.task_contract["same_p2_inputs_if_available"] is True
    assert packet.task_contract["dispatch_allowed"] is False
    assert packet.task_contract["active_inbox_write_allowed"] is False
    assert packet.side_effects == schema["required_side_effects"]

    required_files = {item.filename for item in packet.required_artifacts}
    assert set(schema["required_outputs"]).issubset(required_files)
    assert set(schema["decision_summary_any_of"]).issubset(required_files)

    categories = {item.category for item in packet.required_artifacts}
    assert set(schema["required_categories"]).issubset(categories)


def test_b14_l74_validation_task_design_packet_writer_is_local_only(tmp_path: Path) -> None:
    packet = build_b14_l74_validation_task_design_packet()

    written = write_validation_task_design_packet(
        packet,
        tmp_path / "b14_l74_validation_task_design_packet.json",
    )

    assert written["status"] == "written"
    assert written["packet"]["packet_id"] == "B14-L74_validation_task_design_packet"
    assert written["packet"]["validation_bucket_path"].replace("\\", "/").endswith(
        ".solution/continuation/b14_l74_bounded_validation_bucket.yaml"
    )
    assert written["packet"]["task_contract"]["decision_summary_any_of"] == [
        "decision_summary.json",
        "decision_summary.yaml",
    ]
    assert written["side_effects"] == {
        "remote_execution": False,
        "scheduler_dispatch": False,
        "active_inbox_write": False,
        "registry_mutation": False,
        "canonical_kb_mutation": False,
    }
