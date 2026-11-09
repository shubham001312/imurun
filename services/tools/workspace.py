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

Reading outside the workspace: the permission policy may turn a read of an
outside path into an approval card (``permissions.outsideReads = "ask"``).
Only after the user approves that one call does the executor run the tool
inside ``outside_grant(paths)``; ``resolve`` then accepts exactly those
paths (and what is under an approved folder) for reading, still refusing
secret files, ``.git``, credential folders (``~/.ssh``...) and IMURUN's own
data folder (it holds the API token). Writes and deletes never leave the
workspace.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator

from ..safety.decisions import check_action


def default_workspace() -> Path:
    """``<data root>/workspace``: ``IMURUN_DATA_ROOT`` when set (it used to be
    ignored, so a server started with a scratch data root still worked in
    the real ``~/.imurun/workspace``), else ``~/.imurun``."""
    env = os.environ.get("IMURUN_DATA_ROOT")
    base = Path(os.path.expanduser(env)) if env else Path(os.path.expanduser("~/.imurun"))
    return base / "workspace"


# Paths the user approved for reading outside the workspace, for the call
# that is running in this thread (set by the executor, see outside_grant).
_OUTSIDE_OK: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("imurun_outside_ok", default=())

# Folders under the home directory that hold credentials. Never readable
# through the outside-read approval, whatever the user clicks.
CREDENTIAL_DIRS = (".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker", ".gcloud",
                   os.path.join(".config", "gcloud"), os.path.join(".config", "gh"))
_SECRET_NAMES = (".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".htpasswd", ".pgpass")


@contextlib.contextmanager
def outside_grant(paths: Iterable[str | Path]) -> Iterator[None]:
    """Let ``resolve(kind="file_read")`` accept ``paths`` in this context."""
    token = _OUTSIDE_OK.set(tuple(os.path.realpath(str(p)) for p in paths))
    try:
        yield
    finally:
        _OUTSIDE_OK.reset(token)


def _data_root() -> Path:
    env = os.environ.get("IMURUN_DATA_ROOT")
    return Path(os.path.realpath(os.path.expanduser(env or "~/.imurun")))


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

    @classmethod
    def fixed(cls, root: Path | str) -> "Workspace":
        return cls(lambda: root)

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
        if not _inside(resolved, root) and kind == "file_read" and any(
                _inside(resolved, Path(g)) for g in _OUTSIDE_OK.get()):
            outside_refusal(resolved)        # raises for secrets and credential folders
            return resolved
        if not _inside(resolved, root):
            raise WorkspaceError(
                f"{text or '.'} is outside the workspace ({root}). "
                "Use a path inside the workspace."
            )
        decision = check_action(
            {"kind": kind if kind in ("file_read", "file_write", "file_delete") else "file_read",
             "path": str(resolved)},
            project_root=str(root),
            user_data_root=str(root),
        )
        if decision.kind == "deny":
            raise WorkspaceError("; ".join(decision.reasons) or "that path is protected")
        return resolved

    def resolve_outside(self, path: str | None) -> Path:
        """The absolute path of ``path`` when it is outside the workspace and
        may be read after an approval; raises ``WorkspaceError`` otherwise
        (inside paths go through ``resolve``)."""
        text = str(path or "").strip().strip('"').strip("'")
        given = Path(os.path.expanduser(text or "."))
        candidate = given if given.is_absolute() else self.root / given
        resolved = Path(os.path.realpath(candidate))
        if _inside(resolved, self.root):
            raise WorkspaceError(f"{text} is inside the workspace")
        outside_refusal(resolved)
        return resolved

    def relative(self, path: Path) -> str:
        """``path`` relative to the root, with forward slashes, for display."""
        try:
            rel = Path(path).relative_to(self.root)
        except ValueError:
            return str(path)
        text = rel.as_posix()
        return text or "."


def outside_refusal(path: Path) -> None:
    """Raise ``WorkspaceError`` for an outside path that must stay unreadable."""
    parts = [p.casefold() for p in Path(path).parts]
    name = Path(path).name.casefold()
    if ".git" in parts:
        raise WorkspaceError(f"{path} is inside a .git folder")
    if name == ".env" or name.startswith(".env.") or name in _SECRET_NAMES:
        raise WorkspaceError(f"{path} is a secrets file")
    home = Path(os.path.realpath(os.path.expanduser("~")))
    for folder in CREDENTIAL_DIRS:
        if _inside(path, home / folder):
            raise WorkspaceError(f"{path} is in a credentials folder ({folder})")
    data = _data_root()
    if _inside(path, data):
        raise WorkspaceError(f"{path} is in IMURUN's data folder (it holds the API token)")
    try:
        from ..safety.decisions import GUARDRAIL_DIR

        if _inside(path, Path(os.path.realpath(GUARDRAIL_DIR))):
            raise WorkspaceError(f"{path} is guardrail source")
    except ImportError:  # pragma: no cover
        pass


def _inside(path: Path, root: Path) -> bool:
    a = os.path.normcase(str(path))
    b = os.path.normcase(str(root))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)
