"""Build glue for the ``imurun`` wheel. All metadata lives in pyproject.toml.

The app is not a single Python package in the repository: ``services/``,
``model/*.py``, ``start.py``, ``install.py``, ``window.py``, the frontend and
the engine library all sit at the repository root and find each other through
``__file__``-relative paths. The wheel keeps that layout intact by copying them
*inside* the ``imurun`` package::

    site-packages/imurun/__init__.py, cli.py        (the launcher, imurun/)
    site-packages/imurun/start.py, install.py, window.py, requirements*.txt
    site-packages/imurun/services/**, model/*.py
    site-packages/imurun/frontend/design-lab/**
    site-packages/imurun/docs/connectors/**          (served by the connectors routes)
    site-packages/imurun/bin/fish.dll | libfish.so | libfish.dylib   (when bundled)

so only one name (``imurun``) is added to site-packages, and no code under
``services/`` has to know whether it runs from a checkout or a wheel.

Engine library (environment variables, read at build time):
  IMURUN_ENGINE_LIB=<path>   bundle this library (CI passes the one it built)
  IMURUN_BUNDLE_ENGINE=0     build a pure wheel without any engine
  IMURUN_WHEEL_PLAT=<tag>    platform tag for a wheel with an engine
                             (e.g. manylinux_2_28_x86_64, macosx_11_0_arm64)
Without IMURUN_ENGINE_LIB, the library for the build machine's OS is taken
from bin/ if it exists. A wheel with an engine is tagged py3-none-<platform>;
without one it is py3-none-any (pip installs it anywhere, and the app tells
the user how to get an engine).
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import sys
from pathlib import Path

from setuptools import Distribution, setup
from setuptools.command.build_py import build_py as _build_py

try:  # setuptools >= 70.1 ships bdist_wheel itself
    from setuptools.command.bdist_wheel import bdist_wheel as _bdist_wheel
except ImportError:  # pragma: no cover - older setuptools
    from wheel.bdist_wheel import bdist_wheel as _bdist_wheel

HERE = Path(__file__).resolve().parent

# Trees copied whole (minus EXCLUDE) and single files copied into imurun/.
APP_TREES = ["services", "frontend/design-lab", "docs/connectors"]
APP_FILES = ["start.py", "install.py", "window.py", "LICENSE"]
APP_GLOBS = ["model/*.py", "requirements*.txt"]
EXCLUDE = [
    "__pycache__", "*.pyc", "*.pyo", "*.local.json", "*.local.json.tmp", "*.tmp", "*.log",
    ".DS_Store", "Thumbs.db", "*.gguf", "node_modules", ".pytest_cache",
]


def _engine_name() -> str:
    if sys.platform == "win32":
        return "fish.dll"
    if sys.platform == "darwin":
        return "libfish.dylib"
    return "libfish.so"


def engine_source() -> Path | None:
    """The engine library to bundle, or None for a pure wheel."""
    if os.environ.get("IMURUN_BUNDLE_ENGINE", "1").strip() in ("0", "false", "no"):
        return None
    explicit = os.environ.get("IMURUN_ENGINE_LIB")
    if explicit:
        path = Path(explicit).resolve()
        if not path.is_file():
            raise SystemExit(f"IMURUN_ENGINE_LIB={explicit} does not exist")
        return path
    candidate = HERE / "bin" / _engine_name()
    return candidate if candidate.is_file() else None


def _bundled_name(src: Path) -> str:
    """Name the bindings look for, whatever the CI artifact was called."""
    suffix = src.suffix.lower()
    return {".dll": "fish.dll", ".dylib": "libfish.dylib"}.get(suffix, "libfish.so")


def _excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE)


def _copy_tree(src: Path, dst: Path) -> None:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not _excluded(d)]
        rel = Path(root).relative_to(src)
        for name in files:
            if _excluded(name):
                continue
            target = dst / rel / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(root) / name, target)


def _clear_app_copy(pkg: Path) -> None:
    """Remove everything build_py copies into ``pkg`` (never imurun/*.py itself)."""
    tops = {Path(tree).parts[0] for tree in APP_TREES}
    tops.update({Path(pattern).parts[0] for pattern in APP_GLOBS if "/" in pattern})
    tops.add("bin")
    for top in sorted(tops):
        target = pkg / top
        if target.is_dir():
            shutil.rmtree(target)
    for name in APP_FILES:
        (pkg / name).unlink(missing_ok=True)
    for pattern in APP_GLOBS:
        if "/" not in pattern:
            for stale in pkg.glob(pattern):
                stale.unlink()


class build_py(_build_py):
    """Also copy the app (and the engine) into build/lib/imurun/."""

    def run(self) -> None:
        super().run()
        if getattr(self, "editable_mode", False):
            return  # editable installs run the checkout itself (imurun/cli.py)
        pkg = Path(self.build_lib) / "imurun"
        # setuptools reuses build/lib between builds and only ever adds to it,
        # so a file deleted from the checkout (or an engine bundled by an
        # earlier build) would ship again in every later wheel. Start the
        # copied part of the package from empty on every build.
        _clear_app_copy(pkg)
        for tree in APP_TREES:
            src = HERE / tree
            if src.is_dir():
                _copy_tree(src, pkg / tree)
        for name in APP_FILES:
            if (HERE / name).is_file():
                shutil.copy2(HERE / name, pkg / name)
        for pattern in APP_GLOBS:
            for src in sorted(HERE.glob(pattern)):
                if src.is_file() and not _excluded(src.name):
                    target = pkg / src.relative_to(HERE)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
        engine = engine_source()
        if engine is not None:
            (pkg / "bin").mkdir(parents=True, exist_ok=True)
            shutil.copy2(engine, pkg / "bin" / _bundled_name(engine))


class BinaryDistribution(Distribution):
    """A bundled engine is platform-specific: install into platlib, tag the wheel."""

    def has_ext_modules(self) -> bool:
        return engine_source() is not None


class bdist_wheel(_bdist_wheel):
    """py3-none-<platform> when an engine is bundled, py3-none-any otherwise."""

    def get_tag(self) -> tuple[str, str, str]:
        python, abi, plat = super().get_tag()
        if engine_source() is None:
            return python, abi, plat
        plat = os.environ.get("IMURUN_WHEEL_PLAT") or plat
        return "py3", "none", plat


setup(distclass=BinaryDistribution, cmdclass={"build_py": build_py, "bdist_wheel": bdist_wheel})
