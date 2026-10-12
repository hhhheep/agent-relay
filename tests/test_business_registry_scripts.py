from __future__ import annotations

import json
import subprocess
import sys
import yaml


def test_run_probe_smoke_writes_contract_artifacts(tmp_path):
    config = tmp_path / "config.yaml"
    output_dir = tmp_path / "out"
    config.write_text("sample_count: 1\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/probes/run_probe.py",
            "--config",
            str(config),
            "--output_dir",
            str(output_dir),
            "--smoke",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    metrics = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["ok"] is True
    assert metrics["script"] == "run_probe"
    assert (output_dir / "artifact.txt").exists()
    assert (output_dir / "summary.md").exists()


def test_run_eval_probe_writes_metrics(tmp_path):
    config = tmp_path / "config.yaml"
    output_dir = tmp_path / "out"
    config.write_text("sample_count: 2\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/probes/run_eval_probe.py",
            "--config",
            str(config),
            "--output_dir",
            str(output_dir),
            "--smoke",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    metrics = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["auc"] == 0.99
    assert metrics["ap"] == 0.98


def test_run_recipe_ablation_writes_variant_summary(tmp_path):
    config = tmp_path / "config.yaml"
    output_dir = tmp_path / "out"
    config.write_text("variant: tiny\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/eval/run_recipe_ablation.py",
            "--config",
            str(config),
            "--output_dir",
            str(output_dir),
            "--variant",
            "fallback",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    metrics = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["script"] == "run_recipe_ablation"
    assert metrics["variant"] == "tiny"


def test_contract_artifact_writer_writes_structured_artifacts(tmp_path):
    config = tmp_path / "contract_artifact_request.yaml"
    output_dir = tmp_path / "out"
    config.write_text(
        yaml.safe_dump(
            {
                "task_id": "contract_writer_test",
                "outputs": [
                    {
                        "path": "evidence_contract.md",
                        "kind": "markdown",
                        "title": "Evidence Contract",
                        "sections": [
                            {
                                "heading": "Required Metrics",
                                "bullets": ["loss curve rows must be present", "train_iou must be finite"],
                            }
                        ],
                    },
                    {
                        "path": "expected_metrics_schema.json",
                        "kind": "json",
                        "content": {"required": ["train_iou", "train_loss_final"]},
                    },
                    {
                        "path": "judge_rules.yaml",
                        "kind": "yaml",
                        "content": {"missing_evidence": {"when": "loss_curve_rows == 0"}},
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/reports/run_contract_artifact_writer.py",
            "--config",
            str(config),
            "--output_dir",
            str(output_dir),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "train_iou" in (output_dir / "evidence_contract.md").read_text(encoding="utf-8")
    assert json.loads((output_dir / "expected_metrics_schema.json").read_text(encoding="utf-8"))["required"] == [
        "train_iou",
        "train_loss_final",
    ]
    assert yaml.safe_load((output_dir / "judge_rules.yaml").read_text(encoding="utf-8"))["missing_evidence"]["when"]
    assert (output_dir / "worker_notes.md").exists()


def test_contract_artifact_writer_rejects_path_escape(tmp_path):
    config = tmp_path / "bad_request.yaml"
    output_dir = tmp_path / "out"
    config.write_text(
        yaml.safe_dump(
            {
                "task_id": "bad_contract_writer_test",
                "outputs": [{"path": "../escape.md", "kind": "markdown", "content": "bad"}],
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/reports/run_contract_artifact_writer.py",
            "--config",
            str(config),
            "--output_dir",
            str(output_dir),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode != 0
    assert "may not contain" in proc.stderr or "escapes output_dir" in proc.stderr
