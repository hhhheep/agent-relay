from __future__ import annotations

import csv
from pathlib import Path

from scripts.loupe.run_manifest_metadata_audit import run


def test_manifest_metadata_audit_reads_csv_without_opening_images(tmp_path):
    manifest = tmp_path / "datasets" / "train" / "manifest.csv"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        "path,label,split,width,height,file_size_bytes\n"
        "/data/train/fake/a.jpg,fake,train,512,256,100000\n"
        "/data/train/real/b.png,real,train,256,256,50000\n",
        encoding="utf-8",
    )
    output = tmp_path / "out"

    assert run([tmp_path / "datasets"], output, max_manifest_depth=4) == 0

    with (output / "metadata_all.csv").open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["label"] == "fake"
    assert rows[0]["resolution_bucket"] == "512_1023"
    assert rows[1]["aspect_bucket"] == "square"
    assert (output / "metadata_pool_summary.csv").exists()
    assert (output / "metadata_by_label_split_extension.csv").exists()
    assert "No image files were opened" in (output / "worker_notes.md").read_text(encoding="utf-8")


def test_manifest_metadata_audit_extracts_nested_image_and_mask_paths(tmp_path):
    manifest = tmp_path / "datasets" / "train" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        """
[
  {"image": {"path": "/data/imgs/fake_a.png"}, "mask": {"path": "/data/masks/fake_a.png"}, "name": "fake_a.png"},
  {"image": {"path": "/data/imgs/real_b.png"}, "mask": null, "name": "real_b.png"}
]
""".strip(),
        encoding="utf-8",
    )
    output = tmp_path / "out"

    assert run([tmp_path / "datasets"], output, max_manifest_depth=4) == 0

    with (output / "metadata_all.csv").open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["path"] for row in rows] == ["/data/imgs/fake_a.png", "/data/imgs/real_b.png"]
    assert [row["label"] for row in rows] == ["fake", "real"]
    assert rows[0]["mask_path"] == "/data/masks/fake_a.png"
