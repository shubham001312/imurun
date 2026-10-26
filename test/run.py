"""Strict test runner: zero tests or any skip is a failure, not a pass.

unittest's default is to exit 0 when it discovers nothing, and skips are
easy to ignore. CI calls this on Windows, macOS and Linux so a green run
always means the tests actually ran (C-07, M-01).
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run the IMURUN test suite strictly")
    parser.add_argument("--start", default="test", help="folder to discover (default: test)")
    parser.add_argument("--allow-skip", action="store_true", help="accept skipped tests")
    args = parser.parse_args(argv)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    loader = unittest.TestLoader()
    suite = loader.discover(os.path.join(root, args.start), top_level_dir=root)
    result = unittest.TextTestRunner(verbosity=1).run(suite)

    problem = verdict(
        result.testsRun,
        len(result.failures),
        len(result.errors),
        len(result.skipped),
        allow_skip=args.allow_skip,
    )
    if problem:
        print(f"FAIL: {problem}")
        return 1
    print(f"PASS: {result.testsRun} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
