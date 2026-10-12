from types import SimpleNamespace
from unittest import TestCase, mock

from helpers import WorkspaceTempDir
from solution_agent.orchestrator import core


class LocalInitTests(TestCase):
    def test_local_init_writes_report_and_profile(self):
        def fake_which(command):
            return f"C:/bin/{command}.exe"

        def fake_run(command, **kwargs):
            name = str(command[0])
            return SimpleNamespace(returncode=0, stdout=f"{name} version test\n", stderr="")

        with WorkspaceTempDir() as tmp:
            with mock.patch.object(core, "_local_init_dir", return_value=tmp / "local_init"):
                with mock.patch.object(core.shutil, "which", side_effect=fake_which):
                    with mock.patch.object(core.subprocess, "run", side_effect=fake_run):
                        with mock.patch.object(core, "_path_write_probe", return_value={"path": "x", "exists": True, "writable": True}):
                            result = core.local_environment_init(create_dirs=False)

            report = result["local_paths"]["report"]
            profile = result["local_paths"]["profile"]

        self.assertTrue(result["ok"])
        self.assertIn("local", result["profile_id"])
        self.assertTrue(report.endswith("local_preflight.md"))
        self.assertTrue(profile.endswith("local_profile.json"))
        self.assertEqual(result["missing_required"], [])


if __name__ == "__main__":
    from unittest import main

    main()
