"""Characterization tests for Phase 2A: ProviderAdapter interface extraction.

These tests document existing behaviour as a safety net before any refactoring.
They must pass with zero changes to the production code they test.

Coverage:
  1. load_env_file()  — .env quote stripping and setdefault semantics
  2. load_policy_clients()  — provider policy YAML resolution
  3. OpenAICompatibleChatClient.complete()  — HTTP error paths
  4. OpenAICompatibleChatClient.generate()  — ProviderAdapter wrapper
  5. Exception propagation  — unhandled RuntimeError leaves no result_package.yaml
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make sure we can import from tests/ helpers
sys.path.insert(0, str(Path(__file__).parent))
from helpers import WorkspaceTempDir

from solution_agent.remote_materializer.model_clients import (
    OpenAICompatibleChatClient,
    OpenAICompatibleConfig,
    StaticMaterializerClient,
    StaticReaderClient,
    load_env_file,
    load_policy_clients,
)
from solution_agent.remote_materializer.api_metrics import summarize_api_metrics
from solution_agent.remote_materializer.controller import run_materializer
from solution_agent.remote_materializer.provider_adapter import (
    ProviderAdapter,
    ProviderRequest,
    ProviderResponse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_POLICY_YAML = textwrap.dedent("""\
    primary_reader:
      provider: test_provider
      model: reader-model
      base_url: http://localhost:8080/v1
      api_key_env: TEST_API_KEY

    primary_materializer:
      provider: test_provider
      model: mat-model
      base_url: http://localhost:8080/v1
      api_key_env: TEST_API_KEY
""")

_POLICY_WITH_BACKUP_MODEL = textwrap.dedent("""\
    primary_reader:
      provider: test_provider
      model: reader-model
      base_url: http://localhost:8080/v1
      api_key_env: TEST_API_KEY

    primary_materializer:
      provider: test_provider
      model: mat-model
      backup_model: mat-model-backup
      base_url: http://localhost:8080/v1
      api_key_env: TEST_API_KEY
""")

_POLICY_WITH_VERIFIER = textwrap.dedent("""\
    primary_reader:
      provider: test_provider
      model: reader-model
      base_url: http://localhost:8080/v1
      api_key_env: TEST_API_KEY

    primary_materializer:
      provider: test_provider
      model: mat-model
      base_url: http://localhost:8080/v1
      api_key_env: TEST_API_KEY

    reader_verifier:
      provider: test_provider
      model: verifier-model
      base_url: http://localhost:8080/v1
      api_key_env: TEST_API_KEY
""")


def _make_policy_file(tmp: Path, content: str) -> Path:
    p = tmp / "provider_policy.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _openai_response_body(content: str, usage: dict | None = None) -> bytes:
    payload = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload).encode()


def _http_mock(status: int, body: bytes = b"error body") -> MagicMock:
    """Return a mock that raises HTTPError when urlopen is called."""
    exc = urllib.error.HTTPError(
        url="http://test", code=status, msg="err", hdrs=MagicMock(), fp=BytesIO(body)
    )
    m = MagicMock(side_effect=exc)
    return m


def _success_mock(content: str) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = _openai_response_body(content)
    return MagicMock(return_value=resp)


def _success_mock_with_usage(content: str, usage: dict) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    resp.read.return_value = _openai_response_body(content, usage)
    return MagicMock(return_value=resp)


# ---------------------------------------------------------------------------
# 1. load_env_file — .env quote stripping
# ---------------------------------------------------------------------------

class TestLoadEnvFile(unittest.TestCase):
    def setUp(self):
        # Save and clear any pre-existing test keys
        self._saved = {k: os.environ.pop(k, None) for k in ["ENV_A", "ENV_B", "ENV_C", "ENV_D", "ENV_E"]}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_env(self, tmp: Path, content: str) -> Path:
        p = tmp / ".env"
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return p

    def test_strips_double_quotes(self):
        with WorkspaceTempDir() as tmp:
            env_path = self._write_env(tmp, 'ENV_A="hello"\n')
            load_env_file(env_path)
        self.assertEqual(os.environ["ENV_A"], "hello")

    def test_strips_single_quotes(self):
        with WorkspaceTempDir() as tmp:
            env_path = self._write_env(tmp, "ENV_B='world'\n")
            load_env_file(env_path)
        self.assertEqual(os.environ["ENV_B"], "world")

    def test_no_quotes_unchanged(self):
        with WorkspaceTempDir() as tmp:
            env_path = self._write_env(tmp, "ENV_C=plain\n")
            load_env_file(env_path)
        self.assertEqual(os.environ["ENV_C"], "plain")

    def test_skips_comment_lines(self):
        with WorkspaceTempDir() as tmp:
            env_path = self._write_env(tmp, "# ENV_D=should_be_ignored\n")
            load_env_file(env_path)
        self.assertNotIn("# ENV_D", os.environ)
        self.assertNotIn("ENV_D", os.environ)

    def test_skips_blank_lines(self):
        with WorkspaceTempDir() as tmp:
            env_path = self._write_env(tmp, "\n\nENV_E=set\n\n")
            load_env_file(env_path)
        self.assertEqual(os.environ["ENV_E"], "set")

    def test_setdefault_does_not_overwrite_existing(self):
        os.environ["ENV_A"] = "original"
        with WorkspaceTempDir() as tmp:
            env_path = self._write_env(tmp, "ENV_A=new_value\n")
            load_env_file(env_path)
        self.assertEqual(os.environ["ENV_A"], "original")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_env_file(Path("/nonexistent/.env"))


# ---------------------------------------------------------------------------
# 2. load_policy_clients — provider policy YAML resolution
# ---------------------------------------------------------------------------

class TestLoadPolicyClients(unittest.TestCase):
    def test_primary_reader_resolved(self):
        with WorkspaceTempDir() as tmp:
            p = _make_policy_file(tmp, _MINIMAL_POLICY_YAML)
            clients = load_policy_clients(p)
        self.assertIsNotNone(clients.reader)

    def test_primary_materializer_resolved(self):
        with WorkspaceTempDir() as tmp:
            p = _make_policy_file(tmp, _MINIMAL_POLICY_YAML)
            clients = load_policy_clients(p)
        self.assertIsNotNone(clients.materializer)

    def test_no_backup_when_absent(self):
        with WorkspaceTempDir() as tmp:
            p = _make_policy_file(tmp, _MINIMAL_POLICY_YAML)
            clients = load_policy_clients(p)
        self.assertIsNone(clients.backup_materializer)

    def test_backup_resolved_from_backup_model_field(self):
        with WorkspaceTempDir() as tmp:
            p = _make_policy_file(tmp, _POLICY_WITH_BACKUP_MODEL)
            clients = load_policy_clients(p)
        self.assertIsNotNone(clients.backup_materializer)

    def test_verifier_resolved_when_present(self):
        with WorkspaceTempDir() as tmp:
            p = _make_policy_file(tmp, _POLICY_WITH_VERIFIER)
            clients = load_policy_clients(p)
        self.assertIsNotNone(clients.reader_verifier)

    def test_verifier_none_when_absent(self):
        with WorkspaceTempDir() as tmp:
            p = _make_policy_file(tmp, _MINIMAL_POLICY_YAML)
            clients = load_policy_clients(p)
        self.assertIsNone(clients.reader_verifier)


# ---------------------------------------------------------------------------
# 3. OpenAICompatibleChatClient — HTTP error paths (existing complete())
# ---------------------------------------------------------------------------

class TestOpenAICompatibleChatClientErrors(unittest.TestCase):
    def _make_client(self) -> OpenAICompatibleChatClient:
        cfg = OpenAICompatibleConfig(
            provider="test_provider",
            model="test-model",
            base_url="http://localhost:8080/v1",
            api_key_env="TEST_API_KEY_CHARTEST",
        )
        return OpenAICompatibleChatClient(cfg)

    def setUp(self):
        os.environ["TEST_API_KEY_CHARTEST"] = "test-key"

    def tearDown(self):
        os.environ.pop("TEST_API_KEY_CHARTEST", None)

    def test_missing_api_key_raises_runtime_error(self):
        os.environ.pop("TEST_API_KEY_CHARTEST", None)
        client = self._make_client()
        with self.assertRaises(RuntimeError) as ctx:
            client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("missing API key", str(ctx.exception))

    def test_http_error_raises_runtime_error(self):
        client = self._make_client()
        with patch("urllib.request.urlopen", _http_mock(500, b"internal server error")):
            with self.assertRaises(RuntimeError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("500", str(ctx.exception))

    def test_http_429_raises_runtime_error(self):
        client = self._make_client()
        with patch("urllib.request.urlopen", _http_mock(429, b"rate limit exceeded")):
            with self.assertRaises(RuntimeError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("429", str(ctx.exception))

    def test_empty_choices_raises_runtime_error(self):
        client = self._make_client()
        empty = MagicMock()
        empty.__enter__ = lambda s: s
        empty.__exit__ = MagicMock(return_value=False)
        empty.read.return_value = json.dumps({"choices": []}).encode()
        with patch("urllib.request.urlopen", MagicMock(return_value=empty)):
            with self.assertRaises(RuntimeError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("no choices", str(ctx.exception))

    def test_empty_content_raises_runtime_error(self):
        client = self._make_client()
        empty_content = MagicMock()
        empty_content.__enter__ = lambda s: s
        empty_content.__exit__ = MagicMock(return_value=False)
        empty_content.read.return_value = json.dumps(
            {"choices": [{"message": {"content": ""}}]}
        ).encode()
        with patch("urllib.request.urlopen", MagicMock(return_value=empty_content)):
            with self.assertRaises(RuntimeError) as ctx:
                client.complete([{"role": "user", "content": "hi"}])
        self.assertIn("empty", str(ctx.exception))


# ---------------------------------------------------------------------------
# 4. OpenAICompatibleChatClient.generate() — ProviderAdapter wrapper
# ---------------------------------------------------------------------------

class TestOpenAICompatibleChatClientGenerate(unittest.TestCase):
    def setUp(self):
        os.environ["TEST_API_KEY_CHARTEST"] = "test-key"

    def tearDown(self):
        os.environ.pop("TEST_API_KEY_CHARTEST", None)

    def _make_client(self) -> OpenAICompatibleChatClient:
        cfg = OpenAICompatibleConfig(
            provider="test_provider",
            model="test-model",
            base_url="http://localhost:8080/v1",
            api_key_env="TEST_API_KEY_CHARTEST",
        )
        return OpenAICompatibleChatClient(cfg)

    def test_generate_returns_provider_response(self):
        client = self._make_client()
        request = ProviderRequest(
            provider_name="test_provider",
            model="test-model",
            system_prompt="You are a test assistant.",
            user_prompt="Say hello.",
        )
        with patch("urllib.request.urlopen", _success_mock("Hello!")):
            response = client.generate(request)

        self.assertIsInstance(response, ProviderResponse)
        self.assertEqual(response.text, "Hello!")
        self.assertEqual(response.provider_name, "test_provider")
        self.assertEqual(response.model, "test-model")
        self.assertGreaterEqual(response.latency_ms, 0)

    def test_generate_propagates_http_error_as_runtime_error(self):
        client = self._make_client()
        request = ProviderRequest(
            provider_name="test_provider",
            model="test-model",
            system_prompt="sys",
            user_prompt="user",
        )
        with patch("urllib.request.urlopen", _http_mock(500)):
            with self.assertRaises(RuntimeError):
                client.generate(request)

    def test_client_satisfies_provider_adapter_protocol(self):
        """OpenAICompatibleChatClient.generate() matches the ProviderAdapter protocol."""
        client = self._make_client()
        # Runtime check: the method must exist and be callable
        self.assertTrue(callable(getattr(client, "generate", None)))


# ---------------------------------------------------------------------------
# 5. Exception propagation — characterize current (no-catch) behaviour
# ---------------------------------------------------------------------------

class TestOpenAICompatibleChatClientMetrics(unittest.TestCase):
    def setUp(self):
        os.environ["TEST_API_KEY_CHARTEST"] = "test-key"

    def tearDown(self):
        os.environ.pop("TEST_API_KEY_CHARTEST", None)
        os.environ.pop("SOLUTION_AGENT_API_METRICS_PATH", None)

    def _make_client(self) -> OpenAICompatibleChatClient:
        cfg = OpenAICompatibleConfig(
            provider="test_provider",
            model="test-model",
            base_url="http://localhost:8080/v1",
            api_key_env="TEST_API_KEY_CHARTEST",
            role="primary_reader",
        )
        return OpenAICompatibleChatClient(cfg)

    def test_complete_writes_success_metric_when_path_configured(self):
        with WorkspaceTempDir() as tmp:
            metrics_path = tmp / "api_model_calls.jsonl"
            os.environ["SOLUTION_AGENT_API_METRICS_PATH"] = str(metrics_path)
            client = self._make_client()
            with patch(
                "urllib.request.urlopen",
                _success_mock_with_usage("Hello!", {"prompt_tokens": 3, "completion_tokens": 2}),
            ):
                self.assertEqual(client.complete([{"role": "user", "content": "hi"}]), "Hello!")

            rows = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "success")
        self.assertEqual(rows[0]["provider"], "test_provider")
        self.assertEqual(rows[0]["model"], "test-model")
        self.assertEqual(rows[0]["role"], "primary_reader")
        self.assertEqual(rows[0]["usage"]["prompt_tokens"], 3)
        self.assertEqual(rows[0]["completion_chars"], 6)

    def test_complete_writes_error_metric_without_secret_value(self):
        os.environ.pop("TEST_API_KEY_CHARTEST", None)
        with WorkspaceTempDir() as tmp:
            metrics_path = tmp / "api_model_calls.jsonl"
            os.environ["SOLUTION_AGENT_API_METRICS_PATH"] = str(metrics_path)
            client = self._make_client()
            with self.assertRaises(RuntimeError):
                client.complete([{"role": "user", "content": "hi"}])

            rows = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(rows[0]["error_type"], "missing_api_key")
        self.assertIn("TEST_API_KEY_CHARTEST", rows[0]["error_message"])
        self.assertNotIn("test-key", rows[0]["error_message"])

    def test_summarize_api_metrics_groups_by_provider_model_role(self):
        with WorkspaceTempDir() as tmp:
            metrics_path = tmp / "api_model_calls.jsonl"
            metrics_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "provider": "p",
                                "model": "m",
                                "role": "reader",
                                "status": "success",
                                "latency_ms": 100,
                                "prompt_chars": 10,
                                "completion_chars": 5,
                                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                            }
                        ),
                        json.dumps(
                            {
                                "provider": "p",
                                "model": "m",
                                "role": "reader",
                                "status": "error",
                                "latency_ms": 50,
                                "prompt_chars": 8,
                                "completion_chars": 0,
                                "usage": {},
                                "error_type": "http_429",
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            summary = summarize_api_metrics(metrics_path)

        self.assertEqual(summary["total_calls"], 2)
        group = summary["groups"][0]
        self.assertEqual(group["calls"], 2)
        self.assertEqual(group["success"], 1)
        self.assertEqual(group["error"], 1)
        self.assertEqual(group["success_rate"], 0.5)
        self.assertEqual(group["avg_latency_ms"], 75.0)
        self.assertEqual(group["errors_by_type"], {"http_429": 1})


class TestExceptionPropagation(unittest.TestCase):
    """Characterize what happens when an LLM call throws before _finish().

    Current behaviour: the exception propagates uncaught; no result_package.yaml
    is written.  These tests pin this behaviour so future error-handling changes
    are intentional and visible.
    """

    def _task_md(self, task_id: str) -> str:
        return f"""---
task_id: {task_id}
title: Test task
priority: normal
worker: remote_materializer
status: pending
created_at: 2026-05-01T00:00:00+08:00
task_type: training_validation
---

# Goal

Test exception propagation.
"""

    def test_reader_exception_propagates_to_caller(self):
        class RaisingReaderClient:
            def create_work_order(self, task_markdown: str, schema_text: str, prompt_text: str) -> str:
                raise RuntimeError("simulated API failure")

        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(self._task_md("exc_test"), encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                run_materializer(
                    task_path,
                    tmp / "runs",
                    RaisingReaderClient(),
                    StaticMaterializerClient([""]),
                )
            self.assertIn("simulated API failure", str(ctx.exception))

    def test_reader_exception_leaves_no_result_package(self):
        class RaisingReaderClient:
            def create_work_order(self, task_markdown: str, schema_text: str, prompt_text: str) -> str:
                raise RuntimeError("simulated API failure")

        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(self._task_md("exc_test2"), encoding="utf-8")
            try:
                run_materializer(
                    task_path,
                    tmp / "runs",
                    RaisingReaderClient(),
                    StaticMaterializerClient([""]),
                )
            except RuntimeError:
                pass
            result_package = tmp / "runs" / "exc_test2" / "result_package.yaml"
            # Characterization: exception before _finish() → no result_package.yaml
            self.assertFalse(
                result_package.exists(),
                "Current behaviour: exception before _finish() leaves no result_package.yaml. "
                "If this test fails, the error-handling path changed — verify intentionally.",
            )

    def test_materializer_exception_propagates_to_caller(self):
        # Must include all REQUIRED_FORBIDDEN actions to pass work_order validation.
        work_order_json = json.dumps({
            "status": "ready",
            "task_id": "mat_exc_test",
            "task_type": "training_validation",
            "source_task_path": "tasks/mat_exc_test/task.md",
            "jobs": [{"job_id": "generate", "worker": "qwen_coder_30b", "job_type": "generate_materials"}],
            "required_outputs": ["commands.md", "output_contract.yaml"],
            "input_files": ["tasks/mat_exc_test/task.md"],
            "allowed_output_root": "runs/mat_exc_test/",
            "forbidden_actions": [
                "modify_source_code", "execute_training",
                "change_dataset_split", "overwrite_checkpoint", "delete_files",
            ],
            "validation_steps": ["validate_commands_allowlist"],
            "retry_policy": {"reader_max_retries": 0, "qwen_max_retries": 0},
            "escalation_policy": {"escalate_if": []},
        })

        class RaisingMaterializerClient:
            def materialize(self, *args, **kwargs) -> str:
                raise RuntimeError("simulated materializer failure")

        with WorkspaceTempDir() as tmp:
            task_path = tmp / "task.md"
            task_path.write_text(self._task_md("mat_exc_test"), encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                run_materializer(
                    task_path,
                    tmp / "runs",
                    StaticReaderClient(work_order_json),
                    RaisingMaterializerClient(),
                )
            self.assertIn("simulated materializer failure", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
