import sys
import unittest

from solution_agent.utils.subprocess_utils import decode_output, run_subprocess


class SubprocessUtilsTests(unittest.TestCase):
    def test_utf8_subprocess_output_does_not_use_windows_locale(self):
        code = (
            "import sys; "
            "sys.stdout.buffer.write("
            "'\\u4e2d\\u6587 \\u2705 \\u201chello\\u201d \\u2014\\n'.encode('utf-8')"
            ")"
        )

        completed = run_subprocess([sys.executable, "-c", code])

        self.assertEqual(completed.returncode, 0)
        self.assertIn("\u4e2d\u6587", completed.stdout)
        self.assertIn("\u201chello\u201d", completed.stdout)

    def test_decode_output_falls_back_to_replacement(self):
        decoded = decode_output(b"\xff\xfe\xfa")

        self.assertTrue(decoded)


if __name__ == "__main__":
    unittest.main()
