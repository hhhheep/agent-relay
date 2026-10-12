from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "skills" / "research-token-guard" / "scripts" / "token_guard.py"


class TokenGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def latest_receipt(self) -> Path:
        return next((self.root / ".token-guard" / "runs").glob("*/receipt.json"))

    def test_small_output_is_complete_and_hashes_verify(self) -> None:
        result = self.call(
            "run",
            "--root",
            str(self.root),
            "--",
            sys.executable,
            "-c",
            "print('small-output')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertFalse(receipt["lossy"])
        self.assertEqual(receipt["inline_output"]["stdout"], "small-output" + os.linesep)
        verify = self.call(
            "verify", "--root", str(self.root), "--receipt", str(self.latest_receipt())
        )
        self.assertEqual(verify.returncode, 0, verify.stdout)
        self.assertTrue(json.loads(verify.stdout)["valid"])

    def test_large_output_is_bounded_and_exact_signal_is_recoverable(self) -> None:
        code = (
            "print('header');"
            "[print('noise-%04d' % i) for i in range(800)];"
            "print('AUC=0.731 baseline=0.744');print('tail')"
        )
        result = self.call(
            "run",
            "--root",
            str(self.root),
            "--inline-bytes",
            "1000",
            "--",
            sys.executable,
            "-c",
            code,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["lossy"])
        self.assertLess(receipt["returned_bytes"], receipt["raw_bytes"])
        excerpt = self.call(
            "excerpt",
            "--root",
            str(self.root),
            "--receipt",
            str(self.latest_receipt()),
            "--query",
            "AUC=0.731",
        )
        found = json.loads(excerpt.stdout)
        self.assertEqual(found["match_count"], 1)
        self.assertIn("baseline=0.744", found["content"])

    def test_signal_matching_does_not_treat_room_as_oom(self) -> None:
        code = "print('room=office'); print('x' * 500); print('OOM on device')"
        result = self.call(
            "run",
            "--root",
            str(self.root),
            "--inline-bytes",
            "200",
            "--",
            sys.executable,
            "-c",
            code,
        )
        receipt = json.loads(result.stdout)
        signals = receipt["inline_output"]["stdout"]["signals"]
        rendered = json.dumps(signals)
        self.assertIn("OOM on device", rendered)
        self.assertNotIn("room=office", rendered)

    def test_failure_is_reported_without_becoming_launch_success(self) -> None:
        result = self.call(
            "run",
            "--root",
            str(self.root),
            "--inline-bytes",
            "64",
            "--failure-inline-bytes",
            "256",
            "--",
            sys.executable,
            "-c",
            "import sys; print('ERROR exact-cause', file=sys.stderr); raise SystemExit(7)",
        )
        self.assertEqual(result.returncode, 1)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["exit_code"], 7)
        self.assertIn("ERROR exact-cause", json.dumps(receipt["inline_output"]))

    def test_arguments_are_not_interpreted_by_a_shell(self) -> None:
        literal = "hello; echo should-not-run"
        result = self.call(
            "run",
            "--root",
            str(self.root),
            "--",
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1])",
            literal,
        )
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["inline_output"]["stdout"], literal + os.linesep)

    def test_secret_like_argument_is_redacted_but_command_hash_is_stable(self) -> None:
        secret = "sk-" + "fake-example-secret-123456789"
        argv = [sys.executable, "-c", "print('ok')", "--api-key", secret]
        result = self.call("run", "--root", str(self.root), "--", *argv)
        receipt = json.loads(result.stdout)
        self.assertNotIn(secret, json.dumps(receipt))
        payload = json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertEqual(receipt["command_sha256"], hashlib.sha256(payload).hexdigest())

    def test_excerpt_rejects_path_outside_root(self) -> None:
        outside = Path(self.temp.name).parent / "outside-receipt.json"
        outside.write_text("{}", encoding="utf-8")
        result = self.call(
            "excerpt", "--root", str(self.root), "--receipt", str(outside), "--query", "x"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("escapes root", result.stdout)
        outside.unlink()

    def test_verify_detects_tampered_raw_output(self) -> None:
        result = self.call(
            "run", "--root", str(self.root), "--", sys.executable, "-c", "print('original')"
        )
        self.assertEqual(result.returncode, 0)
        receipt_path = self.latest_receipt()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        raw_path = self.root / receipt["streams"]["stdout"]["path"]
        raw_path.write_bytes(b"tampered\n")
        verify = self.call("verify", "--root", str(self.root), "--receipt", str(receipt_path))
        self.assertEqual(verify.returncode, 2)
        self.assertFalse(json.loads(verify.stdout)["valid"])

    def test_timeout_is_explicit(self) -> None:
        result = self.call(
            "run",
            "--root",
            str(self.root),
            "--timeout",
            "0.1",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(5)",
        )
        self.assertEqual(result.returncode, 1)
        self.assertTrue(json.loads(result.stdout)["timed_out"])

    def test_launch_error_is_not_reported_as_command_success(self) -> None:
        result = self.call(
            "run", "--root", str(self.root), "--", "definitely-not-a-real-executable-rtg"
        )
        self.assertEqual(result.returncode, 1)
        receipt = json.loads(result.stdout)
        self.assertIsNotNone(receipt["launch_error"])
        self.assertIsNone(receipt["exit_code"])

    def test_invalid_utf8_is_marked_and_raw_hash_still_verifies(self) -> None:
        result = self.call(
            "run",
            "--root",
            str(self.root),
            "--",
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(bytes([255, 254, 10]))",
        )
        receipt = json.loads(result.stdout)
        self.assertFalse(receipt["streams"]["stdout"]["utf8_valid"])
        verify = self.call(
            "verify", "--root", str(self.root), "--receipt", str(self.latest_receipt())
        )
        self.assertEqual(verify.returncode, 0)


if __name__ == "__main__":
    unittest.main()
