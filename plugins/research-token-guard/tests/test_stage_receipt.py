from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    PLUGIN_ROOT
    / "skills"
    / "research-token-guard"
    / "scripts"
    / "validate_stage_receipt.py"
)
TOKEN_GUARD = (
    PLUGIN_ROOT
    / "skills"
    / "research-token-guard"
    / "scripts"
    / "token_guard.py"
)


def valid_receipt() -> dict:
    return {
        "schema_version": "research-stage-receipt/v1",
        "task_id": "task-1",
        "stage_id": "stage-1",
        "status": "complete",
        "objective": "Validate one bounded result.",
        "acceptance_checks": [
            {"name": "Metric was computed", "status": "pass", "evidence": ["metric.json"]}
        ],
        "decision": {
            "gate": "baseline comparison",
            "result": "stop",
            "rationale": "Candidate did not beat baseline.",
            "evidence": ["metric.json"],
        },
        "commands": {
            "total": 2,
            "guarded": 1,
            "unguarded": 1,
            "unguarded_reasons": ["One exact version probe returned one line."],
        },
        "token_guard": {
            "run_ids": [],
            "verified_run_ids": [],
            "raw_bytes": 100,
            "returned_bytes": 20,
        },
        "artifacts": [],
        "handoff": {
            "next_action": "Close the branch.",
            "required_inputs": [],
            "do_not_assume": ["The candidate improved the baseline."],
        },
    }


class StageReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "metric.json").write_text("{}\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, data: dict, *extra: str) -> subprocess.CompletedProcess[str]:
        path = self.root / "stage_receipt.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(path), "--root", str(self.root), *extra],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_valid_receipt_passes(self) -> None:
        result = self.call(valid_receipt(), "--check-artifacts")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(json.loads(result.stdout)["valid"])

    def test_uncovered_command_count_fails(self) -> None:
        data = valid_receipt()
        data["commands"]["total"] = 3
        result = self.call(data)
        self.assertEqual(result.returncode, 2)
        self.assertIn("total must equal", result.stdout)

    def test_complete_stage_cannot_leave_pending_check(self) -> None:
        data = valid_receipt()
        data["acceptance_checks"][0]["status"] = "pending"
        result = self.call(data)
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot contain pending", result.stdout)

    def test_evidence_cannot_escape_root(self) -> None:
        data = valid_receipt()
        data["decision"]["evidence"] = ["../outside.txt"]
        result = self.call(data)
        self.assertEqual(result.returncode, 2)
        self.assertIn("escapes root", result.stdout)

    def test_real_token_guard_run_verifies_as_stage_evidence(self) -> None:
        guarded = subprocess.run(
            [
                sys.executable,
                str(TOKEN_GUARD),
                "run",
                "--root",
                str(self.root),
                "--",
                sys.executable,
                "-c",
                "print('AUC=0.731 baseline=0.744')",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(guarded.returncode, 0, guarded.stdout)
        guard_receipt = json.loads(guarded.stdout)
        run_id = guard_receipt["run_id"]
        guard_receipt_path = guard_receipt["recovery"]["receipt"]
        data = valid_receipt()
        data["acceptance_checks"][0]["evidence"] = [guard_receipt_path]
        data["decision"]["evidence"] = [guard_receipt_path]
        data["commands"] = {
            "total": 1,
            "guarded": 1,
            "unguarded": 0,
            "unguarded_reasons": [],
        }
        data["token_guard"] = {
            "run_ids": [run_id],
            "verified_run_ids": [run_id],
            "raw_bytes": guard_receipt["raw_bytes"],
            "returned_bytes": guard_receipt["returned_bytes"],
        }
        data["artifacts"] = [
            {
                "path": guard_receipt["streams"]["stdout"]["path"],
                "sha256": guard_receipt["streams"]["stdout"]["sha256"],
                "kind": "raw_stdout",
            }
        ]
        result = self.call(data, "--check-artifacts", "--verify-token-guard")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(json.loads(result.stdout)["valid"])


if __name__ == "__main__":
    unittest.main()
