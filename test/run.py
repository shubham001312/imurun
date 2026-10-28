"""Strict test runner: zero tests or any skip is a failure, not a pass.

unittest's default is to exit 0 when it discovers nothing, and skips are
easy to ignore. CI calls this on Windows, macOS and Linux so a green run
always means the tests actually ran (C-07, M-01).

The console gets a rotating wheel with the current test and elapsed time.
A non-TTY (CI logs, pipes) gets periodic progress lines instead, so no
carriage-return noise ends up in a build log. ``--verbose`` logs every test.
"""
from __future__ import annotations

import argparse
import functools
import os
import platform
import shutil
import sys
import threading
import time
import unittest

# Running this file directly puts test/ at the front of sys.path, so
# `import services` would resolve to test/services/ instead of the real
# package and every test would fail on a missing submodule. Drop it.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if sys.path and os.path.abspath(sys.path[0] or os.getcwd()) == _THIS_DIR:
    del sys.path[0]

WHEEL = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ASCII_WHEEL = "|/-\\"
PROGRESS_STEPS = 10


def verdict(
    tests_run: int,
    failures: int,
    errors: int,
    skipped: int,
    allow_skip: bool = False,
) -> str:
    """Return the failure reason, or "" when the run is acceptable."""
    if tests_run == 0:
        return "no tests ran"
    if failures or errors:
        return f"{failures} failure(s), {errors} error(s)"
    if skipped and not allow_skip:
        return f"{skipped} skipped test(s); rerun with --allow-skip if intended"
    return ""


def _can_encode(text: str, stream) -> bool:
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


class _Sink:
    """Discard unittest's own summary; the status object prints ours."""

    def write(self, _text: str) -> None:
        return

    def flush(self) -> None:
        return


class Status:
    """One live line for the wheel; logs clear it, print, and resume it."""

    def __init__(self, stream=None, *, enabled=None, verbose: bool = False) -> None:
        self.stream = stream if stream is not None else sys.stdout
        if enabled is None:
            enabled = bool(getattr(self.stream, "isatty", lambda: False)())
        self.enabled = bool(enabled)
        self.verbose = verbose
        self.wheel = WHEEL if _can_encode(WHEEL, self.stream) else ASCII_WHEEL
        self.total = 0
        self.done = 0
        self.current = ""
        self.started = time.monotonic()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._width = 0
        self._next_progress = 1
        self._running = False

    def start(self, total: int) -> None:
        self.total = max(0, total)
        self.started = time.monotonic()
        self._next_progress = max(1, self.total // PROGRESS_STEPS)
        self._running = True
        if self.enabled:
            self._thread = threading.Thread(
                target=self._spin, name="test-wheel", daemon=True
            )
            self._thread.start()

    def _spin(self) -> None:
        index = 0
        while not self._stop.wait(0.1):
            try:
                with self._lock:
                    self._render(index)
            except (OSError, ValueError):
                return
            index = (index + 1) % len(self.wheel)

    def _render(self, index: int) -> None:
        elapsed = time.monotonic() - self.started
        width = shutil.get_terminal_size((100, 24)).columns
        head = f"{self.wheel[index]} {self.done}/{self.total}"
        text = f"{head} {elapsed:6.1f}s {self.current}"
        text = text[: max(12, width - 1)]
        padding = " " * max(0, self._width - len(text))
        self.stream.write("\r" + text + padding)
        self.stream.flush()
        self._width = len(text)

    def set_test(self, name: str) -> None:
        with self._lock:
            self.current = name

    def finish_test(
        self,
        name: str,
        ok: bool,
        detail: str = "",
        duration_ms: float = 0.0,
    ) -> None:
        with self._lock:
            self.done += 1
            self.current = ""
            if not self.enabled:
                if (
                    self.done == self.total
                    or self.done % self._next_progress == 0
                ):
                    elapsed = time.monotonic() - self.started
                    self._write(f"[{self.done}/{self.total}] {elapsed:.1f}s")
            if self.verbose or not ok:
                label = "PASS" if ok else "FAIL"
                self._write(
                    f"{label} {name} ({duration_ms:.0f} ms){detail}"
                )

    def log(self, text: str = "") -> None:
        with self._lock:
            self._write(text)

    def _write(self, text: str) -> None:
        if self.enabled and self._width:
            self.stream.write("\r" + " " * self._width + "\r")
        self.stream.write(text + "\n")
        self.stream.flush()
        self._width = 0
        if self.enabled and self._running and not self._stop.is_set():
            self._render(0)

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        with self._lock:
            if self.enabled and self._width:
                self.stream.write("\r" + " " * self._width + "\r")
                self.stream.flush()
                self._width = 0


class WheelResult(unittest.TextTestResult):
    """TextTestResult with no dots, a live wheel, and per-test timings."""

    def __init__(self, stream, descriptions, verbosity, status: Status) -> None:
        super().__init__(stream, descriptions, verbosity=0)
        self.status = status
        self._test_started = 0.0
        self._outcome = "ok"
        self._detail = ""
        self._timings: list[tuple[float, str]] = []

    def startTest(self, test) -> None:
        super().startTest(test)
        self._test_started = time.monotonic()
        self._outcome = "ok"
        self._detail = ""
        self.status.set_test(test.id())

    def stopTest(self, test) -> None:
        super().stopTest(test)
        duration = (time.monotonic() - self._test_started) * 1000
        self._timings.append((duration, test.id()))
        self.status.finish_test(
            test.id(), self._outcome == "ok", self._detail, duration
        )

    def addSuccess(self, test) -> None:
        super().addSuccess(test)
        self._outcome = "ok"

    def addFailure(self, test, err) -> None:
        super().addFailure(test, err)
        self._outcome = "fail"

    def addError(self, test, err) -> None:
        super().addError(test, err)
        self._outcome = "error"

    def addSkip(self, test, reason) -> None:
        super().addSkip(test, reason)
        self._outcome = "skip"
        self._detail = f" ({reason})"

    def addExpectedFailure(self, test, err) -> None:
        super().addExpectedFailure(test, err)
        self._outcome = "ok"
        self._detail = " (expected failure)"

    def addUnexpectedSuccess(self, test) -> None:
        super().addUnexpectedSuccess(test)
        self._outcome = "fail"
        self._detail = " (unexpected success)"

    def stopTestRun(self) -> None:
        self.status.stop()
        elapsed = time.monotonic() - self.status.started
        unexpected = len(self.unexpectedSuccesses)
        passed = (
            self.testsRun
            - len(self.failures)
            - len(self.errors)
            - len(self.skipped)
            - unexpected
        )
        self.status.log("")
        self.status.log(f"ran {self.testsRun} tests in {elapsed:.1f}s")
        self.status.log(
            f"  passed {passed} | failed {len(self.failures)} | "
            f"errors {len(self.errors)} | skipped {len(self.skipped)} | "
            f"unexpected {unexpected}"
        )
        slowest = sorted(self._timings, reverse=True)[:5]
        if slowest:
            self.status.log("  slowest:")
            for duration, name in slowest:
                self.status.log(f"    {duration:8.1f} ms  {name}")

    def printErrors(self) -> None:
        if not self.failures and not self.errors:
            return
        self.status.log("")
        self.status.log("failure details:")
        for label, entries in (("FAIL", self.failures), ("ERROR", self.errors)):
            for test, err in entries:
                self.status.log(self.separator1)
                self.status.log(f"{label}: {self.getDescription(test)}")
                self.status.log(self.separator2)
                for line in str(err).rstrip().splitlines():
                    self.status.log(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="run the IMURUN test suite strictly"
    )
    parser.add_argument(
        "--start", default="test", help="folder to discover (default: test)"
    )
    parser.add_argument(
        "--allow-skip", action="store_true", help="accept skipped tests"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log every test, not only failures",
    )
    parser.add_argument(
        "--no-spinner",
        action="store_true",
        help="disable the wheel even on a TTY",
    )
    args = parser.parse_args(argv)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    enabled = False if args.no_spinner else None
    status = Status(enabled=enabled, verbose=args.verbose)
    status.log("IMURUN test suite")
    status.log(f"  python {platform.python_version()} on {platform.platform()}")
    status.log(f"  root: {root}")

    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(root, args.start), top_level_dir=root)
    total = suite.countTestCases()
    status.log(f"  discovered {total} tests")
    status.start(total)

    runner = unittest.TextTestRunner(
        stream=_Sink(),
        verbosity=0,
        resultclass=functools.partial(WheelResult, status=status),
    )
    result = runner.run(suite)

    problem = verdict(
        result.testsRun,
        len(result.failures),
        len(result.errors),
        len(result.skipped),
        allow_skip=args.allow_skip,
    )
    if result.unexpectedSuccesses:
        problem = (
            f"{len(result.unexpectedSuccesses)} unexpected success(es)"
            if not problem
            else f"{problem}; {len(result.unexpectedSuccesses)} unexpected success(es)"
        )
    if problem:
        print(f"FAIL: {problem}")
        return 1
    print(f"PASS: {result.testsRun} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
