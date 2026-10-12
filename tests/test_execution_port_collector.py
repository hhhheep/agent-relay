"""Sprint 5 tests: collector.py — 5 status conditions + write-once contract."""
from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path

import pytest
import yaml

from solution_agent.execution_port.collector import collect, verify_collection_seal
from solution_agent.execution_port.io_utils import atomic_write_json, read_json
from solution_agent.execution_port.monitor import check_once
from solution_agent.execution_port.registry import register_execution
from solution_agent.execution_port.safe_runner import run_execution

_TEST_REGISTRY = Path(__file__).parent / "fixtures" / "script_registry_test.yaml"
_REPO_ROOT = Path(__file__).parent.parent


def test_failure_taxonomy_declares_required_routing_fields():
    taxonomy = yaml.safe_load((_REPO_ROOT / "solution_agent" / "orchestrator" / "failure_taxonomy.yaml").read_text(encoding="utf-8"))
    required = {
        "plan_parse_error",
        "plan_schema_error",
        "gate_denied_missing_approval",
        "gate_denied_heavy_read",
        "invalid_dataset_path",
        "path_policy_violation",
        "arg_policy_violation",
        "execution_failed_exit_code",
        "artifact_missing",
        "metrics_missing",
        "metrics_invalid",
        "weak_metric_result",
        "missing_evidence",
        "missing_capability",
        "proxy_model_fallback",
        "invalid_model_execution_evidence",
    }
    entries = taxonomy["failure_types"]
    assert required <= set(entries)
    for name in required:
        entry = entries[name]
        for field in ("repairable", "user_action_required", "codex_recommended", "execution_success", "warn_only"):
            assert field in entry
        assert "recommended_action" in entry
    assert entries["artifact_missing"]["repairable"] is False
    assert entries["artifact_missing"]["codex_recommended"] is True
    assert entries["metrics_missing"]["repairable"] is False
    assert entries["metrics_invalid"]["codex_recommended"] is True
    assert entries["missing_evidence"]["execution_success"] is True
    assert entries["missing_capability"]["recommended_action"]["type"] == "create_action_request"
    assert entries["proxy_model_fallback"]["warn_only"] is True
    assert entries["invalid_model_execution_evidence"]["execution_success"] is True


def _registry_with_metric_contract(tmp_path: Path) -> Path:
    payload = yaml.safe_load(_TEST_REGISTRY.read_text(encoding="utf-8"))
    payload["scripts"]["test_write_metrics"]["expected_metrics"] = {
        "file": "metrics.json",
        "required_keys": ["train_iou", "train_loss_final"],
        "numeric_keys": ["train_iou", "train_loss_final"],
        "finite_required": True,
    }
    payload["scripts"]["test_write_metrics"]["warning_rules"] = {
        "weak_metric_result": {
            "key": "train_iou",
            "lt": 0.1,
            "recommended_action": {
                "type": "no_retry",
                "message": "Weak finite metric; inspect model quality.",
            },
        }
    }
    path = tmp_path / "script_registry_metric_contract.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _registry_with_closure_contract(tmp_path: Path) -> Path:
    payload = yaml.safe_load(_TEST_REGISTRY.read_text(encoding="utf-8"))
    payload["closure_contract"] = {
        "default_required_files": [
            "worker_notes.md",
            "artifact_summary.md",
            "action_requests.json",
        ]
    }
    path = tmp_path / "script_registry_closure_contract.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _collect_fake_metric_job(
    tmp_path: Path,
    metrics: dict,
    metric_keys: list[str] | None = None,
    registry_path: Path | None = None,
) -> dict:
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "metrics.json").write_text(json.dumps(metrics, allow_nan=True), encoding="utf-8")
    stub = tmp_path / "stub.txt"
    stub.write_text("ok", encoding="utf-8")
    spec_path = _make_spec(
        tmp_path,
        "test_write_metrics",
        {"output_dir": str(out)},
        required_artifacts=["stub.txt"],
        metrics_file=str(Path("out") / "metrics.json"),
        metric_keys=metric_keys if metric_keys is not None else ["train_iou", "train_loss_final"],
        workspace_root=tmp_path,
    )
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, registry_path or _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id
    state = read_json(job_dir / "state.json")
    state.update(status="exited", exit_code=0, finished_at="2026-05-04T00:00:00+00:00")
    atomic_write_json(job_dir / "state.json", state)
    return collect(job_dir)


def _make_spec(
    tmp_path: Path,
    script_id: str,
    args: dict,
    required_artifacts: list[str] | None = None,
    metrics_file: str = "",
    metric_keys: list[str] | None = None,
    timeout: int = 8,
    workspace_root: Path | None = None,
    evidence: dict | None = None,
) -> Path:
    root = workspace_root or _REPO_ROOT
    spec = {
        "task_id": "test_task",
        "job_type": "test",
        "workspace": {
            "root": str(root),
            "cwd": str(root),
        },
        "command": {
            "script_id": script_id,
            "args": args,
            "timeout_sec": timeout,
        },
        "resources": {"gpu_required": False, "gpu_count": 0},
        "artifacts": {
            "required": required_artifacts or ["tests/fixtures/write_metrics.py"],
        },
        "success_criteria": {
            "exit_code": 0,
            "metrics_file": metrics_file,
            "metric_keys": metric_keys or [],
        },
        "evidence": evidence or {},
    }
    p = tmp_path / "execution_spec.yaml"
    p.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return p


def _run_and_wait(
    tmp_path: Path,
    script_id: str,
    args: dict,
    required_artifacts=None,
    metrics_file="",
    metric_keys=None,
    timeout=8,
    workspace_root=None,
) -> Path:
    """Register, run, wait for exit_code.txt, return job_dir."""
    spec_path = _make_spec(tmp_path, script_id, args, required_artifacts,
                           metrics_file, metric_keys, timeout, workspace_root)
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id
    run_execution(job_dir)

    deadline = time.monotonic() + 8
    while not (job_dir / "exit_code.txt").exists():
        if time.monotonic() > deadline:
            pytest.fail("exit_code.txt not written within 8s")
        time.sleep(0.2)

    check_once(job_dir)   # transition running → exited
    return job_dir


# ── 1. succeeded ─────────────────────────────────────────────────────────────

def test_collector_succeeded(tmp_path):
    out = tmp_path / "out"
    job_dir = _run_and_wait(
        tmp_path, "test_write_metrics", {"output_dir": str(out)},
        required_artifacts=["tests/fixtures/write_metrics.py"],
    )
    result = collect(job_dir)
    assert result["status"] == "succeeded"
    assert (job_dir / "final_result.yaml").exists()
    assert (job_dir / "collect_report.yaml").exists()
    assert (job_dir / "_worker_out" / "result.yaml").exists()
    worker = yaml.safe_load((job_dir / "_worker_out" / "result.yaml").read_text())
    assert worker["status"] == "success"
    assert worker["execution_status"] == "succeeded"
    assert result["artifact_manifest"]
    verify_collection_seal(job_dir, result)


def test_collection_seal_rejects_artifact_changed_after_collect(tmp_path):
    result = _collect_fake_metric_job(
        tmp_path,
        {"train_iou": 0.5, "train_loss_final": 0.2},
    )
    job_dir = next((tmp_path / "executions").iterdir())
    (tmp_path / "stub.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_collection_seal(job_dir, result)


def test_collector_verifies_required_token_guard_receipt(tmp_path):
    out = tmp_path / "out"
    out.mkdir(parents=True)
    stub = tmp_path / "stub.txt"
    stub.write_text("ok", encoding="utf-8")
    run_dir = tmp_path / ".token-guard" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    stdout = b"metric=ok\n"
    stderr = b""
    (run_dir / "stdout.log").write_bytes(stdout)
    (run_dir / "stderr.log").write_bytes(stderr)
    receipt = {
        "run_id": "r1",
        "command_sha256": "command-hash",
        "exit_code": 0,
        "timed_out": False,
        "streams": {
            "stdout": {
                "path": ".token-guard/runs/r1/stdout.log",
                "bytes": len(stdout),
                "sha256": hashlib.sha256(stdout).hexdigest(),
            },
            "stderr": {
                "path": ".token-guard/runs/r1/stderr.log",
                "bytes": 0,
                "sha256": hashlib.sha256(stderr).hexdigest(),
            },
        },
    }
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    spec_path = _make_spec(
        tmp_path,
        "test_write_metrics",
        {"output_dir": str(out)},
        required_artifacts=["stub.txt"],
        workspace_root=tmp_path,
        evidence={
            "token_guard_receipt_required": True,
            "token_guard_receipt": ".token-guard/runs/r1/receipt.json",
        },
    )
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id
    state = read_json(job_dir / "state.json")
    state.update(status="exited", exit_code=0, finished_at="2026-05-04T00:00:00+00:00")
    atomic_write_json(job_dir / "state.json", state)

    result = collect(job_dir)

    assert result["status"] == "succeeded"
    assert result["token_guard_receipt"]["verified"] is True
    assert result["token_guard_receipt_errors"] == []


def test_collector_fails_closed_for_tampered_required_receipt(tmp_path):
    out = tmp_path / "out"
    out.mkdir(parents=True)
    stub = tmp_path / "stub.txt"
    stub.write_text("ok", encoding="utf-8")
    run_dir = tmp_path / ".token-guard" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "stdout.log").write_text("changed", encoding="utf-8")
    (run_dir / "stderr.log").write_bytes(b"")
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "command_sha256": "command-hash",
                "exit_code": 0,
                "timed_out": False,
                "streams": {
                    "stdout": {"path": ".token-guard/runs/r1/stdout.log", "bytes": 3, "sha256": hashlib.sha256(b"old").hexdigest()},
                    "stderr": {"path": ".token-guard/runs/r1/stderr.log", "bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()},
                },
            }
        ),
        encoding="utf-8",
    )
    spec_path = _make_spec(
        tmp_path,
        "test_write_metrics",
        {"output_dir": str(out)},
        required_artifacts=["stub.txt"],
        workspace_root=tmp_path,
        evidence={
            "token_guard_receipt_required": True,
            "token_guard_receipt": ".token-guard/runs/r1/receipt.json",
        },
    )
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id
    state = read_json(job_dir / "state.json")
    state.update(status="exited", exit_code=0, finished_at="2026-05-04T00:00:00+00:00")
    atomic_write_json(job_dir / "state.json", state)

    result = collect(job_dir)

    assert result["status"] == "missing_evidence"
    assert result["failure_type"] == "missing_evidence"
    assert "stream verification failed: stdout" in result["token_guard_receipt_errors"][0]


# ── 2. failed_exit_code ───────────────────────────────────────────────────────

def test_collector_failed_exit_code(tmp_path):
    out = tmp_path / "out"
    job_dir = _run_and_wait(
        tmp_path, "test_write_metrics",
        {"output_dir": str(out), "fail": True},
        required_artifacts=["tests/fixtures/write_metrics.py"],
    )
    result = collect(job_dir)
    assert result["status"] == "failed_exit_code"
    assert result["exit_code"] != 0
    assert result["failure_type"] == "execution_failed_exit_code"
    assert result["recommended_action"]["type"] == "escalate_to_codex_debug"


# ── 3. artifact_missing ───────────────────────────────────────────────────────

def test_collector_artifact_missing(tmp_path):
    out = tmp_path / "out"
    job_dir = _run_and_wait(
        tmp_path, "test_write_metrics", {"output_dir": str(out)},
        required_artifacts=["this/file/does/not/exist.json"],
    )
    result = collect(job_dir)
    assert result["status"] == "artifact_missing"
    assert result["failure_type"] == "artifact_missing"
    assert "this/file/does/not/exist.json" in result["artifacts_missing"]


# ── 4. metrics_missing ────────────────────────────────────────────────────────

def test_collector_enforces_registry_closure_contract(tmp_path):
    out = tmp_path / "out"
    out.mkdir(parents=True)
    stub = tmp_path / "stub.txt"
    stub.write_text("ok", encoding="utf-8")
    spec_path = _make_spec(
        tmp_path,
        "test_write_metrics",
        {"output_dir": str(out)},
        required_artifacts=["stub.txt"],
        workspace_root=tmp_path,
    )
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _registry_with_closure_contract(tmp_path), executions_dir)
    job_dir = executions_dir / job_id
    state = read_json(job_dir / "state.json")
    state.update(status="exited", exit_code=0, finished_at="2026-05-04T00:00:00+00:00")
    atomic_write_json(job_dir / "state.json", state)

    result = collect(job_dir)

    assert result["status"] == "artifact_missing"
    assert str(out / "artifact_summary.md") in result["artifacts_missing"]
    assert str(out / "action_requests.json") in result["artifacts_missing"]


def test_execution_wrapper_writes_minimal_closure_package(tmp_path):
    out = tmp_path / "out"
    job_dir = _run_and_wait(
        tmp_path,
        "test_write_metrics",
        {"output_dir": str(out)},
        required_artifacts=["tests/fixtures/write_metrics.py"],
    )

    assert (out / "worker_notes.md").exists()
    assert (out / "artifact_summary.md").exists()
    assert (out / "action_requests.json").exists()
    assert read_json(job_dir / "state.json")["status"] == "exited"


def test_collector_metrics_missing(tmp_path):
    # Fake execution: exit 0, metrics.json written with {auc, loss}, ask for absent key.
    # Using tmp_path as workspace_root avoids path-outside-root issues with metrics_file.
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "metrics.json").write_text(
        json.dumps({"auc": 0.91, "loss": 0.23}), encoding="utf-8"
    )
    stub = tmp_path / "stub.txt"
    stub.write_text("ok", encoding="utf-8")

    spec_path = _make_spec(
        tmp_path, "test_write_metrics", {"output_dir": str(out)},
        required_artifacts=["stub.txt"],
        metrics_file=str(Path("out") / "metrics.json"),
        metric_keys=["nonexistent_metric"],
        workspace_root=tmp_path,
    )
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id

    state = read_json(job_dir / "state.json")
    state.update(status="exited", exit_code=0,
                 finished_at="2026-05-04T00:00:00+00:00")
    atomic_write_json(job_dir / "state.json", state)

    result = collect(job_dir)
    assert result["status"] == "metrics_missing"
    assert result["failure_type"] == "metrics_missing"
    assert "nonexistent_metric" in result["metrics_missing"]


def test_collector_treats_null_required_metric_as_missing(tmp_path):
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "metrics.json").write_text(
        json.dumps({"train_iou": 0.0693, "train_loss_final": None}),
        encoding="utf-8",
    )
    stub = tmp_path / "stub.txt"
    stub.write_text("ok", encoding="utf-8")

    spec_path = _make_spec(
        tmp_path,
        "test_write_metrics",
        {"output_dir": str(out)},
        required_artifacts=["stub.txt"],
        metrics_file=str(Path("out") / "metrics.json"),
        metric_keys=["train_iou", "train_loss_final"],
        workspace_root=tmp_path,
    )
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id

    state = read_json(job_dir / "state.json")
    state.update(status="exited", exit_code=0,
                 finished_at="2026-05-04T00:00:00+00:00")
    atomic_write_json(job_dir / "state.json", state)

    result = collect(job_dir)

    assert result["status"] == "metrics_missing"
    assert result["failure_type"] == "metrics_missing"
    assert result["metrics_summary"] == {"train_iou": 0.0693}
    assert "train_loss_final" in result["metrics_missing"]


def test_collector_treats_empty_required_metric_as_missing(tmp_path):
    result = _collect_fake_metric_job(
        tmp_path,
        {"train_iou": 0.0693, "train_loss_final": ""},
        registry_path=_registry_with_metric_contract(tmp_path),
    )

    assert result["status"] == "metrics_missing"
    assert "train_loss_final" in result["metrics_missing"]
    assert result["judge"]["failure_type"] == "metrics_missing"


def test_collector_rejects_nan_inf_and_nonnumeric_required_metric(tmp_path):
    for value in (float("nan"), float("inf"), float("-inf"), "not-a-number"):
        run_tmp = tmp_path / str(value).replace("-", "neg").replace(".", "_")
        run_tmp.mkdir()
        result = _collect_fake_metric_job(
            run_tmp,
            {"train_iou": value, "train_loss_final": 1.0},
            registry_path=_registry_with_metric_contract(run_tmp),
        )

        assert result["status"] == "metrics_invalid"
        assert result["failure_type"] == "metrics_invalid"
        assert "train_iou" in result["metrics_invalid"]
        assert result["judge"]["failure_type"] == "metrics_invalid"


def test_collector_treats_weak_finite_metric_as_warning_not_failure(tmp_path):
    result = _collect_fake_metric_job(
        tmp_path,
        {"train_iou": 0.009, "train_loss_final": 9.368},
        metric_keys=[],
        registry_path=_registry_with_metric_contract(tmp_path),
    )

    assert result["status"] == "succeeded"
    assert result["failure_type"] is None
    assert result["warning_type"] == "weak_metric_result"
    assert result["metrics_summary"]["train_iou"] == 0.009
    assert result["judge"]["outcome"] == "completed_with_warning"
    assert result["judge"]["warning_type"] == "weak_metric_result"


def test_collector_routes_script_emitted_missing_evidence_to_partial(tmp_path):
    result = _collect_fake_metric_job(
        tmp_path,
        {
            "train_iou": 0.18,
            "train_loss_final": 5.792,
            "contract_status": "incomplete",
            "science_status": "not_proven",
            "failure_taxonomy": "missing_evidence",
        },
        metric_keys=[],
        registry_path=_registry_with_metric_contract(tmp_path),
    )

    assert result["status"] == "missing_evidence"
    assert result["failure_type"] == "missing_evidence"
    assert result["judge"]["outcome"] == "partial"
    assert result["judge"]["execution_success"] is True
    assert result["recommended_action"]["type"] == "suggest_followup_probe"


def test_collector_downgrades_proxy_model_fallback_to_warning(tmp_path):
    result = _collect_fake_metric_job(
        tmp_path,
        {
            "train_iou": 0.18,
            "train_loss_final": 5.792,
            "model_execution_evidence": {
                "requested_model": "GenD_DINOv3_L",
                "executed_model": "GenD_CLIP_L_14",
                "fallback_reason": "Remote transformers lacks DINOv3 classes.",
                "proxy_only": True,
                "formal_evidence": False,
                "evidence_scope": "proxy_probe",
                "allowed_claims": ["bounded proxy probe completed"],
                "forbidden_claims": ["DINOv3 formally validated the feature leakage hypothesis"],
            },
        },
        metric_keys=[],
        registry_path=_registry_with_metric_contract(tmp_path),
    )

    assert result["status"] == "succeeded"
    assert result["failure_type"] is None
    assert result["warning_type"] == "proxy_model_fallback"
    assert result["judge"]["outcome"] == "completed_with_warning"
    assert result["judge"]["execution_success"] is True
    assert result["model_execution_evidence"]["requested_model"] == "GenD_DINOv3_L"
    assert result["model_execution_evidence"]["executed_model"] == "GenD_CLIP_L_14"
    assert result["model_execution_evidence"]["formal_evidence"] is False


def test_collector_rejects_inconsistent_model_execution_evidence(tmp_path):
    result = _collect_fake_metric_job(
        tmp_path,
        {
            "train_iou": 0.18,
            "train_loss_final": 5.792,
            "model_execution_evidence": {
                "requested_model": "GenD_DINOv3_L",
                "executed_model": "GenD_CLIP_L_14",
                "proxy_only": False,
                "formal_evidence": True,
                "evidence_scope": "formal",
            },
        },
        metric_keys=[],
        registry_path=_registry_with_metric_contract(tmp_path),
    )

    assert result["status"] == "invalid_model_execution_evidence"
    assert result["failure_type"] == "invalid_model_execution_evidence"
    assert result["judge"]["outcome"] == "partial"
    assert result["judge"]["execution_success"] is True
    assert any("requested_model != executed_model" in item for item in result["model_execution_evidence_errors"])


# ── 5. _worker_out/result.yaml only written by collector ─────────────────────

def test_collector_routes_script_emitted_missing_capability_to_partial(tmp_path):
    result = _collect_fake_metric_job(
        tmp_path,
        {
            "train_iou": 0.18,
            "train_loss_final": 5.792,
            "failure_taxonomy": "missing_capability",
        },
        metric_keys=[],
        registry_path=_registry_with_metric_contract(tmp_path),
    )

    assert result["status"] == "missing_capability"
    assert result["failure_type"] == "missing_capability"
    assert result["judge"]["outcome"] == "partial"
    assert result["judge"]["execution_success"] is True
    assert result["recommended_action"]["type"] == "create_action_request"


def test_worker_result_not_written_before_collect(tmp_path):
    out = tmp_path / "out"
    job_dir = _run_and_wait(
        tmp_path, "test_write_metrics", {"output_dir": str(out)},
    )
    # After runner + monitor — before collect — _worker_out/result.yaml must not exist
    assert not (job_dir / "_worker_out" / "result.yaml").exists()
    collect(job_dir)
    assert (job_dir / "_worker_out" / "result.yaml").exists()


# ── 6. terminal-as-is: timeout preserves status ──────────────────────────────

def test_collector_timeout_status_preserved(tmp_path):
    out = tmp_path / "out"
    spec_path = _make_spec(tmp_path, "test_write_metrics", {"output_dir": str(out)})
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id

    # Fake a timeout state
    state = read_json(job_dir / "state.json")
    state.update(status="timeout", exit_code=-1,
                 finished_at="2026-05-04T00:00:00+00:00")
    atomic_write_json(job_dir / "state.json", state)

    result = collect(job_dir)
    assert result["status"] == "timeout"


# ── 7. reject non-collectable state ──────────────────────────────────────────

def test_collector_rejects_running_state(tmp_path):
    out = tmp_path / "out"
    spec_path = _make_spec(tmp_path, "test_write_metrics", {"output_dir": str(out)})
    executions_dir = tmp_path / "executions"
    job_id = register_execution(spec_path, _TEST_REGISTRY, executions_dir)
    job_dir = executions_dir / job_id

    with pytest.raises(RuntimeError, match="not collectable"):
        collect(job_dir)   # state is still "registered"
