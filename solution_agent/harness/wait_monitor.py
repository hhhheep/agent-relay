from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from solution_agent.execution_port.io_utils import atomic_write_json
from solution_agent.execution_port.monitor import check_once

from .wait_contracts import utc_now
from .wait_runtime import (
    evaluate_stuck_breaker,
    load_wait_state,
    synchronize_execution_state,
    synchronize_observation,
)


def monitor_until_material(job_dir: Path, *, interval: float = 5.0) -> int:
    wait_dir = job_dir / "wait"
    wait_dir.mkdir(parents=True, exist_ok=True)
    pid_path = wait_dir / "monitor.pid"
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        while True:
            atomic_write_json(
                wait_dir / "monitor_heartbeat.json",
                {"pid": os.getpid(), "observed_at": utc_now().isoformat()},
            )
            try:
                execution_state = check_once(job_dir)
                synchronize_observation(job_dir, available=True)
                wait_state = synchronize_execution_state(job_dir, execution_state)
                if wait_state.lifecycle_state == "WAITING_EXTERNAL":
                    wait_state = evaluate_stuck_breaker(job_dir)
            except (OSError, ValueError, RuntimeError) as exc:
                try:
                    synchronize_observation(job_dir, available=False, detail=type(exc).__name__)
                except (OSError, ValueError, RuntimeError):
                    pass
                time.sleep(interval)
                continue
            if wait_state.lifecycle_state != "WAITING_EXTERNAL":
                return 0
            time.sleep(interval)
    finally:
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink(missing_ok=True)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic no-model wait monitor for one Harness job.")
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    return monitor_until_material(args.job_dir.resolve(), interval=args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
