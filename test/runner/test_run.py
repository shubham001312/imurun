"""Batch H: the strict runner refuses empty runs and hidden skips."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("imu_test_run", ROOT / "test" / "run.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class VerdictTests(unittest.TestCase):
    def test_a_green_run_passes(self):
        self.assertEqual(RUNNER.verdict(165, 0, 0, 0), "")

    def test_zero_tests_is_a_failure(self):
        self.assertIn("no tests", RUNNER.verdict(0, 0, 0, 0))

    def test_failures_are_a_failure(self):
        self.assertIn("failure", RUNNER.verdict(10, 1, 0, 0))

    def test_errors_are_a_failure(self):
        self.assertIn("error", RUNNER.verdict(10, 0, 1, 0))

    def test_a_skip_is_a_failure_by_default(self):
        self.assertIn("skipped", RUNNER.verdict(10, 0, 0, 1))

    def test_a_skip_is_accepted_only_with_the_flag(self):
        self.assertEqual(RUNNER.verdict(10, 0, 0, 1, allow_skip=True), "")

    def test_failures_win_over_the_skip_flag(self):
        self.assertIn("failure", RUNNER.verdict(10, 1, 0, 1, allow_skip=True))


if __name__ == "__main__":
    unittest.main()
