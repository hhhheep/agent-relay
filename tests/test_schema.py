import unittest

from solution_agent.orchestrator.schema import worker_result_from_dict


class SchemaTests(unittest.TestCase):
    def test_worker_result_validation_accepts_success(self):
        result = worker_result_from_dict(
            {
                "task_id": "exp_test",
                "status": "success",
                "worker": "codex",
                "summary_short": "done",
            }
        )

        self.assertEqual(result.status, "success")

    def test_worker_result_validation_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            worker_result_from_dict({"task_id": "exp_test", "status": "weird", "worker": "codex"})


if __name__ == "__main__":
    unittest.main()
