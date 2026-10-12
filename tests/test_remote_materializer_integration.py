import shutil
import unittest
import uuid

import yaml

from solution_agent.orchestrator import core
import solution_agent.orchestrator.adapters as _adapters_mod
import solution_agent.orchestrator.retry_controller as _retry_mod
from solution_agent.orchestrator.adapters import write_materializer_worker_out
from solution_agent.orchestrator.paths import result_dir, result_latest_dir, task_dir, worker_out_dir, worktree_dir
from solution_agent.orchestrator.registry import load_registry, save_registry
from solution_agent.orchestrator.task_loader import load_task, render_new_task


def _write_running_task(task_id: str) -> None:
    running = task_dir("running", task_id)
    running.mkdir(parents=True, exist_ok=True)
    (running / "task.md").write_text(render_new_task(task_id, "Remote materializer integration", worker="remote_materializer"), encoding="utf-8")


def _cleanup_task(task_id: str) -> None:
    for status in ("pending", "running", "completed", "partial", "failed"):
        path = task_dir(status, task_id)
        if path.exists():
            shutil.rmtree(path)
    for path in (worktree_dir(task_id), result_dir(task_id)):
        if path.exists():
            shutil.rmtree(path)


class RemoteMaterializerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.registry_backup = load_registry()

    def tearDown(self):
        save_registry(self.registry_backup)

    def test_materialized_worker_out_collects_partial(self):
        task_id = f"rm_mat_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def fake_run_remote_materializer(task_path, output_root, env_file, policy_path, feedback_text=""):
                # RetryController passes output_root=mat_out/attempts/attempt_001; write inside it.
                run_dir = output_root / task_id
                (run_dir / "generated").mkdir(parents=True, exist_ok=True)
                (run_dir / "generated" / "config.yaml").write_text("task_id: test\n", encoding="utf-8")
                (run_dir / "context_pack").mkdir(parents=True, exist_ok=True)
                (run_dir / "context_pack" / "task.md").write_text("task", encoding="utf-8")
                (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
                return {
                    "task_id": task_id,
                    "status": "materialized",
                    "task_type": "training_validation",
                    "generated_files": ["generated/config.yaml"],
                    "validation_summary": {"status": "pass", "failed_validators": []},
                    "retry_count": {"reader": 0, "qwen": 0},
                    "created_at": "2026-05-03T00:00:00+08:00",
                }

            _retry_mod.run_remote_materializer = fake_run_remote_materializer
            core.run_worker(task_id, worker="remote_materializer")

            result_yaml = worker_out_dir(task_id) / "result.yaml"
            self.assertTrue(result_yaml.exists())
            payload = yaml.safe_load(result_yaml.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "partial")
            self.assertEqual(payload["completion_scope"], "materialization_only")
            self.assertTrue(payload["requires_downstream_execution"])
            self.assertEqual(payload["outputs"]["generated"], "generated/")
            self.assertTrue((worker_out_dir(task_id) / "generated" / "config.yaml").exists())

            save_registry({"tasks": {task_id: {"task_id": task_id, "title": "Remote materializer integration", "status": "running"}}})
            collected = core.collect(task_id)

            self.assertEqual(collected["status"], "partial")
            self.assertTrue((result_latest_dir(task_id) / "generated" / "config.yaml").exists())
            latest_payload = yaml.safe_load((result_latest_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
            self.assertNotIn("missing _worker_out/result.yaml", latest_payload.get("summary_short", ""))
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup_task(task_id)

    def test_validation_failed_maps_to_failed(self):
        task_id = f"rm_fail_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            task = load_task(task_dir("running", task_id) / "task.md")
            write_materializer_worker_out(
                task,
                worker_out_dir(task_id),
                {
                    "task_id": task_id,
                    "status": "validation_failed",
                    "validation_summary": {"status": "fail", "failed_validators": ["validate_commands"]},
                    "escalation_reason": "validator failed",
                },
                mat_run_dir=result_dir(task_id) / "materializer" / task_id,
            )
            payload = yaml.safe_load((worker_out_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
            notes = (worker_out_dir(task_id) / "worker_notes.md").read_text(encoding="utf-8")
            self.assertEqual(payload["status"], "failed")
            self.assertIn("validate_commands", notes)
        finally:
            _cleanup_task(task_id)

    def test_escalation_required_records_partial_without_codex_fallback(self):
        task_id = f"rm_esc_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        original_run_codex = core.run_codex
        try:
            _write_running_task(task_id)

            def fake_run_remote_materializer(task_path, output_root, env_file, policy_path, feedback_text=""):
                return {
                    "task_id": task_id,
                    "status": "escalation_required",
                    "escalation_reason": "context_pack_validation_failed",
                    "created_at": "2026-05-03T00:00:00+08:00",
                }

            def fail_if_called(*args, **kwargs):
                raise AssertionError("run_codex should not be called by default")

            _retry_mod.run_remote_materializer = fake_run_remote_materializer
            core.run_codex = fail_if_called
            result = core.run_worker(task_id, worker="remote_materializer")

            payload = yaml.safe_load((worker_out_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "partial")
            self.assertTrue(payload["needs_escalation"])
            self.assertFalse(payload["fallback"]["executed"])
            self.assertFalse(result["fallback_executed"])
        finally:
            _retry_mod.run_remote_materializer = original
            core.run_codex = original_run_codex
            _cleanup_task(task_id)

    def test_ambiguous_maps_to_partial_without_codex_fallback(self):
        task_id = f"rm_amb_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            task = load_task(task_dir("running", task_id) / "task.md")
            write_materializer_worker_out(
                task,
                worker_out_dir(task_id),
                {
                    "task_id": task_id,
                    "status": "ambiguous",
                    "escalation_reason": "task.md is missing checkpoint path",
                },
                mat_run_dir=result_dir(task_id) / "materializer" / task_id,
            )
            payload = yaml.safe_load((worker_out_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "partial")
            self.assertTrue(payload["needs_escalation"])
            self.assertFalse(payload["fallback"]["executed"])
            self.assertEqual(payload["fallback"]["recommended_worker"], "codex")
        finally:
            _cleanup_task(task_id)

    def test_missing_mat_run_dir_does_not_crash_and_notes_artifacts_not_copied(self):
        task_id = f"rm_no_run_dir_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            task = load_task(task_dir("running", task_id) / "task.md")
            summary = write_materializer_worker_out(
                task,
                worker_out_dir(task_id),
                {"task_id": task_id, "status": "materialized"},
                mat_run_dir=None,
            )
            notes = (worker_out_dir(task_id) / "worker_notes.md").read_text(encoding="utf-8")
            payload = yaml.safe_load((worker_out_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
            self.assertTrue((worker_out_dir(task_id) / "result.yaml").exists())
            self.assertFalse(summary.generated_copied)
            self.assertEqual(payload["status"], "partial")
            self.assertIn("artifacts not copied: mat_run_dir not provided", notes)
        finally:
            _cleanup_task(task_id)

    def test_missing_result_package_still_writes_failed_result_yaml(self):
        task_id = f"rm_missing_pkg_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            task = load_task(task_dir("running", task_id) / "task.md")
            run_dir = result_dir(task_id) / "materializer" / task_id
            (run_dir / "generated").mkdir(parents=True, exist_ok=True)
            (run_dir / "generated" / "config.yaml").write_text("task_id: test\n", encoding="utf-8")
            write_materializer_worker_out(
                task,
                worker_out_dir(task_id),
                {"task_id": task_id, "status": "materialized"},
                mat_run_dir=run_dir,
            )
            payload = yaml.safe_load((worker_out_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["summary_short"], "remote_materializer did not produce result_package.yaml")
        finally:
            _cleanup_task(task_id)


    # --- early exception hardening ---

    def test_early_exception_still_writes_result_yaml(self):
        task_id = f"rm_exc_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def raise_immediately(*args, **kwargs):
                raise RuntimeError("deepseek_openai_compatible HTTP 401: Authentication Fails, Your api key is invalid")

            _retry_mod.run_remote_materializer = raise_immediately
            core.run_worker(task_id, worker="remote_materializer")

            result_yaml = worker_out_dir(task_id) / "result.yaml"
            self.assertTrue(result_yaml.exists(), "result.yaml must exist even on early exception")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup_task(task_id)

    def test_early_exception_result_yaml_has_partial_status_and_no_fallback(self):
        task_id = f"rm_exc_status_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        original_run_codex = core.run_codex
        try:
            _write_running_task(task_id)

            def raise_immediately(*args, **kwargs):
                raise RuntimeError("HTTP 401 api_key=sk-secret-value")

            def fail_if_called(*args, **kwargs):
                raise AssertionError("run_codex must not be called on early exception")

            _retry_mod.run_remote_materializer = raise_immediately
            core.run_codex = fail_if_called
            core.run_worker(task_id, worker="remote_materializer")

            payload = yaml.safe_load((worker_out_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "partial")
            self.assertFalse(payload["fallback"]["executed"])
            self.assertTrue(payload["needs_escalation"])
            self.assertEqual(payload["worker"], "remote_materializer")
        finally:
            _retry_mod.run_remote_materializer = original
            core.run_codex = original_run_codex
            _cleanup_task(task_id)

    def test_early_exception_stderr_is_scrubbed(self):
        task_id = f"rm_exc_scrub_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def raise_with_secret(*args, **kwargs):
                raise RuntimeError("HTTP 401 api_key=sk-real-secret-123456")

            _retry_mod.run_remote_materializer = raise_with_secret
            core.run_worker(task_id, worker="remote_materializer")

            stderr_text = (worker_out_dir(task_id) / "stderr.log").read_text(encoding="utf-8")
            self.assertIn("RuntimeError", stderr_text)
            self.assertNotIn("sk-real-secret-123456", stderr_text)
            self.assertIn("[REDACTED]", stderr_text)
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup_task(task_id)

    def test_early_exception_collect_does_not_fail(self):
        task_id = f"rm_exc_collect_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def raise_immediately(*args, **kwargs):
                raise RuntimeError("network unreachable")

            _retry_mod.run_remote_materializer = raise_immediately
            core.run_worker(task_id, worker="remote_materializer")

            save_registry({"tasks": {task_id: {"task_id": task_id, "title": "exc test", "status": "running"}}})
            collected = core.collect(task_id)

            self.assertIn(collected["status"], {"partial", "failed"})
            self.assertNotIn("missing _worker_out/result.yaml", collected.get("summary_short", ""))
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup_task(task_id)

    # --- provider policy resolution (unit tests against _resolve_provider_policy directly) ---

    def test_policy_resolution_uses_frontmatter_over_default(self):
        import tempfile
        import os
        from solution_agent.orchestrator.core import _resolve_provider_policy
        from solution_agent.orchestrator.schema import TaskBook
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w", encoding="utf-8") as f:
            f.write("reader:\n  model: stub\n")
            policy_path = f.name
        try:
            task = TaskBook(task_id="t1", title="t", raw_front_matter={"remote_materializer_policy": policy_path})
            old_env = os.environ.pop("REMOTE_MATERIALIZER_PROVIDER_POLICY", None)
            try:
                result = _resolve_provider_policy(task, Path("/pkg"))
            finally:
                if old_env is not None:
                    os.environ["REMOTE_MATERIALIZER_PROVIDER_POLICY"] = old_env
            self.assertEqual(str(result), policy_path)
        finally:
            os.unlink(policy_path)

    def test_policy_resolution_uses_env_var(self):
        import tempfile
        import os
        from solution_agent.orchestrator.core import _resolve_provider_policy
        from solution_agent.orchestrator.schema import TaskBook
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w", encoding="utf-8") as f:
            f.write("reader:\n  model: stub\n")
            policy_path = f.name
        try:
            task = TaskBook(task_id="t1", title="t", raw_front_matter={})
            old_env = os.environ.get("REMOTE_MATERIALIZER_PROVIDER_POLICY")
            os.environ["REMOTE_MATERIALIZER_PROVIDER_POLICY"] = policy_path
            try:
                result = _resolve_provider_policy(task, Path("/pkg"))
            finally:
                if old_env is None:
                    os.environ.pop("REMOTE_MATERIALIZER_PROVIDER_POLICY", None)
                else:
                    os.environ["REMOTE_MATERIALIZER_PROVIDER_POLICY"] = old_env
            self.assertEqual(str(result), policy_path)
        finally:
            os.unlink(policy_path)

    def test_policy_resolution_default_is_bailian(self):
        import os
        from solution_agent.orchestrator.core import _resolve_provider_policy
        from solution_agent.orchestrator.schema import TaskBook
        from pathlib import Path

        task = TaskBook(task_id="t1", title="t", raw_front_matter={})
        pkg = Path(__file__).resolve().parents[1] / "solution_agent"
        old_env = os.environ.pop("REMOTE_MATERIALIZER_PROVIDER_POLICY", None)
        try:
            result = _resolve_provider_policy(task, pkg)
        finally:
            if old_env is not None:
                os.environ["REMOTE_MATERIALIZER_PROVIDER_POLICY"] = old_env
        self.assertIn("bailian", str(result))
        self.assertNotIn("s2", str(result))


class PolicyResolutionBoundaryTests(unittest.TestCase):
    """Group 1: provider policy resolution edge cases.

    Even when the resolved policy path does not exist, run_worker must still
    write _worker_out/result.yaml (early exception guarantee).
    """

    def setUp(self):
        self.registry_backup = load_registry()
        self._original = _retry_mod.run_remote_materializer

    def tearDown(self):
        save_registry(self.registry_backup)
        _retry_mod.run_remote_materializer = self._original

    def _run_with_missing_policy(self, task_id: str, policy_path: str) -> None:
        _write_running_task(task_id)

        def raise_missing_policy(*args, **kwargs):
            raise FileNotFoundError(f"policy not found: {policy_path}")

        _retry_mod.run_remote_materializer = raise_missing_policy
        core.run_worker(task_id, worker="remote_materializer")

    def test_missing_frontmatter_policy_still_writes_result_yaml(self):
        import tempfile, os
        task_id = f"pol_miss_fm_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            missing = "/nonexistent/policy_frontmatter.yaml"

            def raise_missing(*args, **kwargs):
                raise FileNotFoundError(f"policy not found: {missing}")

            _retry_mod.run_remote_materializer = raise_missing
            core.run_worker(task_id, worker="remote_materializer")

            result_yaml = worker_out_dir(task_id) / "result.yaml"
            self.assertTrue(result_yaml.exists(), "result.yaml must exist even when policy path is missing")
            payload = yaml.safe_load(result_yaml.read_text(encoding="utf-8"))
            self.assertIn(payload["status"], {"partial", "failed"})
            self.assertTrue(payload.get("needs_escalation", False) or payload["status"] == "failed")
        finally:
            _cleanup_task(task_id)

    def test_missing_env_var_policy_still_writes_result_yaml(self):
        import os
        task_id = f"pol_miss_env_{uuid.uuid4().hex[:8]}"
        old_env = os.environ.get("REMOTE_MATERIALIZER_PROVIDER_POLICY")
        try:
            os.environ["REMOTE_MATERIALIZER_PROVIDER_POLICY"] = "/nonexistent/env_policy.yaml"
            _write_running_task(task_id)

            def raise_missing(*args, **kwargs):
                raise FileNotFoundError("env policy file not found")

            _retry_mod.run_remote_materializer = raise_missing
            core.run_worker(task_id, worker="remote_materializer")

            result_yaml = worker_out_dir(task_id) / "result.yaml"
            self.assertTrue(result_yaml.exists())
            payload = yaml.safe_load(result_yaml.read_text(encoding="utf-8"))
            self.assertIn(payload["status"], {"partial", "failed"})
        finally:
            if old_env is None:
                os.environ.pop("REMOTE_MATERIALIZER_PROVIDER_POLICY", None)
            else:
                os.environ["REMOTE_MATERIALIZER_PROVIDER_POLICY"] = old_env
            _cleanup_task(task_id)

    def test_missing_default_policy_still_writes_result_yaml(self):
        import os
        task_id = f"pol_miss_def_{uuid.uuid4().hex[:8]}"
        old_env = os.environ.pop("REMOTE_MATERIALIZER_PROVIDER_POLICY", None)
        try:
            _write_running_task(task_id)

            def raise_missing(*args, **kwargs):
                raise FileNotFoundError("bailian.yaml not found")

            _retry_mod.run_remote_materializer = raise_missing
            core.run_worker(task_id, worker="remote_materializer")

            result_yaml = worker_out_dir(task_id) / "result.yaml"
            self.assertTrue(result_yaml.exists())
            payload = yaml.safe_load(result_yaml.read_text(encoding="utf-8"))
            self.assertIn(payload["status"], {"partial", "failed"})
        finally:
            if old_env is not None:
                os.environ["REMOTE_MATERIALIZER_PROVIDER_POLICY"] = old_env
            _cleanup_task(task_id)

    def test_relative_policy_path_in_frontmatter_resolves(self):
        import tempfile, os
        from solution_agent.orchestrator.core import _resolve_provider_policy
        from solution_agent.orchestrator.schema import TaskBook
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w", encoding="utf-8") as f:
            f.write("reader:\n  model: stub\n")
            abs_path = f.name
        rel_path = os.path.relpath(abs_path)
        try:
            task = TaskBook(task_id="t1", title="t", raw_front_matter={"remote_materializer_policy": rel_path})
            old_env = os.environ.pop("REMOTE_MATERIALIZER_PROVIDER_POLICY", None)
            try:
                result = _resolve_provider_policy(task, Path("/pkg"))
            finally:
                if old_env is not None:
                    os.environ["REMOTE_MATERIALIZER_PROVIDER_POLICY"] = old_env
            self.assertEqual(str(result), rel_path)
        finally:
            os.unlink(abs_path)

    def test_absolute_policy_path_in_frontmatter_resolves(self):
        import tempfile, os
        from solution_agent.orchestrator.core import _resolve_provider_policy
        from solution_agent.orchestrator.schema import TaskBook
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w", encoding="utf-8") as f:
            f.write("reader:\n  model: stub\n")
            abs_path = f.name
        try:
            task = TaskBook(task_id="t1", title="t", raw_front_matter={"remote_materializer_policy": abs_path})
            old_env = os.environ.pop("REMOTE_MATERIALIZER_PROVIDER_POLICY", None)
            try:
                result = _resolve_provider_policy(task, Path("/pkg"))
            finally:
                if old_env is not None:
                    os.environ["REMOTE_MATERIALIZER_PROVIDER_POLICY"] = old_env
            self.assertEqual(str(result), abs_path)
        finally:
            os.unlink(abs_path)


class SecretScrubExtendedTests(unittest.TestCase):
    """Group 2: secret scrub covers all output paths and key formats."""

    def setUp(self):
        self.registry_backup = load_registry()
        self._original = _retry_mod.run_remote_materializer

    def tearDown(self):
        save_registry(self.registry_backup)
        _retry_mod.run_remote_materializer = self._original

    def _run_with_key_in_exception(self, task_id: str, key_string: str) -> None:
        _write_running_task(task_id)

        def raise_with_key(*args, **kwargs):
            raise RuntimeError(f"HTTP 401 DASHSCOPE_API_KEY={key_string}")

        _retry_mod.run_remote_materializer = raise_with_key
        core.run_worker(task_id, worker="remote_materializer")

    def test_dashscope_key_scrubbed_in_stderr(self):
        task_id = f"scrub_dash_{uuid.uuid4().hex[:8]}"
        secret = "sk-dashscope-9999-real"
        try:
            self._run_with_key_in_exception(task_id, secret)
            stderr = (worker_out_dir(task_id) / "stderr.log").read_text(encoding="utf-8")
            self.assertNotIn(secret, stderr)
            self.assertIn("[REDACTED]", stderr)
        finally:
            _cleanup_task(task_id)

    def test_deepseek_key_scrubbed_in_stderr(self):
        task_id = f"scrub_deep_{uuid.uuid4().hex[:8]}"
        secret = "sk-" + "fake-deepseek-abcdef-real"
        try:
            _write_running_task(task_id)

            def raise_with_key(*args, **kwargs):
                raise RuntimeError(f"api_key={secret} unauthorized")

            _retry_mod.run_remote_materializer = raise_with_key
            core.run_worker(task_id, worker="remote_materializer")

            stderr = (worker_out_dir(task_id) / "stderr.log").read_text(encoding="utf-8")
            self.assertNotIn(secret, stderr)
        finally:
            _cleanup_task(task_id)

    def test_double_quoted_key_scrubbed(self):
        task_id = f"scrub_dq_{uuid.uuid4().hex[:8]}"
        secret = "sk-double-quoted-real"
        try:
            _write_running_task(task_id)

            def raise_with_key(*args, **kwargs):
                raise RuntimeError(f'api_key="{secret}"')

            _retry_mod.run_remote_materializer = raise_with_key
            core.run_worker(task_id, worker="remote_materializer")

            stderr = (worker_out_dir(task_id) / "stderr.log").read_text(encoding="utf-8")
            self.assertNotIn(secret, stderr)
        finally:
            _cleanup_task(task_id)

    def test_single_quoted_key_scrubbed(self):
        task_id = f"scrub_sq_{uuid.uuid4().hex[:8]}"
        secret = "sk-single-quoted-real"
        try:
            _write_running_task(task_id)

            def raise_with_key(*args, **kwargs):
                raise RuntimeError(f"api_key='{secret}'")

            _retry_mod.run_remote_materializer = raise_with_key
            core.run_worker(task_id, worker="remote_materializer")

            stderr = (worker_out_dir(task_id) / "stderr.log").read_text(encoding="utf-8")
            self.assertNotIn(secret, stderr)
        finally:
            _cleanup_task(task_id)

    def test_key_not_in_result_yaml_validation_errors(self):
        task_id = f"scrub_ryaml_{uuid.uuid4().hex[:8]}"
        secret = "sk-" + "fake-result-yaml-leak-real"
        try:
            _write_running_task(task_id)

            def raise_with_key(*args, **kwargs):
                raise RuntimeError(f"api_key={secret} rejected")

            _retry_mod.run_remote_materializer = raise_with_key
            core.run_worker(task_id, worker="remote_materializer")

            result_text = (worker_out_dir(task_id) / "result.yaml").read_text(encoding="utf-8")
            self.assertNotIn(secret, result_text)
        finally:
            _cleanup_task(task_id)

    def test_key_not_in_worker_notes(self):
        task_id = f"scrub_notes_{uuid.uuid4().hex[:8]}"
        secret = "sk-notes-leak-real"
        try:
            _write_running_task(task_id)

            def raise_with_key(*args, **kwargs):
                raise RuntimeError(f"token={secret}")

            _retry_mod.run_remote_materializer = raise_with_key
            core.run_worker(task_id, worker="remote_materializer")

            notes_path = worker_out_dir(task_id) / "worker_notes.md"
            if notes_path.exists():
                self.assertNotIn(secret, notes_path.read_text(encoding="utf-8"))
        finally:
            _cleanup_task(task_id)


class ResultYamlSchemaTests(unittest.TestCase):
    """Group 3: result.yaml always has required fields regardless of materializer status."""

    def setUp(self):
        self.registry_backup = load_registry()

    def tearDown(self):
        save_registry(self.registry_backup)

    REQUIRED_FIELDS = {"status", "worker", "needs_escalation", "fallback", "started_at", "finished_at"}
    VALID_STATUSES = {"success", "partial", "failed"}

    def _load_result(self, task_id: str) -> dict:
        return yaml.safe_load((worker_out_dir(task_id) / "result.yaml").read_text(encoding="utf-8"))

    def test_success_result_has_required_fields(self):
        task_id = f"schema_ok_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            from solution_agent.orchestrator.task_loader import load_task
            task = load_task(task_dir("running", task_id) / "task.md")
            write_materializer_worker_out(
                task, worker_out_dir(task_id),
                {"task_id": task_id, "status": "materialized", "generated_files": [], "created_at": "2026-05-03T00:00:00+08:00"},
                mat_run_dir=None,
            )
            payload = self._load_result(task_id)
            for field in self.REQUIRED_FIELDS:
                self.assertIn(field, payload, f"missing required field: {field}")
            self.assertIn(payload["status"], self.VALID_STATUSES)
            self.assertIn("executed", payload["fallback"])
        finally:
            _cleanup_task(task_id)

    def test_partial_result_has_required_fields(self):
        task_id = f"schema_part_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            from solution_agent.orchestrator.task_loader import load_task
            task = load_task(task_dir("running", task_id) / "task.md")
            write_materializer_worker_out(
                task, worker_out_dir(task_id),
                {"task_id": task_id, "status": "escalation_required", "escalation_reason": "test"},
                mat_run_dir=None,
            )
            payload = self._load_result(task_id)
            for field in self.REQUIRED_FIELDS:
                self.assertIn(field, payload, f"missing required field: {field}")
            self.assertIn(payload["status"], self.VALID_STATUSES)
        finally:
            _cleanup_task(task_id)

    def test_failed_result_has_required_fields(self):
        task_id = f"schema_fail_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            from solution_agent.orchestrator.task_loader import load_task
            task = load_task(task_dir("running", task_id) / "task.md")
            write_materializer_worker_out(
                task, worker_out_dir(task_id),
                {"task_id": task_id, "status": "validation_failed",
                 "validation_summary": {"status": "fail", "failed_validators": ["v1"]}},
                mat_run_dir=None,
            )
            payload = self._load_result(task_id)
            for field in self.REQUIRED_FIELDS:
                self.assertIn(field, payload, f"missing required field: {field}")
            self.assertIn(payload["status"], self.VALID_STATUSES)
        finally:
            _cleanup_task(task_id)

    def test_status_is_always_valid_enum_value(self):
        task_id = f"schema_enum_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def raise_immediately(*args, **kwargs):
                raise ValueError("unexpected error")

            _retry_mod.run_remote_materializer = raise_immediately
            core.run_worker(task_id, worker="remote_materializer")

            payload = self._load_result(task_id)
            self.assertIn(payload["status"], self.VALID_STATUSES,
                          f"status {payload['status']!r} is not a valid enum value")
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup_task(task_id)

    def test_fallback_block_always_present(self):
        task_id = f"schema_fb_{uuid.uuid4().hex[:8]}"
        original = _retry_mod.run_remote_materializer
        try:
            _write_running_task(task_id)

            def raise_immediately(*args, **kwargs):
                raise RuntimeError("boom")

            _retry_mod.run_remote_materializer = raise_immediately
            core.run_worker(task_id, worker="remote_materializer")

            payload = self._load_result(task_id)
            self.assertIn("fallback", payload)
            self.assertIn("executed", payload["fallback"])
        finally:
            _retry_mod.run_remote_materializer = original
            _cleanup_task(task_id)


class ArtifactCopyBoundaryTests(unittest.TestCase):
    """Group 4: artifact copy does not crash on missing/empty dirs and does not
    overwrite contract files (result.yaml, worker_notes.md)."""

    def setUp(self):
        self.registry_backup = load_registry()

    def tearDown(self):
        save_registry(self.registry_backup)

    def test_missing_generated_dir_does_not_crash(self):
        task_id = f"art_no_gen_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            from solution_agent.orchestrator.task_loader import load_task
            task = load_task(task_dir("running", task_id) / "task.md")
            run_dir = result_dir(task_id) / "materializer" / task_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
            write_materializer_worker_out(
                task, worker_out_dir(task_id),
                {"task_id": task_id, "status": "materialized", "generated_files": ["generated/missing.yaml"]},
                mat_run_dir=run_dir,
            )
            self.assertTrue((worker_out_dir(task_id) / "result.yaml").exists())
        finally:
            _cleanup_task(task_id)

    def test_empty_generated_dir_does_not_crash(self):
        task_id = f"art_empty_gen_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            from solution_agent.orchestrator.task_loader import load_task
            task = load_task(task_dir("running", task_id) / "task.md")
            run_dir = result_dir(task_id) / "materializer" / task_id
            (run_dir / "generated").mkdir(parents=True, exist_ok=True)
            (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
            write_materializer_worker_out(
                task, worker_out_dir(task_id),
                {"task_id": task_id, "status": "materialized", "generated_files": []},
                mat_run_dir=run_dir,
            )
            self.assertTrue((worker_out_dir(task_id) / "result.yaml").exists())
        finally:
            _cleanup_task(task_id)

    def test_missing_context_pack_does_not_crash(self):
        task_id = f"art_no_ctx_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            from solution_agent.orchestrator.task_loader import load_task
            task = load_task(task_dir("running", task_id) / "task.md")
            run_dir = result_dir(task_id) / "materializer" / task_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
            write_materializer_worker_out(
                task, worker_out_dir(task_id),
                {"task_id": task_id, "status": "materialized"},
                mat_run_dir=run_dir,
            )
            self.assertTrue((worker_out_dir(task_id) / "result.yaml").exists())
        finally:
            _cleanup_task(task_id)

    def test_artifact_copy_does_not_overwrite_result_yaml(self):
        task_id = f"art_no_ow_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            from solution_agent.orchestrator.task_loader import load_task
            task = load_task(task_dir("running", task_id) / "task.md")
            run_dir = result_dir(task_id) / "materializer" / task_id
            gen_dir = run_dir / "generated"
            gen_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "result_package.yaml").write_text("status: materialized\n", encoding="utf-8")
            (gen_dir / "result.yaml").write_text("POISONED\n", encoding="utf-8")
            write_materializer_worker_out(
                task, worker_out_dir(task_id),
                {"task_id": task_id, "status": "materialized", "generated_files": ["generated/result.yaml"]},
                mat_run_dir=run_dir,
            )
            result_text = (worker_out_dir(task_id) / "result.yaml").read_text(encoding="utf-8")
            self.assertNotEqual(result_text.strip(), "POISONED",
                                "generated/result.yaml must not overwrite _worker_out/result.yaml")
            payload = yaml.safe_load(result_text)
            self.assertIn(payload["status"], {"success", "partial", "failed"})
        finally:
            _cleanup_task(task_id)


class NonRegressionCodexSSHTests(unittest.TestCase):
    """Group 5: Phase 1+ changes must not affect the Codex or SSH paths."""

    def setUp(self):
        self.registry_backup = load_registry()

    def tearDown(self):
        save_registry(self.registry_backup)

    def test_run_worker_codex_does_not_call_run_remote_materializer(self):
        original_run_codex = core.run_codex
        original = _retry_mod.run_remote_materializer
        task_id = f"nr_codex_{uuid.uuid4().hex[:8]}"
        materializer_called = []
        try:
            _write_running_task(task_id)

            def track_materializer(*args, **kwargs):
                materializer_called.append(True)
                return {}

            def fake_codex(prompt, worktree, *args, **kwargs):
                return {"status": "success", "returncode": 0, "stdout": "", "stderr": ""}

            _retry_mod.run_remote_materializer = track_materializer
            core.run_codex = fake_codex
            core.run_worker(task_id, worker="codex")

            self.assertEqual(materializer_called, [],
                             "run_remote_materializer must NOT be called when worker=codex")
        finally:
            core.run_codex = original_run_codex
            _retry_mod.run_remote_materializer = original
            _cleanup_task(task_id)

    def test_run_worker_codex_dispatches_identical_prompt_once_and_returns_compact_stream_metadata(self):
        original_run_codex = core.run_codex
        task_id = f"nr_codex_once_{uuid.uuid4().hex[:8]}"
        calls = []
        try:
            _write_running_task(task_id)

            def fake_codex(prompt, worktree, *args, **kwargs):
                calls.append(True)
                return {
                    "status": "success",
                    "returncode": 0,
                    "stdout": "large-output\n" * 2000,
                    "stderr": "",
                }

            core.run_codex = fake_codex
            first = core.run_worker(task_id, worker="codex")
            second = core.run_worker(task_id, worker="codex")

            self.assertEqual(len(calls), 1)
            self.assertTrue(first["model_dispatched"])
            self.assertTrue(first["output_omitted"])
            self.assertIsInstance(first["stdout"], dict)
            self.assertGreater(first["stdout"]["bytes"], 10000)
            status_text = (worker_out_dir(task_id) / "status.json").read_text(encoding="utf-8")
            self.assertNotIn("large-output", status_text)
            self.assertIn("model_requests", first["stdout"]["path"])
            self.assertFalse(second["model_dispatched"])
            self.assertEqual(second["reason"], "duplicate_model_request")
            self.assertTrue((worker_out_dir(task_id) / "usage" / "usage_events.jsonl").exists())
        finally:
            core.run_codex = original_run_codex
            _cleanup_task(task_id)

    def test_run_worker_remote_materializer_does_not_call_run_codex(self):
        original_run_codex = core.run_codex
        original = _retry_mod.run_remote_materializer
        task_id = f"nr_mat_{uuid.uuid4().hex[:8]}"
        codex_called = []
        try:
            _write_running_task(task_id)

            def track_codex(*args, **kwargs):
                codex_called.append(True)
                raise AssertionError("run_codex must not be called for remote_materializer worker")

            def fake_mat(task_path, output_root, env_file, policy_path, feedback_text=""):
                return {"task_id": task_id, "status": "materialized", "created_at": "2026-05-03T00:00:00+08:00",
                        "generated_files": [], "retry_count": {"reader": 0, "qwen": 0}}

            core.run_codex = track_codex
            _retry_mod.run_remote_materializer = fake_mat
            core.run_worker(task_id, worker="remote_materializer")

            self.assertEqual(codex_called, [],
                             "run_codex must NOT be called when worker=remote_materializer")
        finally:
            core.run_codex = original_run_codex
            _retry_mod.run_remote_materializer = original
            _cleanup_task(task_id)

    def test_run_worker_unknown_raises_value_error(self):
        task_id = f"nr_unk_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)
            with self.assertRaises(ValueError):
                core.run_worker(task_id, worker="totally_unknown_worker_xyz")
        finally:
            _cleanup_task(task_id)

    def test_open_ssh_stream_not_imported_by_materializer_path(self):
        from solution_agent.orchestrator import core as _core
        self.assertTrue(hasattr(_core, "open_ssh_stream"),
                        "open_ssh_stream should exist in core module")
        original = _retry_mod.run_remote_materializer
        ssh_calls = []
        original_ssh = _core.open_ssh_stream

        def track_ssh(*args, **kwargs):
            ssh_calls.append(True)
            return original_ssh(*args, **kwargs)

        task_id = f"nr_ssh_{uuid.uuid4().hex[:8]}"
        try:
            _write_running_task(task_id)

            def fake_mat(*args, **kwargs):
                return {"task_id": task_id, "status": "materialized", "created_at": "2026-05-03T00:00:00+08:00",
                        "generated_files": [], "retry_count": {"reader": 0, "qwen": 0}}

            _retry_mod.run_remote_materializer = fake_mat
            _core.open_ssh_stream = track_ssh
            _core.run_worker(task_id, worker="remote_materializer")

            self.assertEqual(ssh_calls, [], "open_ssh_stream must NOT be called during remote_materializer run")
        finally:
            _retry_mod.run_remote_materializer = original
            _core.open_ssh_stream = original_ssh
            _cleanup_task(task_id)


if __name__ == "__main__":
    unittest.main()
