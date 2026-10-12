from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from solution_agent.remote_materializer.api_metrics import summarize_api_metrics, write_api_call_metric


def test_api_metrics_threaded_writes_do_not_drop_lines(monkeypatch):
    root = Path(".test_tmp") / f"api_metrics_{uuid.uuid4().hex}"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    path = root / "api_model_calls.jsonl"
    monkeypatch.setenv("SOLUTION_AGENT_API_METRICS_PATH", str(path))
    monkeypatch.delenv("SOLUTION_AGENT_API_METRICS_DISABLED", raising=False)
    try:
        def one(index: int) -> None:
            write_api_call_metric(
                provider="test",
                model="same-api",
                role="materializer",
                status="success",
                latency_ms=index,
                messages=[{"role": "user", "content": f"msg {index}"}],
                response_text="ok",
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(one, range(1000)))

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1000
        assert all(json.loads(line)["model"] == "same-api" for line in lines)
        summary = summarize_api_metrics(path)
        assert summary["total_calls"] == 1000
        assert summary["groups"][0]["calls"] == 1000
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_api_metrics_process_writes_do_not_drop_lines():
    root = Path(".test_tmp") / f"api_metrics_proc_{uuid.uuid4().hex}"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    path = root / "api_model_calls.jsonl"
    script = (
        "from solution_agent.remote_materializer.api_metrics import write_api_call_metric\n"
        "import os, sys\n"
        "start=int(sys.argv[1]); count=int(sys.argv[2])\n"
        "for i in range(start, start+count):\n"
        "    write_api_call_metric(provider='test', model='proc-api', role='materializer', status='success', latency_ms=i, messages=[{'role':'user','content':str(i)}], response_text='ok')\n"
    )
    env = os.environ.copy()
    env["SOLUTION_AGENT_API_METRICS_PATH"] = str(path)
    try:
        processes = [
            subprocess.Popen([sys.executable, "-c", script, str(index * 100), "100"], env=env)
            for index in range(8)
        ]
        for process in processes:
            assert process.wait(timeout=30) == 0
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 800
        assert all(json.loads(line)["model"] == "proc-api" for line in lines)
        summary = summarize_api_metrics(path)
        assert summary["total_calls"] == 800
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_api_metrics_reports_provider_cache_and_new_input(monkeypatch):
    root = Path(".test_tmp") / f"api_metrics_usage_{uuid.uuid4().hex}"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    path = root / "api_model_calls.jsonl"
    monkeypatch.setenv("SOLUTION_AGENT_API_METRICS_PATH", str(path))
    monkeypatch.delenv("SOLUTION_AGENT_API_METRICS_DISABLED", raising=False)
    try:
        write_api_call_metric(
            provider="bailian",
            model="deepseek-v4.1-flash",
            role="report_writer",
            status="success",
            latency_ms=10,
            messages=[{"role": "user", "content": "evidence"}],
            response_text="report",
            usage={
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "prompt_tokens_details": {"cached_tokens": 750},
            },
            cost_usd=0.0012,
        )

        event = json.loads(path.read_text(encoding="utf-8").strip())
        assert event["normalized_usage"]["new_input_tokens"] == 250
        assert event["normalized_usage"]["cache_read_tokens"] == 750
        summary = summarize_api_metrics(path)
        group = summary["groups"][0]
        assert group["new_input_tokens_total"] == 250
        assert group["cache_read_tokens_total"] == 750
        assert group["total_tokens_total"] == 1200
        assert group["cost_usd_total"] == 0.0012
    finally:
        shutil.rmtree(root, ignore_errors=True)
