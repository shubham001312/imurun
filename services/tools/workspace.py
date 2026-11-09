"""The workspace: the one folder the file and shell tools may touch.

The user picks the folder (``permissions.workspaceRoot``; default
``~/.imurun/workspace``). Every path a tool receives is resolved here:

* a relative path is joined to the root;
* the result is passed through ``realpath`` so ``..`` and symlinks cannot
  step outside, and must then still be inside the root;
* guardrail source, ``.git`` folders, secret files (``.env``, ``.netrc``...)
  and system locations are refused by ``safety.decisions.check_action``.

The root is read through a callable on every use, so changing the setting
takes effect on the next tool call without a restart.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..safety.decisions import check_action


def default_workspace() -> Path:
    """``<data root>/workspace``; honours IMURUN_DATA_ROOT like every other
    store (it used to be ``~/.imurun/workspace`` whatever the data root)."""
    from ..paths import data_root

    return data_root() / "workspace"


class WorkspaceError(ValueError):
    """A path is outside the workspace or otherwise refused. Safe to show."""


def is_filesystem_root(path: Path) -> bool:
    """True for ``/`` or a bare drive such as ``C:\\`` - never a workspace."""
    resolved = Path(os.path.realpath(path))
    return resolved.parent == resolved


@dataclass
class Workspace:
    """Resolve and check paths against the current workspace root."""

    root_getter: Callable[[], Path | str]
    # Folders the user approved besides the root (Settings > Permissions,
    # ``extraRoots``; Desktop and Documents by default). Same rules inside
    # them: protected and system paths are still refused.
    extra_getter: Callable[[], list] | None = None

    @classmethod
    def fixed(cls, root: Path | str) -> "Workspace":
        return cls(lambda: root)

    @property
    def extra_roots(self) -> list[Path]:
        if self.extra_getter is None:
            return []
        try:
            raw = list(self.extra_getter() or [])
        except Exception:  # noqa: BLE001 - a broken setting means no extras
            return []
        out: list[Path] = []
        for item in raw:
            path = Path(os.path.realpath(os.path.expanduser(str(item))))
            if not is_filesystem_root(path) and path not in out:
                out.append(path)
        return out

    @property
    def root(self) -> Path:
        try:
            raw = self.root_getter()
        except Exception:  # noqa: BLE001 - a broken settings file falls back
            raw = None
        root = Path(os.path.expanduser(str(raw))) if raw else default_workspace()
        return Path(os.path.realpath(root))

    def ensure(self) -> Path:
        """Create the root if it is missing and return it."""
        root = self.root
        root.mkdir(parents=True, exist_ok=True)
        return root

    def resolve(self, path: str | None, *, kind: str = "file_read") -> Path:
        """Return the absolute path for ``path`` or raise ``WorkspaceError``.

        ``kind`` is the safety action kind (``file_read``, ``file_write`` or
        ``file_delete``); it only changes the wording of a refusal.
        """
        root = self.root
        if is_filesystem_root(root):
            raise WorkspaceError("the workspace cannot be a whole drive; pick a folder in Settings")
        text = str(path or "").strip().strip('"').strip("'")
        if text in ("", "."):
            candidate = root
        else:
            given = Path(os.path.expanduser(text))
            candidate = given if given.is_absolute() else root / given
        resolved = Path(os.path.realpath(candidate))
        container = root if _inside(resolved, root) else next(
            (extra for extra in self.extra_roots if _inside(resolved, extra)), None)
        if container is None:
            allowed = ", ".join(str(p) for p in [root, *self.extra_roots])
            raise WorkspaceError(
                f"{text or '.'} is outside the folders IMURUN may use ({allowed}). "
                "Use a path inside one of them, or add the folder in Settings > Permissions."
            )
        decision = check_action(
            {"kind": kind if kind in ("file_read", "file_write", "file_delete") else "file_read",
             "path": str(resolved)},
            project_root=str(container),
            user_data_root=str(container),
        )
        if decision.kind == "deny":
            raise WorkspaceError("; ".join(decision.reasons) or "that path is protected")
        return resolved

    def relative(self, path: Path) -> str:
        """``path`` relative to the root, with forward slashes, for display."""
        try:
            rel = Path(path).relative_to(self.root)
        except ValueError:
            return str(path)
        text = rel.as_posix()
        return text or "."


def _inside(path: Path, root: Path) -> bool:
    a = os.path.normcase(str(path))
    b = os.path.normcase(str(root))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)
