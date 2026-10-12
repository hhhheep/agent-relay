from __future__ import annotations

import csv
import json

from scripts.reports.run_feature_leakage_followup_packet import run


def test_feature_leakage_followup_packet_writes_review_outputs(tmp_path):
    workspace = tmp_path / "workspace"
    output = tmp_path / "out"
    clean_dir = workspace / "analysis_outputs" / "v2_run1_10k_dinov3_hs18_hs20_1crop"
    clean_dir.mkdir(parents=True)
    (clean_dir / "clean_source_availability.csv").write_text(
        "image_id,clean_source,score\n1,a,0.2\n2,b,0.4\n",
        encoding="utf-8",
    )
    config_dir = workspace / "analysis_outputs" / "v2_probe_dinov3_hs18_hs20_200x200_gpu_realmetrics"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({"crop_size": 200}), encoding="utf-8")
    code_dir = workspace / "src" / "model" / "forada" / "clip"
    code_dir.mkdir(parents=True)
    (code_dir / "simple_tokenizer.py").write_text("def tokenize(x): return x\n", encoding="utf-8")
    compression = workspace / "analysis_outputs" / "compression_boundary_dinov3_hs20_2k"
    compression.mkdir(parents=True)
    (compression / "variant_metrics.csv").write_text(
        "model,layer_name,hidden_state_index,variant,clean_purity_bottom20,fake_contamination_bottom20,context_leakage_gap\n"
        "GenD_DINOv3_L,hs20,20,original,0.93,0.07,0.11\n",
        encoding="utf-8",
    )
    (compression / "stability_metrics.csv").write_text(
        "model,layer_name,hidden_state_index,variant,rank_corr_mean,bottom20_jaccard_mean,top10_jaccard_mean,bottom20_fake_contamination_mean\n"
        "GenD_DINOv3_L,hs20,20,resize_downup_224,0.82,0.66,0.71,0.09\n",
        encoding="utf-8",
    )

    assert run(output, workspace) == 0

    assert (output / "cluster_visual_sheet.jpg").exists()
    assert (output / "clean_center_perturbation_metrics.csv").exists()
    assert (output / "context_size_sensitivity.csv").exists()
    assert (output / "maskout_clean_token_probe.csv").exists()
    assert (output / "decision_summary.json").exists()
    assert (output / "action_requests.json").exists()
    with (output / "clean_center_perturbation_metrics.csv").open("r", encoding="utf-8", newline="") as handle:
        clean_rows = list(csv.DictReader(handle))
    assert clean_rows[0]["metric_status"] == "computed_existing_artifact"
    decision = json.loads((output / "decision_summary.json").read_text(encoding="utf-8"))
    assert decision["execution_success"] is True
    assert decision["new_gpu_execution"] is False
    assert decision["clean_center_perturbation_metrics"] == "partial_existing_compression_boundary_evidence"
    actions = json.loads((output / "action_requests.json").read_text(encoding="utf-8"))
    assert [item["missing_or_needed"] for item in actions["action_requests"]] == ["maskout_clean_token_probe_runner"]
