from __future__ import annotations

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
    / "replay_benchmark.py"
)
TOKEN_GUARD = (
    PLUGIN_ROOT
    / "skills"
    / "research-token-guard"
    / "scripts"
    / "token_guard.py"
)
STAGE_LEDGER = (
    PLUGIN_ROOT
    / "skills"
    / "research-token-guard"
    / "scripts"
    / "stage_ledger.py"
)


class ReplayBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.cases = []
        for index in range(8):
            path = self.root / f"case-{index}.txt"
            marker = f"required-marker-{index}"
            path.write_text(("noise\n" * 1000) + marker + "\n", encoding="utf-8")
            self.cases.append(
                {
                    "id": f"case-{index}",
                    "mode": "file",
                    "path": path.name,
                    "expected_exit_code": 0,
                    "required_literals": [marker],
                }
            )
        self.manifest = self.root / "corpus.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "schema_version": "research-token-guard/replay-corpus/v1",
                    "cases": self.cases,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(
        self, usage: Path | None = None, *, with_ledger: bool = False
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCRIPT),
            "run",
            "--root",
            str(self.root),
            "--manifest",
            str(self.manifest),
            "--token-guard",
            str(TOKEN_GUARD),
            "--output",
            "result.json",
            "--inline-bytes",
            "512",
        ]
        if usage is not None:
            command.extend(["--provider-usage", str(usage)])
        if with_ledger:
            command.extend(["--stage-ledger", str(STAGE_LEDGER)])
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def test_offline_replay_proves_correctness_and_bytes_not_tokens(self) -> None:
        result = self.call()
        self.assertEqual(result.returncode, 0, result.stdout)
        data = json.loads(result.stdout)
        self.assertTrue(data["correctness_gate_passed"])
        self.assertTrue(data["byte_proxy_improved"])
        self.assertEqual(data["provider_token_gate"]["claim"], "not_measured")
        self.assertFalse(data["actual_token_savings_proven"])
        self.assertTrue(all(case["facts_recovered"] == 1 for case in data["cases"]))

    def test_provider_usage_must_pass_both_token_thresholds(self) -> None:
        usage = self.root / "usage.json"
        usage.write_text(
            json.dumps(
                {
                    "schema_version": "research-token-guard/provider-usage/v1",
                    "cases": {
                        case["id"]: {
                            "baseline": {
                                "new_input_tokens": 1000,
                                "cached_input_tokens": 100,
                            },
                            "guarded": {
                                "new_input_tokens": 500,
                                "cached_input_tokens": 80,
                            },
                        }
                        for case in self.cases
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.call(usage)
        self.assertEqual(result.returncode, 0, result.stdout)
        data = json.loads(result.stdout)
        self.assertTrue(data["provider_token_gate"]["passed"])
        self.assertTrue(data["actual_token_savings_proven"])

    def test_ledger_mode_reuses_identical_case(self) -> None:
        self.cases[-1] = {
            **self.cases[0],
            "id": "case-7",
        }
        self.manifest.write_text(
            json.dumps(
                {
                    "schema_version": "research-token-guard/replay-corpus/v1",
                    "cases": self.cases,
                }
            ),
            encoding="utf-8",
        )
        result = self.call(with_ledger=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        data = json.loads(result.stdout)
        self.assertEqual(data["guard_mode"], "stage_ledger")
        self.assertTrue(data["cases"][-1]["cache_hit"])
        self.assertEqual(data["guarded_command_executions"], 7)

    def test_provider_usage_case_set_must_match(self) -> None:
        usage = self.root / "usage.json"
        usage.write_text(
            json.dumps(
                {
                    "schema_version": "research-token-guard/provider-usage/v1",
                    "cases": {},
                }
            ),
            encoding="utf-8",
        )
        result = self.call(usage)
        self.assertEqual(result.returncode, 2)
        self.assertIn("exactly match", result.stdout)

    def test_manifest_requires_eight_cases(self) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "schema_version": "research-token-guard/replay-corpus/v1",
                    "cases": self.cases[:7],
                }
            ),
            encoding="utf-8",
        )
        result = self.call()
        self.assertEqual(result.returncode, 2)
        self.assertIn("at least eight", result.stdout)


if __name__ == "__main__":
    unittest.main()
