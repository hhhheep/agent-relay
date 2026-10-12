from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "skills" / "research-token-guard" / "scripts" / "codex_usage.py"


def write_session(path: Path, thread_id: str, model: str, input_tokens: int, cached: int) -> None:
    events = [
        {"ordinal": 0, "type": "session_meta", "payload": {"session_id": thread_id}},
        {
            "ordinal": 1,
            "type": "turn_context",
            "payload": {"turn_id": f"turn-{thread_id}", "model": model, "effort": "high"},
        },
        {
            "ordinal": 2,
            "type": "token_usage_record",
            "payload": {
                "thread_id": thread_id,
                "turn_id": f"turn-{thread_id}",
                "response_id": f"response-{thread_id}",
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                    "total_tokens": input_tokens + 20,
                },
                "thread_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                    "total_tokens": input_tokens + 20,
                },
            },
        },
    ]
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


class CodexUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], text=True, capture_output=True, check=False
        )

    def test_summary_separates_new_and_cached_input(self) -> None:
        session = self.root / "session.jsonl"
        write_session(session, "thread-a", "gpt-6-luna", 1000, 800)
        result = self.call("summarize", "--session", str(session))
        self.assertEqual(result.returncode, 0, result.stdout)
        data = json.loads(result.stdout)
        self.assertEqual(data["usage"]["input_tokens"], 1000)
        self.assertEqual(data["usage"]["cached_input_tokens"], 800)
        self.assertEqual(data["usage"]["new_input_tokens"], 200)

    def test_assemble_requires_fresh_threads_and_same_model(self) -> None:
        baseline = self.root / "baseline.jsonl"
        guarded = self.root / "guarded.jsonl"
        write_session(baseline, "thread-a", "gpt-6-luna", 1000, 200)
        write_session(guarded, "thread-b", "gpt-6-luna", 600, 200)
        manifest = self.root / "pairs.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "research-token-guard/ab-session-pairs/v1",
                    "model": "gpt-6-luna",
                    "reasoning_effort": "high",
                    "cases": {
                        "case-1": {
                            "baseline_session": baseline.name,
                            "guarded_session": guarded.name,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.call("assemble", "--manifest", str(manifest))
        self.assertEqual(result.returncode, 0, result.stdout)
        data = json.loads(result.stdout)
        self.assertEqual(data["cases"]["case-1"]["baseline"]["new_input_tokens"], 800)
        self.assertEqual(data["cases"]["case-1"]["guarded"]["new_input_tokens"], 400)

    def test_assemble_rejects_same_thread(self) -> None:
        baseline = self.root / "baseline.jsonl"
        guarded = self.root / "guarded.jsonl"
        write_session(baseline, "thread-a", "gpt-6-luna", 1000, 200)
        write_session(guarded, "thread-a", "gpt-6-luna", 600, 200)
        manifest = self.root / "pairs.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "research-token-guard/ab-session-pairs/v1",
                    "model": "gpt-6-luna",
                    "cases": {
                        "case-1": {
                            "baseline_session": baseline.name,
                            "guarded_session": guarded.name,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.call("assemble", "--manifest", str(manifest))
        self.assertEqual(result.returncode, 2)
        self.assertIn("fresh, different threads", result.stdout)

    def test_evaluate_requires_exact_answer_and_provider_token_savings(self) -> None:
        baseline_session = self.root / "baseline.jsonl"
        guarded_session = self.root / "guarded.jsonl"
        write_session(baseline_session, "thread-a", "gpt-6-luna", 1000, 200)
        write_session(guarded_session, "thread-b", "gpt-6-luna", 600, 200)
        baseline_summary = self.root / "baseline-usage.json"
        guarded_summary = self.root / "guarded-usage.json"
        for session, output in (
            (baseline_session, baseline_summary),
            (guarded_session, guarded_summary),
        ):
            summarized = self.call(
                "summarize", "--session", str(session), "--output", str(output)
            )
            self.assertEqual(summarized.returncode, 0, summarized.stdout)
        expected = self.root / "expected.json"
        baseline_answer = self.root / "baseline-answer.json"
        guarded_answer = self.root / "guarded-answer.json"
        answer = {"decision": "stop", "score": 0.7}
        for path in (expected, baseline_answer, guarded_answer):
            path.write_text(json.dumps(answer), encoding="utf-8")
        result = self.call(
            "evaluate",
            "--case-id",
            "case-1",
            "--model",
            "gpt-6-luna",
            "--reasoning-effort",
            "high",
            "--expected-answer",
            str(expected),
            "--baseline-answer",
            str(baseline_answer),
            "--guarded-answer",
            str(guarded_answer),
            "--baseline-usage",
            str(baseline_summary),
            "--guarded-usage",
            str(guarded_summary),
            "--min-new-input-reduction",
            "0.4",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        data = json.loads(result.stdout)
        self.assertTrue(data["correctness"]["passed"])
        self.assertEqual(data["usage"]["new_input_tokens_saved"], 400)
        self.assertEqual(data["usage"]["new_input_reduction_percent"], 50.0)
        self.assertTrue(data["acceptance"]["passed"])

        guarded_answer.write_text(
            json.dumps({"decision": "go", "score": 0.7}), encoding="utf-8"
        )
        failed = self.call(
            "evaluate",
            "--case-id",
            "case-1",
            "--model",
            "gpt-6-luna",
            "--reasoning-effort",
            "high",
            "--expected-answer",
            str(expected),
            "--baseline-answer",
            str(baseline_answer),
            "--guarded-answer",
            str(guarded_answer),
            "--baseline-usage",
            str(baseline_summary),
            "--guarded-usage",
            str(guarded_summary),
        )
        self.assertEqual(failed.returncode, 1, failed.stdout)
        self.assertEqual(json.loads(failed.stdout)["correctness"]["guarded_differing_fields"], ["decision"])


if __name__ == "__main__":
    unittest.main()
