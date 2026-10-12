import unittest

from solution_agent.orchestrator.paths import result_latest_dir, result_run_dir


class PathTests(unittest.TestCase):
    def test_result_run_paths_are_separate_from_latest(self):
        self.assertTrue(str(result_latest_dir("exp_x")).endswith("results\\exp_x\\latest") or str(result_latest_dir("exp_x")).endswith("results/exp_x/latest"))
        self.assertIn("runs", str(result_run_dir("exp_x", "run_1")))
        self.assertNotEqual(result_latest_dir("exp_x"), result_run_dir("exp_x", "run_1"))


if __name__ == "__main__":
    unittest.main()
