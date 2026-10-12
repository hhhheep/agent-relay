from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import pytest

from scripts.reports import run_feature_leakage_maskout_probe as maskout


def test_metric_delta_rows_computes_numeric_deltas():
    rows = maskout._metric_delta_rows(
        {"patch_auc": 0.8, "heatmap_iou": 0.2},
        {"zero_clean_tokens": {"patch_auc": 0.7, "heatmap_iou": 0.25}},
    )

    auc = next(row for row in rows if row["metric"] == "patch_auc")
    assert auc["delta"] == pytest.approx(-0.1)


def test_maskout_runner_can_use_mocked_oracle_helpers(tmp_path, monkeypatch):
    pytest.importorskip("torch")
    workspace = tmp_path / "workspace"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    module = scripts / "run_gend_middle_layer_oracle_v2.py"
    module.write_text(
        r'''
import csv
import numpy as np
import torch
from PIL import Image

class TokenHead(torch.nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.net = torch.nn.Linear(dim, 1)
    def forward(self, x):
        return self.net(x).squeeze(-1)

def binary_metrics(labels, scores):
    return {"auc": 0.5, "ap": 0.5}

def build_model(model_name, device):
    return torch.nn.Identity(), {"input_size": 8, "patch_size": 4, "num_layers": 24}

def candidate_layers(num_layers, selected_indices=None):
    return [("Lminus4", selected_indices[0])]

class Rec:
    label = 1
    image_id = "x"
    split = "valid"
class Item:
    rec = Rec()

def collect_split(root, split, n_fake, n_real, seed):
    return [Rec() for _ in range(max(1, n_fake + n_real))]

def make_crops(records, tile, max_crops):
    return [Item() for _ in records]

def mask_overlaps(items, grid):
    return np.tile(np.array([[0.0, 1.0, 0.0, 1.0]], dtype=float), (len(items), 1))

def load_crop(item):
    return Image.new("RGB", (8, 8), "white")

def extract_layer_tokens(model, model_name, images, input_size, grid, layers, device):
    batch = len(images)
    tokens = torch.randn(batch, grid[0] * grid[1], 1024, device=device)
    return {layers[0][1]: tokens}

def iou_dice(labels, scores):
    return {"iou_05": 0.1, "dice_05": 0.2}

def clean_top_metrics(labels, scores):
    return {
        "clean_purity_bottom20": 0.8,
        "fake_contamination_bottom20": 0.2,
        "top_fake_recall_top10": 0.4,
    }

def context_leakage(items, labels, scores):
    return {"context_leakage_gap": 0.01}

def write_csv(path, rows):
    fields = list(rows[0])
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
''',
        encoding="utf-8",
    )
    sys.modules.pop("run_gend_middle_layer_oracle_v2", None)
    monkeypatch.setattr(maskout.torch.cuda, "is_available", lambda: False)
    args = argparse.Namespace(
        output_dir=str(tmp_path / "out"),
        workspace_root=str(workspace),
        data_root=str(tmp_path / "data"),
        model="GenD_DINOv3_L",
        layer_index=20,
        train_fake=1,
        train_real=1,
        eval_fake=1,
        eval_real=1,
        tile_size=8,
        max_crops=1,
        clean_threshold=0.05,
        fake_threshold=0.5,
        epochs=1,
        batch_size=1,
        eval_batch_size=1,
        lr=1e-3,
        weight_decay=1e-4,
        max_pos_weight=50.0,
        seed=1,
        allow_cpu=True,
    )

    assert maskout.run(args) == 0

    output = Path(args.output_dir)
    assert (output / "maskout_clean_token_probe.csv").exists()
    assert (output / "maskout_clean_token_delta.csv").exists()
    with (output / "maskout_clean_token_probe.csv").open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["maskout_mode"] for row in rows} == {"baseline", "zero_clean_tokens", "mean_clean_tokens"}
