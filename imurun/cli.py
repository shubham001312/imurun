"""The ``imurun`` console command.

It finds the app root (the bundled copy inside this package when installed
from a wheel, the repository root in a source checkout), makes ``services``
and ``model`` importable from there, and hands over to ``start.main`` with
the user's arguments: ``imurun --no-open --port 8800`` is
``python start.py --no-open --port 8800``.

Subcommands (everything else goes to start.py):
    imurun doctor [--port N]       Python, engine (ABI), GPU runtime, dirs, port
    imurun install [--force ...]   run the installer into this interpreter
                                   (install.py --here; --no-voice, --tuning, ...)
    imurun update [--check] [--yes]  check PyPI/GitHub and install a newer version
    imurun --version               version (+ "update available: X" from the cache)

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


def version_line() -> str:
    """``imurun X`` plus the cached update notice (no network, a JSON read)."""
    line = f"imurun {_version()}"
    try:
        _prepend_path(app_root())
        from services.update import cached_notice  # noqa: PLC0415

        notice = cached_notice(current=_version())
    except Exception:  # noqa: BLE001 - --version must always answer
        notice = None
    return f"{line}  ({notice})" if notice else line


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
        print(version_line())
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

    if args and args[0] == "update":
        from services.update.cli import main as update_main  # noqa: PLC0415

        return update_main(args[1:])

    if args and args[0] in ("doctor", "install"):
        import install  # noqa: PLC0415 - stdlib only, lives next to start.py

        if args[0] == "doctor":
            port = 8732
            if "--port" in args[1:]:
                try:
                    port = int(args[args.index("--port") + 1])
                except (IndexError, ValueError):
                    pass
            return install.main(["--doctor", "--port", str(port)])
        return install.main(["--here", *args[1:]])

    import start  # noqa: PLC0415 - must come after the path is set

    try:
        result = start.main(args)
    except KeyboardInterrupt:
        return 130
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
