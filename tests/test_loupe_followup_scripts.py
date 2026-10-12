from __future__ import annotations

import csv
import json
from argparse import Namespace

from scripts.loupe import run_seg_overfit as seg_overfit_module
from scripts.loupe.run_matched_group_split import run as run_matched_group_split
from scripts.loupe.run_metadata_only_classifier import run as run_metadata_only_classifier
from scripts.loupe.run_protocol_report_writer import run as run_protocol_report_writer


def _write_metadata(path):
    path.write_text(
        "\n".join(
            [
                "image_id,path,pool_name,split_name,label,has_mask,mask_path,extension,width,height,aspect_ratio,is_square,resolution_bucket,aspect_bucket,file_size_bytes,bytes_per_pixel,color_mode,num_channels,exif_exists,jpeg_quality_proxy,folder_source_hint,near_duplicate_group",
                "a,/data/fake/a.jpg,pool,train,fake,true,/m/a.png,.jpg,512,512,1.0,true,512_1023,square,100000,0.38,RGB,3,false,,src_a,",
                "b,/data/real/b.png,pool,train,real,false,,.png,512,512,1.0,true,512_1023,square,200000,0.76,RGB,3,false,,src_b,",
                "c,/data/fake/c.jpg,pool,validation,fake,true,/m/c.png,.jpg,1024,512,2.0,false,1024_2047,wide,300000,0.57,RGB,3,false,,src_a,",
                "d,/data/real/d.png,pool,test,real,false,,.png,512,1024,0.5,false,1024_2047,tall,400000,0.76,RGB,3,false,,src_b,",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_metadata_only_classifier_writes_required_outputs(tmp_path):
    metadata = tmp_path / "metadata_all.csv"
    output = tmp_path / "out"
    _write_metadata(metadata)

    assert run_metadata_only_classifier(metadata, output) == 0

    assert (output / "metadata_only_metrics.json").exists()
    assert (output / "metadata_only_predictions.csv").exists()
    assert (output / "metadata_only_feature_importance.csv").exists()
    assert (output / "metadata_only_ablation_table.csv").exists()
    assert (output / "metadata_only_summary.md").exists()
    assert (output / "metadata_only_classifier_metrics.json").exists()
    assert (output / "metadata_only_split_summary.csv").exists()
    assert (output / "metadata_only_shortcut_summary.md").exists()
    assert (output / "artifact_summary.md").exists()
    assert (output / "action_requests.json").exists()
    metrics = json.loads((output / "metadata_only_metrics.json").read_text(encoding="utf-8"))
    assert metrics["label_counts"] == {"fake": 2, "real": 2}
    assert metrics["metrics"]["all"]["count"] == 4


def test_metadata_only_classifier_writes_closure_outputs_on_missing_input(tmp_path):
    output = tmp_path / "out"

    assert run_metadata_only_classifier(None, output) == 0

    assert (output / "worker_notes.md").exists()
    assert (output / "artifact_summary.md").exists()
    action_requests = json.loads((output / "action_requests.json").read_text(encoding="utf-8"))
    assert action_requests["status"] == "partial"
    assert action_requests["action_requests"]


def test_seg_overfit_writes_closure_outputs_on_missing_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(seg_overfit_module, "DEFAULT_DATA_DIR", tmp_path / "missing_default")
    monkeypatch.setattr(seg_overfit_module, "DEFAULT_RAW_DATA_ROOT", tmp_path / "missing_raw")
    output = tmp_path / "out"
    args = Namespace(
        output_dir=str(output),
        data_dir=str(tmp_path / "missing_explicit"),
        artifact_prefix="seg_overfit",
        workdir=str(tmp_path),
        sample_count=100,
        min_fake_samples=50,
        max_epochs=3,
        batch_size=4,
        seed=42,
    )

    assert seg_overfit_module.run(args) == 0

    assert (output / "worker_notes.md").exists()
    assert (output / "artifact_summary.md").exists()
    action_requests = json.loads((output / "action_requests.json").read_text(encoding="utf-8"))
    assert action_requests["status"] == "partial"
    assert action_requests["failure_taxonomy"] == "missing_evidence"
    assert action_requests["action_requests"]


def test_matched_group_split_writes_required_outputs(tmp_path):
    metadata = tmp_path / "metadata_all.csv"
    output = tmp_path / "out"
    _write_metadata(metadata)

    assert run_matched_group_split(metadata, output) == 0

    assert (output / "matched_group_split_manifest.csv").exists()
    assert (output / "matched_group_split.json").exists()
    assert (output / "matched_group_balance_table.csv").exists()
    assert (output / "matched_group_unmatched_cases.csv").exists()
    assert (output / "matched_group_split_summary.md").exists()
    with (output / "matched_group_split_manifest.csv").open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    assert {"train", "validation", "test"} & {row["assigned_split"] for row in rows}


def test_protocol_report_writer_writes_required_outputs_and_keeps_missing_visible(tmp_path):
    output = tmp_path / "out"
    artifact_summary = tmp_path / "artifact_summary.md"
    metadata = tmp_path / "metadata_only_metrics.json"
    split = tmp_path / "matched_group_split.json"
    artifact_summary.write_text(
        "\n".join(
            [
                "ddlx_loupe_v2_002_metadata_only_classifier completed",
                "ddlx_loupe_v2_003_format_normalized_eval partial missing_capability",
                "ddlx_loupe_v2_004_matched_group_split completed",
                "ddlx_loupe_v2_005_seg_100_fake_overfit partial missing_capability",
                "ddlx_loupe_v2_006_patch_evidence_visualization partial missing_capability",
            ]
        ),
        encoding="utf-8",
    )
    metadata.write_text(
        json.dumps({"metrics": {"all": {"auc": 0.6036, "accuracy": 0.57}}}),
        encoding="utf-8",
    )
    split.write_text(json.dumps({"row_count": 3248, "group_count": 20, "unmatched_count": 10}), encoding="utf-8")

    assert run_protocol_report_writer(output, artifact_summary, metadata, split) == 0

    assert (output / "loupe_protocol_decision_report.md").exists()
    assert (output / "loupe_protocol_result_table.csv").exists()
    assert (output / "next_task_recommendations.md").exists()
    assert (output / "worker_notes.md").exists()
    report = (output / "loupe_protocol_decision_report.md").read_text(encoding="utf-8")
    assert "AUC 0.963" in report
    assert "partial" in report
    with (output / "loupe_protocol_result_table.csv").open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert any(row["task_id"] == "ddlx_loupe_v2_003_format_normalized_eval" and row["decision"] == "retry" for row in rows)
