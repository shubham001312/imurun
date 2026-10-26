"""Batch H: the strict runner refuses empty runs and hidden skips."""
import importlib.util
import io
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


class FakeTTY(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


class StatusTests(unittest.TestCase):
    def test_the_wheel_line_shows_progress_and_the_current_test(self):
        stream = FakeTTY()
        status = RUNNER.Status(stream=stream, enabled=True)
        status.total = 3
        status.done = 1
        status.set_test("test.mod.Case.test_x")
        status._render(0)
        output = stream.getvalue()
        self.assertIn("\r", output)
        self.assertIn(RUNNER.WHEEL[0], output)
        self.assertIn("1/3", output)
        self.assertIn("test.mod.Case.test_x", output)

    def test_a_non_tty_gets_periodic_progress(self):
        stream = io.StringIO()
        status = RUNNER.Status(stream=stream, enabled=False)
        status.start(10)
        for index in range(10):
            status.finish_test(f"t{index}", True, "", 1.0)
        self.assertIn("[10/10]", stream.getvalue())

    def test_verbose_logs_each_test(self):
        stream = io.StringIO()
        status = RUNNER.Status(stream=stream, enabled=False, verbose=True)
        status.start(2)
        status.finish_test("a", True, "", 1.0)
        status.finish_test("b", False, " (boom)", 2.0)
        output = stream.getvalue()
        self.assertIn("PASS a", output)
        self.assertIn("FAIL b", output)

    def test_the_wheel_falls_back_when_the_stream_cannot_encode_it(self):
        class AsciiTTY(FakeTTY):
            encoding = "ascii"

        status = RUNNER.Status(stream=AsciiTTY(), enabled=True)
        self.assertEqual(status.wheel, RUNNER.ASCII_WHEEL)

    def test_failure_details_are_logged_with_the_status(self):
        class Failing(unittest.TestCase):
            def test_boom(self):
                self.fail("boom")

        stream = io.StringIO()
        status = RUNNER.Status(stream=stream, enabled=False)
        result = RUNNER.WheelResult(
            stream=io.StringIO(),
            descriptions=False,
            verbosity=0,
            status=status,
        )
        Failing("test_boom").run(result)
        result.stopTestRun()
        result.printErrors()
        output = stream.getvalue()
        self.assertIn("failure details", output)
        self.assertIn("FAIL: test_boom", output)
        self.assertIn("boom", output)


if __name__ == "__main__":
    unittest.main()
