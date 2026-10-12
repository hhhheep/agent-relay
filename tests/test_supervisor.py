import unittest

from solution_agent.orchestrator.supervisor import fallback_decision


class SupervisorTests(unittest.TestCase):
    def test_fallback_continue_has_reason_code(self):
        decision = fallback_decision("running:\nyes\n")

        self.assertEqual(decision.action, "continue")
        self.assertEqual(decision.reason_code, "worker_active_progressing")

    def test_fallback_gpu_oom_waits(self):
        decision = fallback_decision("stderr_tail:\nCUDA out of memory\nrunning:\nno\n")

        self.assertEqual(decision.action, "wait_gpu")
        self.assertEqual(decision.reason_code, "gpu_oom_detected")
        self.assertGreaterEqual(decision.next_check_minutes, 30)

    def test_running_takes_precedence_over_existing_result_file(self):
        decision = fallback_decision("files:\nresult.yaml\npid:\n123\nrunning:\nyes\n")

        self.assertEqual(decision.action, "continue")
        self.assertEqual(decision.reason_code, "worker_active_progressing")

    def test_stale_heartbeat_warns(self):
        decision = fallback_decision("heartbeat_age_seconds:\n1800\nrunning:\nyes\n")

        self.assertEqual(decision.action, "warn")
        self.assertEqual(decision.reason_code, "heartbeat_stale")

    def test_negated_delete_audit_is_not_destructive_warning(self):
        decision = fallback_decision("files:\nresult.yaml\nexit_code:\n0\nrun_state:\nfinished\nstdout_tail:\nNo training, install, network, delete, or source-code modification commands were run.\n")

        self.assertEqual(decision.action, "collect")
        self.assertEqual(decision.reason_code, "pid_dead_result_valid")

    def test_partial_finish_signals_do_not_collect(self):
        decision = fallback_decision("files:\nresult.yaml\nrunning:\nno\n")

        self.assertEqual(decision.action, "codex_confirm")
        self.assertEqual(decision.reason_code, "pid_dead_result_incomplete")

    def test_finished_result_collects_even_when_exit_code_empty(self):
        decision = fallback_decision("files:\nresult.yaml\nexit_code:\n\nrun_state:\nfinished\nrunning:\nno\n")

        self.assertEqual(decision.action, "collect")
        self.assertEqual(decision.reason_code, "pid_dead_result_valid_missing_exit_code")


if __name__ == "__main__":
    unittest.main()
