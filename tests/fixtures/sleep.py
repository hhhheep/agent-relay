"""Test fixture: sleep for N seconds, optionally write output_dir/done.txt."""
import argparse
import pathlib
import time

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=int, default=1)
parser.add_argument("--output_dir", type=str, default=None)
args = parser.parse_args()

time.sleep(args.seconds)

if args.output_dir:
    out = pathlib.Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "done.txt").write_text("ok", encoding="utf-8")
