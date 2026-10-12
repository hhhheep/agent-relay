"""Tests for execution_port.spec validator — 9 deny conditions + 1 valid pass."""
from __future__ import annotations

import pytest

from solution_agent.execution_port.spec import (
    ExecutionSpec,
    SpecValidationError,
    _parse,
    validate_spec,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

_REGISTRY = {
    "scripts": {
        "run_eval_probe": {
            "entrypoint": ["python", ".codex-orchestrator/runtime/scripts/probes/run_eval_probe.py"],
            "args": {
                "config": {"flag": "--config", "type": "path", "required": True},
                "output_dir": {"flag": "--output_dir", "type": "path", "required": True},
                "smoke": {"flag": "--smoke", "type": "bool", "default": False},
            },
            "max_timeout_sec": 7200,
            "gpu_allowed": True,
        },
        "run_probe_smoke": {
            "entrypoint": ["python", ".codex-orchestrator/runtime/scripts/probes/run_probe.py"],
            "args": {
                "config": {"flag": "--config", "type": "path", "required": True},
                "output_dir": {"flag": "--output_dir", "type": "path", "required": True},
            },
            "max_timeout_sec": 300,
            "gpu_allowed": False,
        },
    }
}


def _valid_data() -> dict:
    """Minimal spec that should always pass."""
    return {
        "task_id": "exp013",
        "job_type": "eval_probe",
        "workspace": {
            "root": "/srv/example-project",
            "cwd": "/srv/example-project",
        },
        "command": {
            "script_id": "run_eval_probe",
            "args": {
                "config": "configs/exp013.yaml",
                "output_dir": "results/exp013",
            },
            "timeout_sec": 3600,
        },
        "resources": {"gpu_required": True, "gpu_count": 1},
        "artifacts": {
            "required": ["results/exp013/metrics.json"],
        },
        "success_criteria": {
            "exit_code": 0,
            "metrics_file": "results/exp013/metrics.json",
            "metric_keys": ["auc"],
        },
    }


def _errors(data: dict) -> list[str]:
    spec = _parse(data)
    return validate_spec(spec, _REGISTRY)


def _assert_deny(data: dict, fragment: str) -> None:
    errs = _errors(data)
    assert errs, f"expected deny but got no errors (looking for {fragment!r})"
    assert any(fragment in e for e in errs), (
        f"expected error containing {fragment!r}, got: {errs}"
    )


# ── Condition 1: unknown script_id ────────────────────────────────────────────

def test_deny_unknown_script_id():
    d = _valid_data()
    d["command"]["script_id"] = "run_nonexistent_script"
    _assert_deny(d, "not found in script registry")


# ── Condition 2: unknown arg key ──────────────────────────────────────────────

def test_deny_unknown_arg():
    d = _valid_data()
    d["command"]["args"]["secret_flag"] = "oops"
    _assert_deny(d, "not in allowed args")


# ── Condition 3: free-form shell string as script_id ─────────────────────────

def test_deny_free_form_shell():
    d = _valid_data()
    d["command"]["script_id"] = "python scripts/run.py --config foo"
    _assert_deny(d, "looks like a shell string")


def test_deny_shell_with_semicolon():
    d = _valid_data()
    d["command"]["script_id"] = "run_eval_probe; rm -rf /"
    _assert_deny(d, "looks like a shell string")


# ── Condition 4: cwd outside workspace root ───────────────────────────────────

def test_deny_cwd_outside_root():
    d = _valid_data()
    d["workspace"]["cwd"] = "/tmp/evil"
    _assert_deny(d, "is not under workspace.root")


def test_deny_cwd_dotdot_escape():
    d = _valid_data()
    d["workspace"]["cwd"] = "/srv/example-project/../../etc"
    _assert_deny(d, "is not under workspace.root")


# ── Condition 5: artifact resolves outside root ───────────────────────────────

def test_deny_artifact_outside_root():
    d = _valid_data()
    d["artifacts"]["required"] = ["../../../etc/passwd"]
    _assert_deny(d, "resolves outside workspace.root")


def test_allow_artifact_under_declared_artifact_root():
    d = _valid_data()
    d["workspace"]["artifact_roots"] = ["/srv/example-project/outputs/lab-runs"]
    d["command"]["args"]["output_dir"] = "/srv/example-project/outputs/lab-runs/exp013"
    d["artifacts"]["required"] = ["/srv/example-project/outputs/lab-runs/exp013/metrics.json"]
    errs = _errors(d)
    assert errs == [], f"expected no errors, got: {errs}"


def test_deny_non_absolute_artifact_root():
    d = _valid_data()
    d["workspace"]["artifact_roots"] = ["outputs/lab-runs"]
    _assert_deny(d, "workspace.artifact_roots contains non-absolute path")


def test_path_roles_allow_external_input_but_protect_authority_output():
    registry = {
        "scripts": {
            "writer": {
                "entrypoint": ["python", "writer.py"],
                "args": {
                    "evidence_path": {"flag": "--evidence_path", "type": "path", "path_role": "input"},
                    "output_dir": {"flag": "--output_dir", "type": "path", "path_role": "output"},
                },
                "max_timeout_sec": 60,
                "gpu_allowed": False,
            }
        }
    }
    data = _valid_data()
    data["workspace"].update(
        input_roots=["/vault/local-files"],
        artifact_roots=["/srv/example-project/artifacts"],
        authority_roots=["/srv/example-project/research/records"],
    )
    data["command"] = {
        "script_id": "writer",
        "args": {
            "evidence_path": "/vault/local-files/evidence.json",
            "output_dir": "/srv/example-project/artifacts/draft",
        },
        "timeout_sec": 60,
    }
    data["resources"] = {"gpu_required": False, "gpu_count": 0}
    assert validate_spec(_parse(data), registry) == []

    data["command"]["args"]["output_dir"] = "/srv/example-project/research/records"
    errors = validate_spec(_parse(data), registry)
    assert any("managed authority root" in error for error in errors)


# ── Condition 6a: timeout missing / zero ─────────────────────────────────────

def test_deny_timeout_missing():
    d = _valid_data()
    d["command"]["timeout_sec"] = 0
    _assert_deny(d, "missing or <= 0")


# ── Condition 6b: timeout too large ──────────────────────────────────────────

def test_deny_timeout_too_large():
    d = _valid_data()
    d["command"]["timeout_sec"] = 99999
    _assert_deny(d, "exceeds maximum")


# ── Condition 7: required artifacts empty ────────────────────────────────────

def test_deny_empty_required_artifacts():
    d = _valid_data()
    d["artifacts"]["required"] = []
    _assert_deny(d, "artifacts.required is empty")


# ── Condition 8: gpu_required but script gpu_allowed=false ───────────────────

def test_deny_gpu_required_but_not_allowed():
    d = _valid_data()
    d["command"]["script_id"] = "run_probe_smoke"   # gpu_allowed=false
    d["command"]["args"] = {
        "config": "configs/exp013.yaml",
        "output_dir": "results/exp013",
    }
    d["resources"]["gpu_required"] = True
    _assert_deny(d, "gpu_allowed=false")


# ── Condition 9: non-absolute paths ──────────────────────────────────────────

def test_deny_non_absolute_root():
    d = _valid_data()
    d["workspace"]["root"] = "relative/path"
    _assert_deny(d, "workspace.root is not absolute")


def test_deny_non_absolute_cwd():
    d = _valid_data()
    d["workspace"]["cwd"] = "relative/cwd"
    _assert_deny(d, "workspace.cwd is not absolute")


def test_deny_missing_registry_entrypoint_when_workspace_exists(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    registry = {
        "scripts": {
            "missing_entrypoint": {
                "entrypoint": ["python", "scripts/missing.py"],
                "entrypoint_must_exist": True,
                "args": {
                    "output_dir": {"flag": "--output_dir", "type": "path", "required": True},
                },
                "max_timeout_sec": 60,
                "gpu_allowed": False,
            }
        }
    }
    d = _valid_data()
    d["workspace"]["root"] = str(root)
    d["workspace"]["cwd"] = str(root)
    d["command"]["script_id"] = "missing_entrypoint"
    d["command"]["args"] = {"output_dir": "results/missing"}
    d["resources"]["gpu_required"] = False

    errs = validate_spec(_parse(d), registry)
    assert any("does not exist" in e for e in errs), errs


# ── Valid spec passes ─────────────────────────────────────────────────────────

def test_deny_missing_must_exist_path_arg_when_workspace_exists(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    registry = {
        "scripts": {
            "needs_input_file": {
                "entrypoint": ["python", "script.py"],
                "args": {
                    "metadata_csv": {
                        "flag": "--metadata_csv",
                        "type": "path",
                        "required": True,
                        "must_exist": True,
                    },
                    "output_dir": {"flag": "--output_dir", "type": "path", "required": True},
                },
                "max_timeout_sec": 60,
                "gpu_allowed": False,
            }
        }
    }
    d = _valid_data()
    d["workspace"]["root"] = str(root)
    d["workspace"]["cwd"] = str(root)
    d["command"]["script_id"] = "needs_input_file"
    d["command"]["args"] = {
        "metadata_csv": "artifacts/missing.csv",
        "output_dir": "results/out",
    }
    d["resources"]["gpu_required"] = False

    errs = validate_spec(_parse(d), registry)
    assert any("command.args.metadata_csv path must exist" in e for e in errs), errs


def test_allow_existing_must_exist_path_arg(tmp_path):
    root = tmp_path / "repo"
    input_path = root / "artifacts" / "metadata_all.csv"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("id,label\n", encoding="utf-8")
    registry = {
        "scripts": {
            "needs_input_file": {
                "entrypoint": ["python", "script.py"],
                "args": {
                    "metadata_csv": {
                        "flag": "--metadata_csv",
                        "type": "path",
                        "required": True,
                        "must_exist": True,
                    },
                    "output_dir": {"flag": "--output_dir", "type": "path", "required": True},
                },
                "max_timeout_sec": 60,
                "gpu_allowed": False,
            }
        }
    }
    d = _valid_data()
    d["workspace"]["root"] = str(root)
    d["workspace"]["cwd"] = str(root)
    d["command"]["script_id"] = "needs_input_file"
    d["command"]["args"] = {
        "metadata_csv": str(input_path),
        "output_dir": "results/out",
    }
    d["resources"]["gpu_required"] = False

    errs = validate_spec(_parse(d), registry)
    assert errs == [], errs


def test_valid_spec_passes():
    errs = _errors(_valid_data())
    assert errs == [], f"expected no errors, got: {errs}"


def test_deny_required_token_guard_receipt_without_path():
    d = _valid_data()
    d["evidence"] = {"token_guard_receipt_required": True}
    _assert_deny(d, "evidence.token_guard_receipt is required")


def test_deny_token_guard_receipt_outside_workspace():
    d = _valid_data()
    d["evidence"] = {
        "token_guard_receipt_required": True,
        "token_guard_receipt": "../../../tmp/receipt.json",
    }
    _assert_deny(d, "evidence.token_guard_receipt resolves outside workspace.root")


def test_allow_required_token_guard_receipt_inside_workspace():
    d = _valid_data()
    d["evidence"] = {
        "token_guard_receipt_required": True,
        "token_guard_receipt": ".token-guard/runs/r1/receipt.json",
    }
    assert _errors(d) == []


def test_valid_spec_load_raises_on_invalid(tmp_path):
    from solution_agent.execution_port.spec import load_spec
    bad = _valid_data()
    bad["command"]["script_id"] = "nonexistent"
    spec_file = tmp_path / "execution_spec.yaml"
    import yaml
    spec_file.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(SpecValidationError) as exc_info:
        load_spec(spec_file, _REGISTRY)
    assert exc_info.value.errors
