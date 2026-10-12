from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--message", default="materialized execution smoke ok")
    parser.add_argument("--sleep_seconds", type=float, default=0.0)
    args = parser.parse_args()

    if args.sleep_seconds > 0:
        time.sleep(args.sleep_seconds)

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": True,
        "message": args.message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (out / "metrics.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (out / "artifact.txt").write_text(args.message + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
