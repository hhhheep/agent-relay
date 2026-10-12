import unittest

from helpers import WorkspaceTempDir
from solution_agent.orchestrator.task_loader import ensure_task_front_matter, load_task, render_new_task


class TaskLoaderTests(unittest.TestCase):
    def test_rendered_task_loads(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "task.md"
            path.write_text(render_new_task("exp_test", "Example task"), encoding="utf-8")
            task = load_task(path)

        self.assertEqual(task.task_id, "exp_test")
        self.assertEqual(task.title, "Example task")
        self.assertEqual(task.status, "pending")
        self.assertIn("result.yaml", task.expected_outputs)

    def test_plain_markdown_gets_front_matter_for_ingest(self):
        with WorkspaceTempDir() as tmp:
            path = tmp / "plain_task.md"
            path.write_text("# Do work\n\nRun the thing.", encoding="utf-8")
            wrapped = ensure_task_front_matter(path)
            wrapped_path = tmp / "wrapped.md"
            wrapped_path.write_text(wrapped, encoding="utf-8")
            task = load_task(wrapped_path)

        self.assertEqual(task.task_id, "plain_task")
        self.assertEqual(task.status, "pending")


if __name__ == "__main__":
    unittest.main()
