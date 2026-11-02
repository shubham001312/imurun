"""Where an installed copy keeps its files (docs/setup/CODE-REQUESTS.md 9-11).

In a source checkout the app root is the repository and ``models/``,
``bin/`` and ``apps.local.json`` live there. In a ``pip install imurun`` the
same tree sits under ``site-packages/imurun/`` (``install.INSTALLED``: the
root has ``__init__.py`` and ``cli.py``), and nothing may be written there:
models, engine binaries and registered app credentials go to the data root
(``IMURUN_DATA_ROOT`` or ``~/.imurun``).
"""
from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


def installed() -> bool:
    """True for a pip-installed copy (the app root is a package)."""
    return (APP_ROOT / "__init__.py").is_file() and (APP_ROOT / "cli.py").is_file()


def data_root() -> Path:
    env = os.environ.get("IMURUN_DATA_ROOT")
    return Path(env).expanduser() if env else Path.home() / ".imurun"


def models_dir() -> Path:
    """``IMURUN_MODELS_DIR`` > ``<data root>/models`` when installed > ``<app root>/models``."""
    env = os.environ.get("IMURUN_MODELS_DIR")
    if env:
        return Path(env).expanduser()
    return data_root() / "models" if installed() else APP_ROOT / "models"


def engine_dir() -> Path:
    """Where ``imurun --get-engine`` saves the prebuilt library."""
    return data_root() / "bin"


def local_file(name: str, package_dir: Path) -> Path:
    """A writable sidecar file: next to the module in a checkout, in the data
    root when installed (site-packages is never written to)."""
    return data_root() / name if installed() else package_dir / name
