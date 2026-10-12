from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml
from solution_agent.continuation.eval_runner import (
    run_fixture_contract_eval,
    run_and_write_fixture_contract_eval_report,
    summarize_fixture_contract_eval,
    write_fixture_contract_eval_report,
)


def test_continuation_fixture_contract_eval_runner_passes_current_fixtures():
    report = run_fixture_contract_eval(Path(".solution/continuation"))

    assert report["status"] == "failed"
    assert report["case_count"] == 11
    assert report["generated_at"].endswith("Z")
    assert {item["case_id"] for item in report["results"]} == {
        "weak_metric_success",
        "missing_artifact",
        "source_code_required",
        "no_workplace_profile",
        "prompt_injection_artifact",
        "prompt_injection_draft",
        "cross_line_leak_risk",
        "repeated_proposal_idempotency",
        "cross_workplace_cache_path_leak",
        "cross_workplace_draft_leakage",
        "missing_capability_request",
    }
    failed = {item["case_id"]: item["failures"] for item in report["results"] if item["status"] == "failed"}
    assert failed.keys() == {"prompt_injection_draft", "cross_workplace_draft_leakage"}
    assert any("simulated output contains forbidden phrases" in failure for failure in failed["prompt_injection_draft"])
    assert any("simulated output contains forbidden phrases" in failure for failure in failed["cross_workplace_draft_leakage"])
    assert report["summary"] == {
        "passed_count": 9,
        "failed_count": 2,
        "failed_cases": ["cross_workplace_draft_leakage", "prompt_injection_draft"],
        "failure_counts_by_rule": {"simulated output contains forbidden phrases": 2},
    }


def test_continuation_fixture_runner_flags_forbidden_simulated_output(tmp_path):
    source_root = Path(".solution/continuation")
    eval_root = tmp_path / "continuation"
    shutil.copytree(source_root / "evals", eval_root / "evals")

    expected = eval_root / "evals" / "expected" / "missing_artifact.yaml"
    expected.write_text(
        expected.read_text(encoding="utf-8")
        + '\nsimulated_output_text: "This draft contains /ssd6/guessed/cache/path and should fail."\n',
        encoding="utf-8",
    )

    report = run_fixture_contract_eval(eval_root)
    missing = next(item for item in report["results"] if item["case_id"] == "missing_artifact")

    assert report["status"] == "failed"
    assert missing["status"] == "failed"
    assert any("simulated output contains forbidden phrases" in failure for failure in missing["failures"])
    assert set(report["summary"]["failed_cases"]) == {
        "missing_artifact",
        "prompt_injection_draft",
        "cross_workplace_draft_leakage",
    }
    assert report["summary"]["failure_counts_by_rule"]["simulated output contains forbidden phrases"] >= 2


def test_continuation_fixture_runner_summary_groups_failure_rules():
    summary = summarize_fixture_contract_eval(
        [
            {"case_id": "a", "status": "failed", "failures": ["rule_one: detail", "rule_two"]},
            {"case_id": "b", "status": "passed", "failures": []},
            {"case_id": "c", "status": "failed", "failures": ["rule_one: other"]},
        ]
    )

    assert summary == {
        "passed_count": 1,
        "failed_count": 2,
        "failed_cases": ["a", "c"],
        "failure_counts_by_rule": {"rule_one": 2, "rule_two": 1},
    }


def test_continuation_fixture_runner_writes_local_json_report(tmp_path):
    report = run_fixture_contract_eval(Path(".solution/continuation"))
    output_path = tmp_path / "continuation" / "reports" / "fixture_eval_report.json"

    result = write_fixture_contract_eval_report(report, output_path)

    assert result == {
        "written": True,
        "path": str(output_path),
        "status": "failed",
        "case_count": 11,
    }
    assert output_path.exists()

    payload_text = output_path.read_text(encoding="utf-8")
    assert '"summary"' in payload_text
    report_payload = json.loads(payload_text)
    assert "case_count" in report_payload
    assert "summary" in report_payload
    assert "failed_cases" in report_payload["summary"]
    assert report_payload["generated_at"].endswith("Z")


def test_continuation_fixture_runner_writes_default_reports_path(tmp_path):
    eval_root = tmp_path / "continuation"
    shutil.copytree(Path(".solution/continuation/evals"), eval_root / "evals")

    result = run_and_write_fixture_contract_eval_report(eval_root)
    default_report = eval_root / "reports" / "fixture_contract_eval.json"

    assert result["report_written"] is True
    assert result["status"] == "failed"
    assert result["case_count"] == 11
    assert result["report_path"] == str(default_report)
    assert result["generated_at"].endswith("Z")
    assert default_report.exists()
    assert default_report.read_text(encoding="utf-8").startswith("{")


def test_continuation_fixture_runner_detects_requires_no_safe_mismatch(tmp_path):
    eval_root = tmp_path / "continuation"
    shutil.copytree(Path(".solution/continuation/evals"), eval_root / "evals")
    expected = eval_root / "evals" / "expected" / "weak_metric_success.yaml"
    expected_payload = yaml.safe_load(expected.read_text(encoding="utf-8"))
    expected_payload["requires_no_safe_continuation"] = True
    expected.write_text(yaml.safe_dump(expected_payload, sort_keys=False), encoding="utf-8")

    report = run_fixture_contract_eval(eval_root)
    weak = next(item for item in report["results"] if item["case_id"] == "weak_metric_success")

    assert report["status"] == "failed"
    assert weak["status"] == "failed"
    assert any("requires_no_safe_continuation" in failure for failure in weak["failures"])


def test_continuation_fixture_runner_flags_overlapping_expected_and_forbidden_next_steps(tmp_path):
    eval_root = tmp_path / "continuation"
    shutil.copytree(Path(".solution/continuation/evals"), eval_root / "evals")
    expected = eval_root / "evals" / "expected" / "missing_artifact.yaml"
    expected_payload = yaml.safe_load(expected.read_text(encoding="utf-8"))
    expected_payload["expected_next_step_kinds"].append("inventory_audit")
    expected_payload["forbidden_next_step_kinds"].append("inventory_audit")
    expected.write_text(yaml.safe_dump(expected_payload, sort_keys=False), encoding="utf-8")

    report = run_fixture_contract_eval(eval_root)
    case_result = next(item for item in report["results"] if item["case_id"] == "missing_artifact")

    assert report["status"] == "failed"
    assert case_result["status"] == "failed"
    assert any("expected and forbidden next-step kinds overlap" in failure for failure in case_result["failures"])


def test_continuation_fixture_runner_detects_expected_case_expected_mismatch(tmp_path):
    eval_root = tmp_path / "continuation"
    shutil.copytree(Path(".solution/continuation/evals"), eval_root / "evals")

    (eval_root / "evals" / "expected" / "weak_metric_success.yaml").unlink()
    extra_expected = eval_root / "evals" / "expected" / "orphaned_expected_case.yaml"
    extra_expected.write_text(
        "---\ncase_id: orphaned_expected_case\nexpected_category: unknown\nexpected_next_step_kinds: []\nforbidden_next_step_kinds: []\nrequires_no_safe_continuation: false\n",
        encoding="utf-8",
    )

    report = run_fixture_contract_eval(eval_root)

    weak_metric = next(item for item in report["results"] if item["case_id"] == "weak_metric_success")
    orphan = next(item for item in report["results"] if item["case_id"] == "orphaned_expected_case")

    assert report["status"] == "failed"
    assert weak_metric["status"] == "failed"
    assert any("expected fixture missing" in failure for failure in weak_metric["failures"])
    assert orphan["status"] == "failed"
    assert any("expected case file missing for expected fixture" in failure for failure in orphan["failures"])
    assert "weak_metric_success" in report["summary"]["failed_cases"]
