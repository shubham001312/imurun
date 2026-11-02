"""IMURUN: local-first AI chat on the FISH engine.

This package is the installable entry point (``pip install imurun`` gives the
``imurun`` command). In a source checkout it is a thin shim next to
``start.py``; in an installed wheel the whole app (``services/``, ``model/``,
``start.py``, the frontend and the engine library) is bundled inside this
directory, laid out exactly like the checkout, so every path the server
derives from ``__file__`` resolves the same way in both.

The version has one source of truth: ``[project].version`` in
``pyproject.toml``. A checkout reads it from there; an installed copy reads
the same value from the wheel's metadata.
"""
from __future__ import annotations

from pathlib import Path


def _read_version() -> str:
    here = Path(__file__).resolve().parent
    pyproject = here.parent / "pyproject.toml"
    # Source checkout: pyproject.toml sits next to services/ and start.py.
    if pyproject.is_file() and (here.parent / "start.py").is_file():
        try:
            import tomllib

            return str(tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"])
        except Exception:  # noqa: BLE001 - fall through to the installed metadata
            pass
    try:
        from importlib.metadata import version

        return version("imurun")
    except Exception:  # noqa: BLE001
        return "0+unknown"


__version__ = _read_version()
__all__ = ["__version__"]
