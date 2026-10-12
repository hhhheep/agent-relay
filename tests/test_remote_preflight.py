from types import SimpleNamespace
from pathlib import Path
import tempfile
from unittest import TestCase, mock

from solution_agent.orchestrator import core


class RemotePreflightTests(TestCase):
    def test_remote_preflight_validates_profile_and_gpu_ids(self):
        ssh_g = SimpleNamespace(
            returncode=0,
            stdout="hostname worker.example.invalid\nuser agent-user\nport 95\n",
            stderr="",
        )
        remote_stdout = "\n".join(
            [
                "hostname=DGX-Station-A100",
                "uid=1001",
                "user=agent-user",
                "workdir_exists=yes",
                "root_exists=yes",
                "root_writable=yes",
                "python3=yes",
                "tmux=yes",
                "nvidia_smi=yes",
                "project_marker=yes",
                "__gpu_begin__",
                "0, GPU-0000, NVIDIA A100",
                "1, GPU-1111, NVIDIA A100",
                "2, GPU-2222, NVIDIA A100",
                "4, GPU-4444, NVIDIA A100",
                "__gpu_end__",
            ]
        )
        remote_result = {"returncode": 0, "stdout": remote_stdout, "stderr": ""}

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(core, "_last_remote_preflight_path", return_value=Path(tmpdir) / "last_remote_preflight.json"):
                with mock.patch.object(core.shutil, "which", return_value="ssh"):
                    with mock.patch.object(core.subprocess, "run", return_value=ssh_g):
                        with mock.patch.object(core, "run_ssh", return_value=remote_result):
                            result = core.remote_target_preflight(
                                host="EXAMPLE_WORKER_B",
                                remote_workdir="/srv/example-project",
                                remote_root="/srv/example-project/outputs/lab-runs",
                                allowed_gpu_ids=["0", "1", "2", "4"],
                            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["ssh"]["port"], "95")
        self.assertIn("EXAMPLE_WORKER_B", result["profile_id"])
        self.assertEqual([gpu["index"] for gpu in result["gpus"]], ["0", "1", "2", "4"])

    def test_remote_preflight_fails_when_allowed_gpu_missing(self):
        ssh_g = SimpleNamespace(returncode=0, stdout="hostname worker.example.invalid\nport 22\n", stderr="")
        remote_stdout = "\n".join(
            [
                "uid=1001",
                "workdir_exists=yes",
                "root_exists=yes",
                "root_writable=yes",
                "python3=yes",
                "tmux=yes",
                "nvidia_smi=yes",
                "project_marker=yes",
                "__gpu_begin__",
                "0, GPU-0000, NVIDIA A100",
                "__gpu_end__",
            ]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(core, "_last_remote_preflight_path", return_value=Path(tmpdir) / "last_remote_preflight.json"):
                with mock.patch.object(core.shutil, "which", return_value="ssh"):
                    with mock.patch.object(core.subprocess, "run", return_value=ssh_g):
                        with mock.patch.object(core, "run_ssh", return_value={"returncode": 0, "stdout": remote_stdout, "stderr": ""}):
                            result = core.remote_target_preflight(
                                host="EXAMPLE_WORKER_B",
                                remote_workdir="/srv/example-project",
                                remote_root="/srv/example-project/outputs/lab-runs",
                                allowed_gpu_ids=["0", "4"],
                            )

        self.assertFalse(result["ok"])
        self.assertIn("configured GPU ids not visible", "; ".join(result["errors"]))

    def test_remote_preflight_classifies_unreadable_identity_as_local_transport_issue(self):
        ssh_g = SimpleNamespace(
            returncode=0,
            stdout=(
                "hostname 203.0.113.10\n"
                "user agent-user\n"
                "port 7735\n"
                "identityfile C:\\Users\\example-user\\.ssh\\id_ed25519_worker1\n"
            ),
            stderr="",
        )
        remote_result = {
            "returncode": 255,
            "stdout": "",
            "stderr": (
                "Warning: Identity file C:\\Users\\example-user\\.ssh\\id_ed25519_worker1 "
                "not accessible: Permission denied.\n"
                "agent-user@203.0.113.10: Permission denied (publickey)."
            ),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(core, "_last_remote_preflight_path", return_value=Path(tmpdir) / "last_remote_preflight.json"):
                with mock.patch.object(core.shutil, "which", return_value="ssh"):
                    with mock.patch.object(core.subprocess, "run", return_value=ssh_g):
                        with mock.patch.object(core, "run_ssh", return_value=remote_result):
                            result = core.remote_target_preflight(
                                host="EXAMPLE_WORKER_A",
                                remote_workdir="/srv/example-workspaces-b/B14_agent_handoff_20260521",
                                remote_root="/srv/example-workspaces-b/B14_agent_handoff_20260521/runs",
                                allowed_gpu_ids=["0"],
                            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["transport"]["status"], "local_transport_auth_unavailable")
        self.assertEqual(
            result["transport"]["reason_code"],
            "ssh_identity_file_unreadable_from_current_context",
        )
        self.assertFalse(result["transport"]["automation_retry_safe"])
        self.assertIn("launch packet", result["transport"]["recovery_action"])

    def test_remote_preflight_classifies_unresolved_alias_in_sandbox_context(self):
        ssh_g = SimpleNamespace(
            returncode=0,
            stdout="hostname EXAMPLE_WORKER_A\nuser codexsandboxoffline\nport 22\n",
            stderr="",
        )
        remote_result = {"returncode": 255, "stdout": "", "stderr": "ssh: Could not resolve hostname example_worker_a"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(core, "_last_remote_preflight_path", return_value=Path(tmpdir) / "last_remote_preflight.json"):
                with mock.patch.object(core.shutil, "which", return_value="ssh"):
                    with mock.patch.object(core.subprocess, "run", return_value=ssh_g):
                        with mock.patch.object(core, "run_ssh", return_value=remote_result):
                            result = core.remote_target_preflight(
                                host="EXAMPLE_WORKER_A",
                                remote_workdir="/srv/example-workspaces-b/B14_agent_handoff_20260521",
                                remote_root="/srv/example-workspaces-b/B14_agent_handoff_20260521/runs",
                                allowed_gpu_ids=["0"],
                            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["transport"]["status"], "local_transport_profile_unresolved")
        self.assertEqual(result["transport"]["reason_code"], "ssh_alias_not_resolved_in_current_context")
        self.assertFalse(result["transport"]["automation_retry_safe"])
