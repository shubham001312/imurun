"""``imurun update [--check] [--yes]`` (called from imurun/cli.py)."""
from __future__ import annotations

import argparse
import sys

from .manager import make_manager


def main(argv: list[str] | None = None, *, manager=None, ask=input) -> int:
    parser = argparse.ArgumentParser(prog="imurun update",
                                     description="Check for a new IMURUN version and install it.")
    parser.add_argument("--check", action="store_true", help="only check, install nothing")
    parser.add_argument("--yes", "-y", action="store_true", help="install without asking")
    args = parser.parse_args(argv)
    mgr = manager or make_manager()
    print(f"IMURUN {mgr.current} ({mgr.source}); checking PyPI and GitHub ...", flush=True)
    view = mgr.check(force=True)
    if view["latest"] is None:
        print(f"Could not check for updates: {view['error']}", file=sys.stderr)
        return 1
    if not view["available"]:
        print(f"Up to date (latest published: {view['latest']}).")
        return 0
    print(f"Update available: {view['current']} -> {view['latest']}")
    if view["releaseNotesUrl"]:
        print(f"Release notes: {view['releaseNotesUrl']}")
    if args.check:
        print("Install it with:  imurun update")
        return 0
    if view["applyBlocked"]:
        print(f"Cannot update automatically: {view['applyBlocked']}", file=sys.stderr)
        return 1
    print(f"This runs: {view['command']}")
    if not args.yes:
        try:
            answer = ask("Update now? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            mgr.dismiss()
            print("Not updated. You will be asked again in a few launches.")
            return 0
    view = mgr.apply(restart=False)
    if view["state"] == "error":
        print(f"Update failed: {view['error']}", file=sys.stderr)
        return 1
    print(f"Updated to {view['latest']}. Restart IMURUN if it is running.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
