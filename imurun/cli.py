"""The ``imurun`` console command.

It finds the app root (the bundled copy inside this package when installed
from a wheel, the repository root in a source checkout), makes ``services``
and ``model`` importable from there, and hands over to ``start.main`` with
the user's arguments: ``imurun --no-open --port 8800`` is
``python start.py --no-open --port 8800``.

Only the standard library is imported here, so a broken environment still
produces a message instead of a traceback.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


def is_installed_layout() -> bool:
    """True when the app is bundled inside this package (wheel install)."""
    return (PACKAGE_DIR / "start.py").is_file()


def app_root() -> Path:
    return PACKAGE_DIR if is_installed_layout() else PACKAGE_DIR.parent


def _version() -> str:
    from imurun import __version__

    return __version__


def _prepend_path(root: Path) -> None:
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)
    # Child processes (voice worker, MCP servers, window, tuning probes) run
    # `python -m services...` or scripts under the root; they need it too.
    current = os.environ.get("PYTHONPATH", "")
    parts = current.split(os.pathsep) if current else []
    if text not in parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([text, *parts])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("--version", "-V", "version"):
        print(f"imurun {_version()}")
        return 0

    root = app_root()
    if not (root / "start.py").is_file():
        print(
            "IMURUN is not installed correctly: start.py was not found next to\n"
            f"  {PACKAGE_DIR}\n"
            "Reinstall it with:  python -m pip install --force-reinstall imurun",
            file=sys.stderr,
        )
        return 2
    _prepend_path(root)

    import start  # noqa: PLC0415 - must come after the path is set

    try:
        result = start.main(args)
    except KeyboardInterrupt:
        return 130
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
