from unittest import TestCase, mock
from pathlib import Path

from solution_agent.orchestrator import core
from tests.helpers import WorkspaceTempDir


class WorkplaceInitTests(TestCase):
    def test_workplace_init_writes_local_report_without_remote_sync(self):
        preflight = {
            "ok": True,
            "checked_at": "2026-04-30T00:00:00+08:00",
            "host": "test_host",
            "remote_workdir": "/tmp/project",
            "remote_root": "/tmp/project/outputs/lab-runs",
            "profile_id": "test_host__p22__tmp_project__uid1000",
            "ssh": {"hostname": "example", "port": "22"},
            "remote": {"hostname": "remote", "uid": "1000", "user": "tester"},
            "gpus": [{"index": "0", "uuid": "GPU-test", "name": "NVIDIA A100"}],
            "warnings": [],
        }
        stdout = "\n".join(
            [
                "bash_path=/usr/bin/bash",
                "tar_path=/usr/bin/tar",
                "gzip_path=/usr/bin/gzip",
                "python3_path=/usr/bin/python3",
                "git_path=/usr/bin/git",
                "nvidia_smi_path=/usr/bin/nvidia-smi",
                "codex_path=/home/tester/.local/bin/codex",
                "codex_version=codex-cli 0.125.0",
                "tmux_path=/usr/bin/tmux",
                "conda_path=/home/tester/miniconda3/bin/conda",
                "rsync_path=/usr/bin/rsync",
                "git_inside_worktree=yes",
                "git_branch=main",
                "git_head=abc1234",
                "git_dirty_count=0",
            ]
        )
        with mock.patch.object(core, "remote_target_preflight", return_value=preflight):
            with mock.patch.object(core, "run_ssh", return_value={"returncode": 0, "stdout": stdout, "stderr": ""}):
                result = core.remote_workplace_init(
                    host="test_host",
                    remote_workdir="/tmp/project",
                    remote_root="/tmp/project/outputs/lab-runs",
                    allowed_gpu_ids=["0"],
                    write_marker=False,
                    sync_profile=False,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["missing_required"], [])
        self.assertIn("preflight.md", result["local_paths"]["report"])
        self.assertIn("workplace_profile.json", result["local_paths"]["profile"])
        self.assertIn("agent_context_yaml", result["local_paths"])
        self.assertEqual(result["agent_context"]["purpose"], "sanitized token-bounded workplace summary for context packs and agent bootstrapping")
        self.assertTrue(any(item["code"] == "tool:python3" and item["status"] == "ok" for item in result["readiness_checks"]))

    def test_workplace_init_treats_missing_codex_as_optional(self):
        preflight = {
            "ok": True,
            "profile_id": "test_host__p22__tmp_project__uid1000",
            "ssh": {"hostname": "example", "port": "22"},
            "remote": {"hostname": "remote", "uid": "1000", "user": "tester"},
            "gpus": [],
            "warnings": [],
        }
        stdout = "\n".join(
            [
                "bash_path=/usr/bin/bash",
                "tar_path=/usr/bin/tar",
                "gzip_path=/usr/bin/gzip",
                "python3_path=/usr/bin/python3",
                "git_path=/usr/bin/git",
                "nvidia_smi_path=/usr/bin/nvidia-smi",
                "codex_path=",
            ]
        )
        with mock.patch.object(core, "remote_target_preflight", return_value=preflight):
            with mock.patch.object(core, "run_ssh", return_value={"returncode": 0, "stdout": stdout, "stderr": ""}):
                result = core.remote_workplace_init(
                    host="test_host",
                    remote_workdir="/tmp/project",
                    remote_root="/tmp/project/outputs/lab-runs",
                    allowed_gpu_ids=["0"],
                    write_marker=False,
                    sync_profile=False,
                )

        self.assertTrue(result["ok"])
        self.assertIn("codex", result["missing_recommended"])
        self.assertTrue(any("Codex CLI is missing" in item for item in result["suggestions"]))

    def test_workplace_init_reports_configured_conda_env_missing(self):
        preflight = {
            "ok": True,
            "profile_id": "test_host__p22__tmp_project__uid1000",
            "ssh": {"hostname": "example", "port": "22"},
            "remote": {"hostname": "remote", "uid": "1000", "user": "tester"},
            "gpus": [],
            "warnings": [],
        }
        stdout = "\n".join(
            [
                "bash_path=/usr/bin/bash",
                "tar_path=/usr/bin/tar",
                "gzip_path=/usr/bin/gzip",
                "python3_path=/usr/bin/python3",
                "git_path=/usr/bin/git",
                "nvidia_smi_path=/usr/bin/nvidia-smi",
                "codex_path=",
                "conda_path=/home/tester/miniconda3/bin/conda",
                "configured_conda_env=missing_env",
                "configured_conda_env_ready=no:EnvironmentNameNotFound",
            ]
        )
        with mock.patch.object(core, "remote_target_preflight", return_value=preflight):
            with mock.patch.object(core, "_remote_execution_profile_for", return_value={"conda": {"env": "missing_env"}}):
                with mock.patch.object(core, "run_ssh", return_value={"returncode": 0, "stdout": stdout, "stderr": ""}):
                    result = core.remote_workplace_init(
                        host="test_host",
                        remote_workdir="/tmp/project",
                        remote_root="/tmp/project/outputs/lab-runs",
                        allowed_gpu_ids=["0"],
                        write_marker=False,
                        sync_profile=False,
                    )

        self.assertFalse(result["ok"])
        self.assertIn("conda_env:missing_env", result["missing_required"])
        self.assertTrue(any(item.get("code") == "configured_conda_env_missing" for item in result["install_guidance"]))
        self.assertTrue(any(item["code"] == "conda_env" and item["status"] == "fail" for item in result["readiness_checks"]))

    def test_workplace_init_syncs_remote_materializer_env(self):
        preflight = {
            "ok": True,
            "checked_at": "2026-04-30T00:00:00+08:00",
            "host": "test_host",
            "remote_workdir": "/tmp/project",
            "remote_root": "/tmp/project/outputs/lab-runs",
            "profile_id": "test_host__p22__tmp_project__uid1000",
            "ssh": {"hostname": "example", "port": "22"},
            "remote": {"hostname": "remote", "uid": "1000", "user": "tester"},
            "gpus": [],
            "warnings": [],
        }
        stdout = "\n".join(
            [
                "bash_path=/usr/bin/bash",
                "tar_path=/usr/bin/tar",
                "gzip_path=/usr/bin/gzip",
                "python3_path=/usr/bin/python3",
                "git_path=/usr/bin/git",
                "nvidia_smi_path=/usr/bin/nvidia-smi",
                "codex_path=/home/tester/.local/bin/codex",
                "codex_version=codex-cli 0.125.0",
            ]
        )

        def fake_run_ssh(host, command, timeout=None):
            if "bash_path=" in command:
                return {"returncode": 0, "stdout": stdout, "stderr": "", "command": command, "host": host}
            return {"returncode": 0, "stdout": "", "stderr": "", "command": command, "host": host}

        with WorkspaceTempDir() as tmp:
            env_path = tmp / ".env"
            env_path.write_text("DUMMY_KEY=redacted\n", encoding="utf-8")
            copied_texts = []

            def fake_scp_to_remote(local_path, host, remote_path):
                if remote_path == ".remote_materializer/.env.tmp":
                    copied_texts.append(Path(local_path).read_text(encoding="utf-8"))
                return {"returncode": 0, "stdout": "", "stderr": ""}

            with mock.patch.object(core, "remote_target_preflight", return_value=preflight):
                with mock.patch.object(core, "remote_bootstrap_context", return_value={"skipped": False}):
                    with mock.patch.object(core, "_local_remote_materializer_env_path", return_value=env_path):
                        with mock.patch.object(core, "state_dir", return_value=tmp / "state"):
                            with mock.patch.object(core, "run_ssh", side_effect=fake_run_ssh):
                                with mock.patch.object(core, "scp_to_remote", side_effect=fake_scp_to_remote) as scp_mock:
                                    result = core.remote_workplace_init(
                                        host="test_host",
                                        remote_workdir="/tmp/project",
                                        remote_root="/tmp/project/outputs/lab-runs",
                                        allowed_gpu_ids=["0"],
                                        write_marker=False,
                                        sync_profile=True,
                                        sync_materializer_env=True,
                                    )

        self.assertTrue(result["ok"])
        self.assertTrue(result["remote_materializer_env"]["ready"])
        self.assertEqual(result["remote_materializer_env"]["remote"], "~/.remote_materializer/.env")
        copied_text = copied_texts[0]
        self.assertIn("DUMMY_KEY=redacted", copied_text)
        self.assertIn("SOLUTION_AGENT_REMOTE_AUTH_TOKEN=", copied_text)
        self.assertIn("auth", result["remote_materializer_env"])
        self.assertTrue(any(call.args[2] == ".remote_materializer/.env.tmp" for call in scp_mock.call_args_list))
        self.assertTrue(any(item["code"] == "remote_materializer_auth" and item["status"] == "ok" for item in result["readiness_checks"]))

    def test_workplace_init_no_sync_checks_existing_remote_materializer_env(self):
        preflight = {
            "ok": True,
            "profile_id": "test_host__p22__tmp_project__uid1000",
            "ssh": {"hostname": "example", "port": "22"},
            "remote": {"hostname": "remote", "uid": "1000", "user": "tester"},
            "gpus": [],
            "warnings": [],
        }
        stdout = "\n".join(
            [
                "bash_path=/usr/bin/bash",
                "tar_path=/usr/bin/tar",
                "gzip_path=/usr/bin/gzip",
                "python3_path=/usr/bin/python3",
                "git_path=/usr/bin/git",
                "nvidia_smi_path=/usr/bin/nvidia-smi",
                "codex_path=",
            ]
        )

        def fake_run_ssh(host, command, timeout=None):
            if "bash_path=" in command:
                return {"returncode": 0, "stdout": stdout, "stderr": "", "command": command, "host": host}
            if ".remote_materializer/.env" in command:
                return {"returncode": 0, "stdout": "", "stderr": "", "command": command, "host": host}
            return {"returncode": 0, "stdout": "", "stderr": "", "command": command, "host": host}

        with WorkspaceTempDir() as tmp:
            with mock.patch.object(core, "remote_target_preflight", return_value=preflight):
                with mock.patch.object(core, "remote_bootstrap_context", return_value={"skipped": False}):
                    with mock.patch.object(core, "state_dir", return_value=tmp / "state"):
                        with mock.patch.object(core, "run_ssh", side_effect=fake_run_ssh):
                            result = core.remote_workplace_init(
                                host="test_host",
                                remote_workdir="/tmp/project",
                                remote_root="/tmp/project/outputs/lab-runs",
                                allowed_gpu_ids=["0"],
                                write_marker=False,
                                sync_profile=True,
                                sync_materializer_env=False,
                            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["remote_materializer_env"]["ready"])
        self.assertTrue(result["remote_materializer_env"]["skipped"])
        self.assertTrue(any(item["code"] == "remote_materializer_auth" and item["status"] == "ok" for item in result["readiness_checks"]))

    def test_workplace_init_marks_missing_remote_materializer_env_required(self):
        preflight = {
            "ok": True,
            "profile_id": "test_host__p22__tmp_project__uid1000",
            "ssh": {"hostname": "example", "port": "22"},
            "remote": {"hostname": "remote", "uid": "1000", "user": "tester"},
            "gpus": [],
            "warnings": [],
        }
        stdout = "\n".join(
            [
                "bash_path=/usr/bin/bash",
                "tar_path=/usr/bin/tar",
                "gzip_path=/usr/bin/gzip",
                "python3_path=/usr/bin/python3",
                "git_path=/usr/bin/git",
                "nvidia_smi_path=/usr/bin/nvidia-smi",
                "codex_path=",
            ]
        )

        def fake_run_ssh(host, command, timeout=None):
            if "bash_path=" in command:
                return {"returncode": 0, "stdout": stdout, "stderr": "", "command": command, "host": host}
            if ".remote_materializer/.env" in command:
                return {"returncode": 1, "stdout": "", "stderr": "", "command": command, "host": host}
            return {"returncode": 0, "stdout": "", "stderr": "", "command": command, "host": host}

        with WorkspaceTempDir() as tmp:
            missing_env = tmp / ".env"
            with mock.patch.object(core, "remote_target_preflight", return_value=preflight):
                with mock.patch.object(core, "remote_bootstrap_context", return_value={"skipped": False}):
                    with mock.patch.object(core, "_local_remote_materializer_env_path", return_value=missing_env):
                        with mock.patch.object(core, "state_dir", return_value=tmp / "state"):
                            with mock.patch.object(core, "run_ssh", side_effect=fake_run_ssh):
                                result = core.remote_workplace_init(
                                    host="test_host",
                                    remote_workdir="/tmp/project",
                                    remote_root="/tmp/project/outputs/lab-runs",
                                    allowed_gpu_ids=["0"],
                                    write_marker=False,
                                    sync_profile=True,
                                )

        self.assertFalse(result["ok"])
        self.assertIn("remote_materializer_env", result["missing_required"])
        self.assertTrue(any(item.get("code") == "remote_materializer_env_missing" for item in result["install_guidance"]))
        self.assertTrue(any(item["code"] == "remote_materializer_auth" and item["status"] == "fail" for item in result["readiness_checks"]))

    def test_workplace_init_marks_remote_materializer_auth_mismatch(self):
        preflight = {
            "ok": True,
            "profile_id": "test_host__p22__tmp_project__uid1000",
            "ssh": {"hostname": "example", "port": "22"},
            "remote": {"hostname": "remote", "uid": "1000", "user": "tester"},
            "gpus": [],
            "warnings": [],
        }
        stdout = "\n".join(
            [
                "bash_path=/usr/bin/bash",
                "tar_path=/usr/bin/tar",
                "gzip_path=/usr/bin/gzip",
                "python3_path=/usr/bin/python3",
                "git_path=/usr/bin/git",
                "nvidia_smi_path=/usr/bin/nvidia-smi",
                "codex_path=",
            ]
        )

        def fake_run_ssh(host, command, timeout=None):
            if "bash_path=" in command:
                return {"returncode": 0, "stdout": stdout, "stderr": "", "command": command, "host": host}
            if "EXPECTED_SIG" in command:
                return {"returncode": 4, "stdout": "", "stderr": "", "command": command, "host": host}
            return {"returncode": 0, "stdout": "", "stderr": "", "command": command, "host": host}

        with WorkspaceTempDir() as tmp:
            with mock.patch.object(core, "remote_target_preflight", return_value=preflight):
                with mock.patch.object(core, "remote_bootstrap_context", return_value={"skipped": False}):
                    with mock.patch.object(core, "state_dir", return_value=tmp / "state"):
                        with mock.patch.object(core, "run_ssh", side_effect=fake_run_ssh):
                            result = core.remote_workplace_init(
                                host="test_host",
                                remote_workdir="/tmp/project",
                                remote_root="/tmp/project/outputs/lab-runs",
                                allowed_gpu_ids=["0"],
                                write_marker=False,
                                sync_profile=True,
                                sync_materializer_env=False,
                            )

        self.assertFalse(result["ok"])
        self.assertIn("remote_materializer_env", result["missing_required"])
        self.assertEqual(result["remote_materializer_env"]["auth"]["status"], "mismatch")
        self.assertTrue(any(item["code"] == "remote_materializer_auth" and item["status"] == "fail" for item in result["readiness_checks"]))
