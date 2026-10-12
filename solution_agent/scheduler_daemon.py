from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List

from .orchestrator import core as lab


def _allowed_gpu_ids(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Background remote scheduler loop.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--remote-workdir", required=True)
    parser.add_argument("--max-starts-per-cycle", type=int, default=4)
    parser.add_argument("--max-running-total", type=int, default=8)
    parser.add_argument("--max-running-gpu", type=int, default=8)
    parser.add_argument("--max-running-cpu", type=int, default=2)
    parser.add_argument("--min-free-gpu-mem-gb", type=float, default=10.0)
    parser.add_argument("--allowed-gpu-ids", default="0,1,2,4")
    parser.add_argument("--max-tasks-per-gpu", type=int, default=2)
    parser.add_argument("--interval-minutes", type=int, default=15)
    parser.add_argument("--max-cycles", type=int, default=0)
    parser.add_argument("--model", default="qwen2.5-3b-local:latest")
    parser.add_argument("--gpu-cache-seconds", type=int, default=120)
    parser.add_argument("--worker", default="materialized_execution")
    parser.add_argument("--log-dir", default="")
    args = parser.parse_args()

    if args.log_dir:
        log_dir = Path(args.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        sys.stdout = (log_dir / "scheduler_stdout.log").open("a", encoding="utf-8", buffering=1)
        sys.stderr = (log_dir / "scheduler_stderr.log").open("a", encoding="utf-8", buffering=1)

    allowed = _allowed_gpu_ids(args.allowed_gpu_ids)
    guard = lab.scheduler_startup_guard(
        host=args.host,
        remote_root=args.remote_root,
        remote_workdir=args.remote_workdir,
        max_starts=args.max_starts_per_cycle,
        max_running_total=args.max_running_total,
        max_running_gpu=args.max_running_gpu,
        max_running_cpu=args.max_running_cpu,
        min_free_gpu_mem_gb=args.min_free_gpu_mem_gb,
        allowed_gpu_ids=allowed,
        max_tasks_per_gpu=args.max_tasks_per_gpu,
        gpu_snapshot_max_age_seconds=args.gpu_cache_seconds,
        worker=args.worker,
    )
    if not guard.get("ok"):
        print(json.dumps({"error": "startup guard failed", "startup_guard": guard}, indent=2, ensure_ascii=False), flush=True)
        return 3

    lock = lab.acquire_scheduler_lock("schedule-loop-remote")
    if not lock.get("acquired"):
        print(json.dumps({"error": "scheduler already running", "lock": lock}, indent=2, ensure_ascii=False), flush=True)
        return 2

    _RESYNC_EVERY = 4  # run stale recovery every N cycles (~1 hour at 15-min intervals)

    reconnect_reconcile = lab.remote_reconcile_running_tasks()
    cycle = 0
    try:
        while True:
            lab.refresh_scheduler_lock()
            supervision = lab.supervise_running_remote_tasks(model=args.model, collect_finished=True)
            if cycle % _RESYNC_EVERY == 0:
                stale = lab.stale_running_recovery(
                    stale_after_minutes=args.interval_minutes * _RESYNC_EVERY,
                )
            else:
                stale = None
            schedule = lab.schedule_remote(
                host=args.host,
                remote_root=args.remote_root,
                remote_workdir=args.remote_workdir,
                max_starts=args.max_starts_per_cycle,
                max_running_total=args.max_running_total,
                max_running_gpu=args.max_running_gpu,
                max_running_cpu=args.max_running_cpu,
                min_free_gpu_mem_gb=args.min_free_gpu_mem_gb,
                allowed_gpu_ids=allowed,
                max_tasks_per_gpu=args.max_tasks_per_gpu,
                gpu_snapshot_max_age_seconds=args.gpu_cache_seconds,
                worker=args.worker,
                dry_run=False,
            )
            payload = {"cycle": cycle + 1, "lock": lock, "supervision": supervision, "schedule": schedule, "stale_recovery": stale}
            if reconnect_reconcile is not None:
                payload["reconnect_reconcile"] = reconnect_reconcile
                reconnect_reconcile = None
            print(json.dumps(payload, indent=2, ensure_ascii=False), flush=True)
            cycle += 1
            if args.max_cycles and cycle >= args.max_cycles:
                break
            sleep_until = time.time() + max(60, args.interval_minutes * 60)
            while time.time() < sleep_until:
                lab.refresh_scheduler_lock()
                time.sleep(min(60, max(1, sleep_until - time.time())))
    finally:
        lab.release_scheduler_lock()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
