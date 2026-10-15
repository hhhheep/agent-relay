import shutil
import tarfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from solution_agent.orchestrator import core
from solution_agent.orchestrator.paths import dispatch_dir, ensure_orchestrator_dirs, registry_path, state_dir, task_dir, worktree_dir
from solution_agent.orchestrator.registry import load_registry, save_registry
from solution_agent.orchestrator.task_loader import render_new_task
from tests.helpers import WorkspaceTempDir


class RemoteStatusWorkerAgnosticTests(unittest.TestCase):
    def setUp(self):
        # The extracted source does not ship private research workspace inputs.
        # Exercise the real loaders/builders with an explicit synthetic workspace.
        workspace = WorkspaceTempDir()
        self.workspace = workspace.__enter__()
        self.addCleanup(workspace.__exit__, None, None, None)
        for target in ("solution_agent.orchestrator.paths.root", "solution_agent.orchestrator.core.root"):
            patcher = patch(target, return_value=self.workspace)
            patcher.start()
            self.addCleanup(patcher.stop)
        ensure_orchestrator_dirs()
        (self.workspace / "config" / "remote_execution_profiles.yaml").write_text(
            "schema_version: 1\nprofiles:\n"
            "  EXAMPLE_WORKER_B:\n    host: EXAMPLE_WORKER_B\n"
            "    remote_workdir: /srv/example-project\n    conda:\n      env: py_torch\n"
            "  EXAMPLE_WORKER_A:\n    host: EXAMPLE_WORKER_A\n"
            "    remote_workdir: /srv/example-workspaces-b/loupe_workspace/Loupe_agent\n"
            "    conda:\n      prefix: /srv/example-workspaces-b/envs/loupe\n",
            encoding="utf-8",
        )
        for relative in (
            "docs/MAINTENANCE_POLICY_V1.md", "docs/SCHEDULER_OPTIMIZATION_NOTES.md",
            "docs/OPENCODER_TASK_EXECUTION_GUIDE.md", "docs/INBOX_TASK_FORMAT.md",
            "scripts/probes/run_probe.py", "scripts/loupe/run_seg_overfit.py",
        ):
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# Synthetic bootstrap input; never executed.\n", encoding="utf-8")
        # run_remote packages the actual core source; only workspace inputs are synthetic.
        source = Path(core.__file__).resolve().parents[2] / "solution_agent"
        shutil.copytree(source, self.workspace / "solution_agent", ignore=shutil.ignore_patterns("__pycache__"))
        self.registry_backup = load_registry()

    def tearDown(self):
        save_registry(self.registry_backup, path=registry_path())

    def _write_task(self, task_id: str) -> None:
        running = task_dir("running", task_id)
        running.mkdir(parents=True, exist_ok=True)
        (running / "task.md").write_text(render_new_task(task_id, "Remote status worker agnostic"), encoding="utf-8")
        save_registry(
            {
                "tasks": {
                    task_id: {
                        "task_id": task_id,
                        "title": "Remote status worker agnostic",
                        "status": "running",
                        "remote": {
                            "host": "host",
                            "remote_root": "/remote/root",
                            "remote_task_dir": f"/remote/root/{task_id}",
                            "remote_out_dir": f"/remote/root/{task_id}/_worker_out",
                        },
                    }
                }
            },
            path=registry_path(),
        )

    def _cleanup_task(self, task_id: str) -> None:
        for status in ("pending", "running", "launching", "launch_failed", "launch_unknown", "completed", "partial", "failed"):
            path = task_dir(status, task_id)
            if path.exists():
                shutil.rmtree(path)
        for path in (dispatch_dir(task_id), worktree_dir(task_id)):
            if path.exists():
                shutil.rmtree(path)

    def test_remote_status_prefers_generic_pid_and_logs(self):
        task_id = f"rs_generic_{uuid.uuid4().hex[:8]}"
        original_run_ssh = core.run_ssh
        seen = {}
        try:
            self._write_task(task_id)

            def fake_run_ssh(host, command, timeout=None):
                seen["command"] = command
                return {"returncode": 0, "stdout": "", "stderr": "", "command": command, "host": host}

            core.run_ssh = fake_run_ssh
            core.remote_status(task_id, host="host", remote_root="/remote/root", force_refresh=True)

            command = seen["command"]
            self.assertIn("runner.pid worker.pid execution.pid codex.pid", command)
            self.assertIn("stdout.log codex_stdout.log runner_stdout.log", command)
            self.assertIn("stderr.log codex_stderr.log runner_stderr.log", command)
            self.assertIn('tail -n 40 "$stderr_file"', command)
            self.assertIn('tail -n 40 "$stdout_file"', command)
        finally:
            core.run_ssh = original_run_ssh
            self._cleanup_task(task_id)

    def test_remote_status_bulk_snapshot_uses_generic_logs(self):
        source = core.remote_status_bulk.__code__.co_consts
        script = "\n".join(str(item) for item in source if isinstance(item, str))
        self.assertIn('"stdout.log"', script)
        self.assertIn('"stderr.log"', script)
        self.assertIn('"execution.pid"', script)
        self.assertIn('"codex_stdout.log"', script)
        self.assertIn('"codex_stderr.log"', script)

    def test_run_remote_materialized_execution_generates_fixed_runner(self):
        task_id = f"rr_mat_{uuid.uuid4().hex[:8]}"
        try:
            pending = task_dir("pending", task_id)
            pending.mkdir(parents=True, exist_ok=True)
            (pending / "task.md").write_text(
                render_new_task(task_id, "Remote materialized execution", worker="materialized_execution"),
                encoding="utf-8",
            )
            save_registry(
                {
                    "tasks": {
                        task_id: {
                            "task_id": task_id,
                            "title": "Remote materialized execution",
                            "status": "pending",
                        }
                    }
                },
                path=registry_path(),
            )

            with WorkspaceTempDir() as tmp:
                with (
                    patch("solution_agent.orchestrator.core.remote_bootstrap_context", return_value={"ok": True}),
                    patch("solution_agent.orchestrator.core.scp_to_remote", return_value={"returncode": 0, "stdout": "", "stderr": ""}),
                    patch("solution_agent.orchestrator.core.run_ssh", return_value={"returncode": 0, "stdout": "", "stderr": ""}),
                    patch("solution_agent.orchestrator.core.state_dir", return_value=tmp / "state"),
                ):
                    result = core.run_remote(
                        task_id,
                        host="EXAMPLE_WORKER_B",
                        remote_root="/srv/example-project/outputs/lab-runs",
                        remote_workdir="/srv/example-project",
                        worker="materialized_execution",
                        detach=True,
                    )
            script = (dispatch_dir(task_id) / "remote_runner.sh").read_text(encoding="utf-8")
            self.assertIn("solution_agent.orchestrator.materialized_runner", script)
            self.assertIn("--task-path", script)
            self.assertIn("--out-dir", script)
            self.assertIn("--workspace-root", script)
            self.assertIn("execution.pid", script)
            self.assertIn("heartbeat.json", script)
            self.assertIn("PYTHONPATH", script)
            self.assertIn("conda run -n py_torch python", script)
            self.assertIn("remote_execution_env.json", script)
            self.assertIn("gpu_sample()", script)
            self.assertIn("gpu_usage.jsonl", script)
            self.assertIn("gpu_usage_summary.json", script)
            self.assertIn("gpu_mem_peak_mb", script)
            self.assertIn('"gpu_mem_used_mb"', script)
            self.assertIn("SOLUTION_AGENT_RUN_AUTH_SIGNATURE", script)
            self.assertIn("remote auth handshake failed", script)
            self.assertIn("hmac.new", script)
            self.assertNotIn("export SOLUTION_AGENT_REMOTE_AUTH_TOKEN", script)
            self.assertNotIn('. "$AUTH_ENV"', script)

            bundle = dispatch_dir(task_id) / "launch_bundle.tar.gz"
            with tarfile.open(bundle, "r:gz") as archive:
                names = set(archive.getnames())
            self.assertIn("task.md", names)
            self.assertIn("remote_runner.sh", names)
            self.assertIn("input_bindings.yaml", names)
            self.assertIn("solution_agent", names)
            self.assertIn("task.md", result["launch_bundle"]["contains"])
            self.assertIn("solution_agent", result["launch_bundle"]["contains"])
            self.assertEqual(result["remote"]["remote_execution_env"]["mode"], "conda")
        finally:
            self._cleanup_task(task_id)

    def test_run_remote_materialized_execution_supports_conda_prefix_profiles(self):
        task_id = f"rr_mat_prefix_{uuid.uuid4().hex[:8]}"
        try:
            pending = task_dir("pending", task_id)
            pending.mkdir(parents=True, exist_ok=True)
            (pending / "task.md").write_text(
                render_new_task(task_id, "Remote materialized execution prefix", worker="materialized_execution"),
                encoding="utf-8",
            )
            save_registry(
                {
                    "tasks": {
                        task_id: {
                            "task_id": task_id,
                            "title": "Remote materialized execution prefix",
                            "status": "pending",
                        }
                    }
                },
                path=registry_path(),
            )

            with WorkspaceTempDir() as tmp:
                with (
                    patch("solution_agent.orchestrator.core.remote_bootstrap_context", return_value={"ok": True}),
                    patch("solution_agent.orchestrator.core.scp_to_remote", return_value={"returncode": 0, "stdout": "", "stderr": ""}),
                    patch("solution_agent.orchestrator.core.run_ssh", return_value={"returncode": 0, "stdout": "", "stderr": ""}),
                    patch("solution_agent.orchestrator.core.state_dir", return_value=tmp / "state"),
                ):
                    result = core.run_remote(
                        task_id,
                        host="EXAMPLE_WORKER_A",
                        remote_root="/srv/example-workspaces-b/loupe_workspace/runs",
                        remote_workdir="/srv/example-workspaces-b/loupe_workspace/Loupe_agent",
                        worker="materialized_execution",
                        detach=True,
                    )

            script = (dispatch_dir(task_id) / "remote_runner.sh").read_text(encoding="utf-8")
            self.assertIn("conda run -p /srv/example-workspaces-b/envs/loupe python", script)
            self.assertEqual(result["remote"]["remote_execution_env"]["conda_prefix"], "/srv/example-workspaces-b/envs/loupe")
            self.assertIsNone(result["remote"]["remote_execution_env"]["conda_env"])
        finally:
            self._cleanup_task(task_id)

    def test_run_remote_launch_failure_moves_task_and_marks_unreachable(self):
        task_id = f"rr_fail_{uuid.uuid4().hex[:8]}"
        try:
            pending = task_dir("pending", task_id)
            pending.mkdir(parents=True, exist_ok=True)
            (pending / "task.md").write_text(
                render_new_task(task_id, "Remote launch failure", worker="materialized_execution"),
                encoding="utf-8",
            )
            save_registry(
                {
                    "tasks": {
                        task_id: {
                            "task_id": task_id,
                            "title": "Remote launch failure",
                            "status": "pending",
                        }
                    }
                },
                path=registry_path(),
            )

            with (
                patch("solution_agent.orchestrator.core.remote_bootstrap_context", return_value={"ok": True}),
                patch("solution_agent.orchestrator.core.scp_to_remote", return_value={"returncode": 255, "stdout": "", "stderr": "ssh failed"}),
                patch("solution_agent.orchestrator.core.run_ssh", return_value={"returncode": 255, "stdout": "", "stderr": "ssh failed"}),
            ):
                core.run_remote(
                    task_id,
                    host="EXAMPLE_WORKER_B",
                    remote_root="/srv/example-project/outputs/lab-runs",
                    remote_workdir="/srv/example-project",
                    worker="materialized_execution",
                    detach=True,
                )

            record = load_registry()["tasks"][task_id]
            self.assertEqual(record["status"], "launch_failed")
            self.assertEqual(record["remote_health"], "unreachable")
            self.assertEqual(record["remote_run"]["launch_reason"], "ssh_unreachable")
            self.assertTrue((task_dir("launch_failed", task_id) / "task.md").exists())
            self.assertFalse((task_dir("running", task_id) / "task.md").exists())
        finally:
            self._cleanup_task(task_id)

    def test_run_remote_bootstrap_blocked_stays_pending_and_does_not_launch(self):
        task_id = f"rr_bootstrap_block_{uuid.uuid4().hex[:8]}"
        try:
            pending = task_dir("pending", task_id)
            pending.mkdir(parents=True, exist_ok=True)
            (pending / "task.md").write_text(
                render_new_task(task_id, "Remote bootstrap block", worker="materialized_execution"),
                encoding="utf-8",
            )
            save_registry(
                {
                    "tasks": {
                        task_id: {
                            "task_id": task_id,
                            "title": "Remote bootstrap block",
                            "status": "pending",
                        }
                    }
                },
                path=registry_path(),
            )

            calls = {"copy": 0, "ssh": 0}

            def fake_remote_bootstrap(*_args, **_kwargs):
                return {
                    "blocked": True,
                    "reason": "remote_context_required_files_missing",
                    "remote_context_dir": "/remote/.codex-orchestrator",
                    "remote_runtime_dir": "/remote/.codex-orchestrator/runtime",
                    "missing_context_files": [
                        {"name": "scheduler_optimization_notes", "path": "missing.md"},
                    ],
                }

            def fake_scp(*_args, **_kwargs):
                calls["copy"] += 1
                return {"returncode": 0, "stdout": "", "stderr": ""}

            def fake_ssh(*_args, **_kwargs):
                calls["ssh"] += 1
                return {"returncode": 0, "stdout": "", "stderr": ""}

            with (
                patch("solution_agent.orchestrator.core.remote_bootstrap_context", side_effect=fake_remote_bootstrap),
                patch("solution_agent.orchestrator.core.scp_to_remote", side_effect=fake_scp),
                patch("solution_agent.orchestrator.core.run_ssh", side_effect=fake_ssh),
            ):
                result = core.run_remote(
                    task_id,
                    host="EXAMPLE_WORKER_B",
                    remote_root="/srv/example-project/outputs/lab-runs",
                    remote_workdir="/srv/example-project",
                    worker="materialized_execution",
                    detach=True,
                )

            record = load_registry()["tasks"][task_id]
            self.assertEqual(record["status"], "launch_failed")
            self.assertEqual(record["remote_health"], "degraded")
            self.assertEqual(record["remote_run"]["launch_reason"], "bootstrap_blocked: remote_context_required_files_missing")
            self.assertEqual(record["remote_run"]["run"]["returncode"], 1)
            self.assertEqual(record["remote_run"]["run"]["status"], "blocked")
            self.assertEqual(result["run"]["status"], "blocked")
            self.assertEqual(calls["copy"], 0)
            self.assertEqual(calls["ssh"], 0)
            self.assertTrue((task_dir("launch_failed", task_id) / "task.md").exists())
        finally:
            self._cleanup_task(task_id)

    def test_remote_bootstrap_context_uses_unique_remote_bundle(self):
        cache_path = state_dir() / "remote_bootstrap_cache.json"
        cache_backup = cache_path.read_text(encoding="utf-8") if cache_path.exists() else None
        seen_remote_paths = []
        seen_local_paths = []
        seen_commands = []
        remote_workdir = f"/remote/workdir/{uuid.uuid4().hex[:8]}"

        def fake_scp_to_remote(local_path, host, remote_path):
            seen_local_paths.append(local_path)
            seen_remote_paths.append(remote_path)
            return {"returncode": 0, "stdout": "", "stderr": ""}

        def fake_run_ssh(host, command, timeout=None):
            seen_commands.append(command)
            return {"returncode": 0, "stdout": "", "stderr": "", "command": command, "host": host}

        try:
            with (
                patch("solution_agent.orchestrator.core.scp_to_remote", side_effect=fake_scp_to_remote),
                patch("solution_agent.orchestrator.core.run_ssh", side_effect=fake_run_ssh),
            ):
                result = core.remote_bootstrap_context("host", remote_workdir)

            remote_bundle = result["context_bundle"]["remote"]
            self.assertEqual(seen_remote_paths, [remote_bundle])
            self.assertIn(remote_bundle, seen_commands[0])
            self.assertRegex(remote_bundle, r"/tmp/solution_agent_context_[0-9a-f]{40}_[0-9a-f]{12}_[0-9a-f]{12}\.tar\.gz")
            self.assertEqual(result["remote_runtime_dir"], remote_workdir.rstrip("/") + "/.codex-orchestrator/runtime")
            self.assertIn("runtime/scripts/probes/run_probe.py", result["context_bundle"]["contains"])
            self.assertIn("runtime/scripts/loupe/run_seg_overfit.py", result["context_bundle"]["contains"])
            self.assertIn("maintenance_policy_v1.md", result["context_bundle"]["contains"])
            self.assertIn("scheduler_optimization_notes.md", result["context_bundle"]["contains"])
            self.assertIn("opencoder_task_execution_guide.md", result["context_bundle"]["contains"])
            self.assertIn("inbox_task_format.md", result["context_bundle"]["contains"])
            with tarfile.open(seen_local_paths[0], "r:gz") as archive:
                names = set(archive.getnames())
            self.assertIn("maintenance_policy_v1.md", names)
            self.assertIn("scheduler_optimization_notes.md", names)
            self.assertIn("opencoder_task_execution_guide.md", names)
            self.assertIn("inbox_task_format.md", names)
        finally:
            if cache_backup is None:
                if cache_path.exists():
                    cache_path.unlink()
            else:
                cache_path.write_text(cache_backup, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
