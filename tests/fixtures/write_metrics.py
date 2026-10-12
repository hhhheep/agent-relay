"""Test fixture: write metrics.json to output_dir, optionally exit non-zero."""
import argparse
import json
import pathlib
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--output_dir", type=str, required=True)
parser.add_argument("--fail", action="store_true")
args = parser.parse_args()

out = pathlib.Path(args.output_dir)
out.mkdir(parents=True, exist_ok=True)
(out / "metrics.json").write_text(
    json.dumps({"auc": 0.91, "loss": 0.23}), encoding="utf-8"
)

if args.fail:
    sys.exit(1)
