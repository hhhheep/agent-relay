import json
import unittest

from helpers import WorkspaceTempDir
from solution_agent.orchestrator.reporting import write_task_report
from solution_agent.orchestrator.schema import TaskBook, WorkerResult


class ReportWriterTests(unittest.TestCase):
    def test_report_writer_creates_markdown(self):
        with WorkspaceTempDir() as tmp:
            result_dir = tmp / "result"
            result_dir.mkdir()
            (result_dir / "metrics.json").write_text(json.dumps({"accuracy": 0.9}), encoding="utf-8")
            (result_dir / "worker_notes.md").write_text("Changed the thing.", encoding="utf-8")
            (result_dir / "stdout.log").write_text("ok", encoding="utf-8")
            (result_dir / "stderr.log").write_text("", encoding="utf-8")
            task = TaskBook(task_id="exp_test", title="Example", objective="Try it")
            result = WorkerResult(task_id="exp_test", status="success", worker="codex", summary_short="done")

            report = write_task_report(result_dir / "report.md", task, result, result_dir, diff_stat="clean")
            text = report.read_text(encoding="utf-8")

        self.assertIn("# exp_test - Example", text)
        self.assertIn("accuracy: 0.9", text)
        self.assertIn("## Completion Check", text)
        self.assertIn("## Variant Results", text)

    def test_report_writer_formats_variant_metrics(self):
        with WorkspaceTempDir() as tmp:
            result_dir = tmp / "result"
            result_dir.mkdir()
            (result_dir / "metrics.json").write_text(
                json.dumps(
                    {
                        "evaluation": {"checkpoints": ["S4", "S4_Delta89_HF_HT"]},
                        "single_checkpoint_metrics": [
                            {"variant": "S4", "DF40_AUC": 0.85, "NTIRE_AUC": 0.79, "NTIRE_TP": 42, "NTIRE_FP": 22},
                            {
                                "variant": "S4_Delta89_HF_HT",
                                "DF40_AUC": 0.86,
                                "NTIRE_AUC": 0.80,
                                "NTIRE_TP": 41,
                                "NTIRE_FP": 20,
                            },
                        ],
                        "candidate_roles": [{"variant": "S4_Delta89_HF_HT", "role_label": "dead", "reasons": ["no rescue"]}],
                    }
                ),
                encoding="utf-8",
            )
            (result_dir / "worker_notes.md").write_text("## Key findings\n\n- useful\n", encoding="utf-8")
            (result_dir / "stdout.log").write_text("ok", encoding="utf-8")
            (result_dir / "stderr.log").write_text("", encoding="utf-8")
            task = TaskBook(
                task_id="exp_test",
                title="Example",
                objective="Try it",
                body="Priority variants:\n1. `S4_Delta89_HF_HT`\n2. `P2_R8_B10B11_HF`",
            )
            result = WorkerResult(task_id="exp_test", status="partial", worker="codex", summary_short="done")

            report = write_task_report(result_dir / "report.md", task, result, result_dir, diff_stat="clean")
            text = report.read_text(encoding="utf-8")

        self.assertIn("| S4_Delta89_HF_HT | done | dead |", text)
        self.assertIn("| P2_R8_B10B11_HF | not run |  |", text)
        self.assertIn("| S4_Delta89_HF_HT | dead | 0.8600 | 0.8000 | 41 | 20 | no rescue |", text)


if __name__ == "__main__":
    unittest.main()
