from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    PLUGIN_ROOT / "skills" / "research-token-guard" / "scripts" / "stage_ledger.py"
)
TOKEN_GUARD = (
    PLUGIN_ROOT / "skills" / "research-token-guard" / "scripts" / "token_guard.py"
)


class StageLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.stage = self.root / "stage.json"
        self.db = self.root / ".token-guard" / "stage.sqlite3"
        self.input = self.root / "input.txt"
        self.input.write_text("alpha\n", encoding="utf-8")
        self.stage.write_text(
            json.dumps(
                {
                    "schema_version": "research-stage-spec/v1",
                    "task_id": "task-test",
                    "stage_id": "stage-test",
                    "objective": "Test verified reuse.",
                    "acceptance_checks": ["Output is exact."],
                    "budgets": {
                        "max_command_executions": 4,
                        "max_model_facing_bytes": 100000,
                        "max_retries_per_fingerprint": 1,
                        "max_wall_seconds": 3600,
                    },
                }
            ),
            encoding="utf-8",
        )
        initialized = self.call(
            "init",
            "--root",
            str(self.root),
            "--stage",
            str(self.stage),
            "--db",
            str(self.db),
        )
        self.assertEqual(initialized.returncode, 0, initialized.stdout)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def run_echo(self, marker: str = "result") -> subprocess.CompletedProcess[str]:
        return self.call(
            "run",
            "--root",
            str(self.root),
            "--db",
            str(self.db),
            "--token-guard",
            str(TOKEN_GUARD),
            "--input",
            str(self.input),
            "--",
            sys.executable,
            "-c",
            f"print('{marker}')",
        )

    def test_identical_verified_run_is_reused_without_execution(self) -> None:
        first = self.run_echo()
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertFalse(json.loads(first.stdout)["cache_hit"])
        second = self.run_echo()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertTrue(json.loads(second.stdout)["cache_hit"])
        status = self.call("status", "--root", str(self.root), "--db", str(self.db))
        data = json.loads(status.stdout)
        self.assertEqual(data["budget"]["command_executions"], 1)
        self.assertEqual(data["event_counts"]["cache_hit"], 1)

    def test_input_mutation_invalidates_cache(self) -> None:
        first = self.run_echo()
        self.assertEqual(first.returncode, 0)
        self.input.write_text("alphb\n", encoding="utf-8")
        second = self.run_echo()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertFalse(json.loads(second.stdout)["cache_hit"])
        self.assertNotEqual(
            json.loads(first.stdout)["fingerprint"], json.loads(second.stdout)["fingerprint"]
        )

    def test_tampered_raw_output_disables_cache_hit(self) -> None:
        first = self.run_echo()
        data = json.loads(first.stdout)
        receipt = data["receipt"]
        raw = self.root / receipt["streams"]["stdout"]["path"]
        raw.write_text("tampered\n", encoding="utf-8")
        second = self.run_echo()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertFalse(json.loads(second.stdout)["cache_hit"])

    def test_command_budget_moves_stage_to_review(self) -> None:
        spec = json.loads(self.stage.read_text(encoding="utf-8"))
        spec["budgets"]["max_command_executions"] = 1
        self.stage.write_text(json.dumps(spec), encoding="utf-8")
        other_db = self.root / ".token-guard" / "limited.sqlite3"
        initialized = self.call(
            "init",
            "--root",
            str(self.root),
            "--stage",
            str(self.stage),
            "--db",
            str(other_db),
        )
        self.assertEqual(initialized.returncode, 0)
        original_db = self.db
        self.db = other_db
        try:
            self.assertEqual(self.run_echo("one").returncode, 0)
            denied = self.run_echo("two")
            self.assertEqual(denied.returncode, 2)
            self.assertIn("budget exhausted", denied.stdout)
            status = self.call("status", "--root", str(self.root), "--db", str(self.db))
            self.assertEqual(json.loads(status.stdout)["state"], "needs_review")
        finally:
            self.db = original_db

    def test_ledger_chain_detects_payload_tampering(self) -> None:
        self.assertEqual(self.run_echo().returncode, 0)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE events SET payload_json='{}' WHERE seq=1")
            conn.commit()
        finally:
            conn.close()
        verified = self.call("verify", "--root", str(self.root), "--db", str(self.db))
        self.assertEqual(verified.returncode, 2)
        self.assertFalse(json.loads(verified.stdout)["valid"])

    def test_secret_argument_is_not_persisted_in_ledger(self) -> None:
        secret = "sk-" + "fake-ledger-secret-123456789"
        result = self.call(
            "run",
            "--root",
            str(self.root),
            "--db",
            str(self.db),
            "--token-guard",
            str(TOKEN_GUARD),
            "--",
            sys.executable,
            "-c",
            "print('ok')",
            "--api-key",
            secret,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(secret, result.stdout)
        conn = sqlite3.connect(self.db)
        try:
            stored = "\n".join(row[0] for row in conn.execute("SELECT payload_json FROM events"))
        finally:
            conn.close()
        self.assertNotIn(secret, stored)
        self.assertIn("<redacted>", stored)

    def test_model_facing_budget_records_final_return_size(self) -> None:
        result = self.run_echo()
        self.assertEqual(result.returncode, 0, result.stdout)
        conn = sqlite3.connect(self.db)
        try:
            recorded = conn.execute(
                "SELECT model_facing_bytes FROM events WHERE event_type='run_completed'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(recorded, len(result.stdout.encode("utf-8")))

    def test_status_reports_review_state_when_its_view_crosses_budget(self) -> None:
        spec = json.loads(self.stage.read_text(encoding="utf-8"))
        spec["budgets"]["max_model_facing_bytes"] = 1
        limited_stage = self.root / "limited-stage.json"
        limited_db = self.root / ".token-guard" / "status-limited.sqlite3"
        limited_stage.write_text(json.dumps(spec), encoding="utf-8")
        initialized = self.call(
            "init",
            "--root",
            str(self.root),
            "--stage",
            str(limited_stage),
            "--db",
            str(limited_db),
        )
        self.assertEqual(initialized.returncode, 0, initialized.stdout)
        status = self.call(
            "status", "--root", str(self.root), "--db", str(limited_db)
        )
        self.assertEqual(status.returncode, 0, status.stdout)
        data = json.loads(status.stdout)
        self.assertEqual(data["state"], "needs_review")
        self.assertEqual(data["budget_exceeded"], "max_model_facing_bytes")

    def test_evidence_view_is_exact_and_recorded(self) -> None:
        first = self.run_echo("AUC=0.731 baseline=0.744")
        receipt = json.loads(first.stdout)["receipt"]
        evidence = self.call(
            "evidence",
            "--root",
            str(self.root),
            "--db",
            str(self.db),
            "--token-guard",
            str(TOKEN_GUARD),
            "--receipt",
            receipt["recovery"]["receipt"],
            "--query",
            "AUC=0.731",
        )
        self.assertEqual(evidence.returncode, 0, evidence.stdout)
        data = json.loads(evidence.stdout)
        self.assertIn("baseline=0.744", data["excerpt"]["content"])
        status = self.call("status", "--root", str(self.root), "--db", str(self.db))
        self.assertGreaterEqual(json.loads(status.stdout)["event_counts"]["view_emitted"], 1)

    def test_change_view_is_bounded_index_not_raw_payload(self) -> None:
        self.assertEqual(self.run_echo().returncode, 0)
        change = self.call(
            "change",
            "--root",
            str(self.root),
            "--db",
            str(self.db),
            "--since-seq",
            "0",
            "--max-events",
            "2",
        )
        self.assertEqual(change.returncode, 0, change.stdout)
        data = json.loads(change.stdout)
        self.assertEqual(len(data["events"]), 2)
        self.assertTrue(data["truncated"])
        self.assertNotIn("fingerprint_payload", change.stdout)

    def test_close_and_handoff_preserve_evidence_hash(self) -> None:
        first = self.run_echo()
        receipt = json.loads(first.stdout)["receipt"]
        evidence_path = receipt["streams"]["stdout"]["path"]
        closed = self.call(
            "close",
            "--root",
            str(self.root),
            "--db",
            str(self.db),
            "--result",
            "stop",
            "--rationale",
            "The candidate did not beat the baseline.",
            "--evidence",
            evidence_path,
        )
        self.assertEqual(closed.returncode, 0, closed.stdout)
        self.assertEqual(json.loads(closed.stdout)["terminal_state"], "rejected")
        handoff = self.call("handoff", "--root", str(self.root), "--db", str(self.db))
        self.assertEqual(handoff.returncode, 0, handoff.stdout)
        data = json.loads(handoff.stdout)
        self.assertTrue(data["ledger_valid"])
        self.assertEqual(data["decision"]["result"], "stop")
        self.assertEqual(data["decision"]["evidence"][0]["path"], evidence_path)

    def test_atomic_execute_runs_and_returns_named_exact_evidence(self) -> None:
        local_guard = self.root / "token_guard.py"
        shutil.copy2(TOKEN_GUARD, local_guard)
        atomic_stage = self.root / "atomic-stage.json"
        atomic_db = self.root / ".token-guard" / "atomic.sqlite3"
        spec = json.loads(self.stage.read_text(encoding="utf-8"))
        spec["stage_id"] = "atomic-stage-test"
        spec["execution"] = {
            "token_guard": "token_guard.py",
            "cwd": ".",
            "inputs": ["input.txt"],
            "inline_bytes": 256,
            "failure_inline_bytes": 512,
            "command": [
                sys.executable,
                "-c",
                "print('noise\\nVALUE=42\\nmore noise')",
            ],
            "evidence_queries": [
                {
                    "name": "value",
                    "stream": "stdout",
                    "query": "VALUE=",
                    "value_after_query": True,
                    "max_bytes": 128,
                }
            ],
        }
        atomic_stage.write_text(json.dumps(spec), encoding="utf-8")
        result = self.call(
            "execute",
            "--root",
            str(self.root),
            "--stage",
            str(atomic_stage),
            "--db",
            str(atomic_db),
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        data = json.loads(result.stdout)
        self.assertTrue(data["verified"])
        self.assertFalse(data["cache_hit"])
        self.assertEqual(data["evidence"]["value"], "42")
        self.assertEqual(data["evidence_meta"]["value"]["match_count"], 1)

        repeated = self.call(
            "execute",
            "--root",
            str(self.root),
            "--stage",
            str(atomic_stage),
            "--db",
            str(atomic_db),
        )
        self.assertEqual(repeated.returncode, 0, repeated.stdout)
        self.assertTrue(json.loads(repeated.stdout)["cache_hit"])


if __name__ == "__main__":
    unittest.main()
