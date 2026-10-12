"""Sprint D tests: build_review_packet assembles correct directory."""
from __future__ import annotations

from pathlib import Path

import yaml

from solution_agent.execution_port.review_packet import build_review_packet


def _make_job_dir(tmp_path: Path) -> Path:
    job = tmp_path / "job_abc"
    job.mkdir()

    spec = {
        "task_id": "t1",
        "job_type": "small_probe",
        "workspace": {"root": str(tmp_path), "cwd": str(tmp_path)},
        "command": {"script_id": "test_write_metrics", "args": {}, "timeout_sec": 10},
        "resources": {"gpu_required": False, "gpu_count": 0},
        "artifacts": {"required": []},
        "success_criteria": {"exit_code": 0, "metrics_file": "", "metric_keys": []},
    }
    (job / "execution_spec.yaml").write_text(
        yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8"
    )

    final = {
        "job_id": "job_abc",
        "task_id": "t1",
        "status": "failed",
        "exit_code": 1,
        "artifacts_missing": ["out.txt"],
        "metrics_missing": [],
        "started_at": "2026-01-01T00:00:00",
        "finished_at": "2026-01-01T00:00:05",
        "collected_at": "2026-01-01T00:00:06",
    }
    (job / "final_result.yaml").write_text(
        yaml.safe_dump(final, allow_unicode=True), encoding="utf-8"
    )

    collect_report = {"job_id": "job_abc", "checks": []}
    (job / "collect_report.yaml").write_text(
        yaml.safe_dump(collect_report, allow_unicode=True), encoding="utf-8"
    )

    state = {"status": "finished", "exit_code": 1}
    import json
    (job / "state.json").write_text(json.dumps(state), encoding="utf-8")

    (job / "stdout.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (job / "stderr.log").write_text("err1\nerr2\n", encoding="utf-8")

    return job


# ── basic structure ───────────────────────────────────────────────────────────

def test_review_packet_creates_directory(tmp_path):
    job = _make_job_dir(tmp_path)
    dest = build_review_packet(job)
    assert dest.is_dir()


def test_review_packet_copies_structured_files(tmp_path):
    job = _make_job_dir(tmp_path)
    dest = build_review_packet(job)
    for name in ("execution_spec.yaml", "final_result.yaml",
                 "collect_report.yaml", "state.json"):
        assert (dest / name).exists(), f"missing {name}"


def test_review_packet_tails_stdout(tmp_path):
    job = _make_job_dir(tmp_path)
    dest = build_review_packet(job)
    tail = (dest / "stdout_tail.txt").read_text(encoding="utf-8")
    assert "line1" in tail
    assert "line3" in tail


def test_review_packet_tails_stderr(tmp_path):
    job = _make_job_dir(tmp_path)
    dest = build_review_packet(job)
    tail = (dest / "stderr_tail.txt").read_text(encoding="utf-8")
    assert "err1" in tail


def test_review_packet_writes_summary_yaml(tmp_path):
    job = _make_job_dir(tmp_path)
    dest = build_review_packet(job)
    summary_path = dest / "review_summary.yaml"
    assert summary_path.exists()
    summary = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["exit_code"] == 1
    assert "out.txt" in summary["artifacts_missing"]
    assert "execution_spec.yaml" in summary["files"]
    assert "stdout_tail.txt" in summary["files"]


def test_review_packet_copies_patch_diff_when_present(tmp_path):
    job = _make_job_dir(tmp_path)
    (job / "patch.diff").write_bytes(b"--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n")
    dest = build_review_packet(job)
    assert (dest / "patch.diff").exists()


def test_review_packet_skips_patch_diff_when_absent(tmp_path):
    job = _make_job_dir(tmp_path)
    dest = build_review_packet(job)
    assert not (dest / "patch.diff").exists()


def test_review_packet_handles_missing_logs(tmp_path):
    job = _make_job_dir(tmp_path)
    (job / "stdout.log").unlink()
    (job / "stderr.log").unlink()
    dest = build_review_packet(job)
    assert "(file not found)" in (dest / "stdout_tail.txt").read_text(encoding="utf-8")
    assert "(file not found)" in (dest / "stderr_tail.txt").read_text(encoding="utf-8")


def test_review_packet_custom_dest(tmp_path):
    job = _make_job_dir(tmp_path)
    custom = tmp_path / "my_packet"
    dest = build_review_packet(job, dest=custom)
    assert dest == custom
    assert custom.is_dir()
    assert (custom / "review_summary.yaml").exists()


def test_review_packet_tail_truncates_long_log(tmp_path):
    job = _make_job_dir(tmp_path)
    lines = [f"line{i}" for i in range(200)]
    (job / "stdout.log").write_text("\n".join(lines), encoding="utf-8")
    dest = build_review_packet(job)
    tail = (dest / "stdout_tail.txt").read_text(encoding="utf-8").splitlines()
    assert len(tail) == 100
    assert tail[0] == "line100"
    assert tail[-1] == "line199"
