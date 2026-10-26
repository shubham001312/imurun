"""Session identity and memory: one session, one memory, isolated by default.

The id is readable (slug + 4 hex chars) and the name comes from the first
command. Renaming changes the name only; the id stays stable so references
never break. Memory is per session and persisted under ~/.imurun/sessions/.
Reading another session requires an explicit user grant and every refusal is
marked (V27).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from ..safety.decisions import PermissionRequired

MAX_NAME = 80


def default_root() -> Path:
    return Path(os.path.expanduser("~/.imurun/sessions"))


def slugify(text: str, max_words: int = 6, max_len: int = 40) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-z0-9]+", folded.casefold())[:max_words]
    slug = "-".join(words)[:max_len].strip("-")
    return slug or "session"


def new_session_id(first_command: str) -> str:
    """Readable: derived from the first command plus four hex characters."""
    return f"{slugify(first_command)}-{secrets.token_hex(2)}"


def derive_name(first_command: str) -> str:
    name = first_command.strip()[:60].rstrip()
    return name or "Untitled session"


def _safe_filename(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id)


@dataclass(frozen=True)
class SessionIdentity:
    id: str
    name: str
    created_at: float
    renamed_at: float | None = None


class Session:
    def __init__(
        self,
        identity: SessionIdentity,
        memory: dict,
        path: Path,
        mark=None,
    ) -> None:
        self._identity = identity
        self._memory = dict(memory)
        self._path = path
        self._mark = mark
        self._lock = threading.RLock()

    @property
    def identity(self) -> SessionIdentity:
        return self._identity

    @property
    def id(self) -> str:
        return self._identity.id

    @property
    def name(self) -> str:
        return self._identity.name

    @classmethod
    def start(cls, first_command: str, root: Path | str | None = None, mark=None) -> "Session":
        if not first_command or not first_command.strip():
            raise ValueError("the first command is required to start a session")
        identity = SessionIdentity(
            id=new_session_id(first_command),
            name=derive_name(first_command),
            created_at=time.time(),
        )
        session = cls(identity, {}, _path_for(identity.id, root), mark)
        session.save()
        return session

    @classmethod
    def load(cls, session_id: str, root: Path | str | None = None, mark=None) -> "Session":
        path = _path_for(session_id, root)
        data = json.loads(path.read_text(encoding="utf-8"))
        identity = SessionIdentity(
            id=data["id"],
            name=data["name"],
            created_at=data["created_at"],
            renamed_at=data.get("renamed_at"),
        )
        return cls(identity, dict(data.get("memory", {})), path, mark)

    def rename(self, new_name: str) -> SessionIdentity:
        cleaned = new_name.strip()
        if not cleaned:
            raise ValueError("the session name cannot be empty")
        if len(cleaned) > MAX_NAME:
            raise ValueError(f"the session name cannot exceed {MAX_NAME} characters")
        with self._lock:
            self._identity = replace(self._identity, name=cleaned, renamed_at=time.time())
            self.save()
            return self._identity

    def remember(self, key: str, value) -> None:
        if not key:
            raise ValueError("the memory key cannot be empty")
        with self._lock:
            self._memory[key] = value
            self.save()

    def recall(self, key: str, default=None):
        with self._lock:
            return self._memory.get(key, default)

    def notes(self) -> dict:
        with self._lock:
            return dict(self._memory)

    def forget(self, key: str) -> bool:
        with self._lock:
            if key not in self._memory:
                return False
            del self._memory[key]
            self.save()
            return True

    def clear(self) -> None:
        with self._lock:
            self._memory = {}
            self.save()

    def save(self) -> None:
        with self._lock:
            payload = {
                "id": self._identity.id,
                "name": self._identity.name,
                "created_at": self._identity.created_at,
                "renamed_at": self._identity.renamed_at,
                "memory": self._memory,
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _private_dir(self._path.parent)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self._path)
            _private_file(self._path)

    @staticmethod
    def read_other(
        target_session_id: str,
        *,
        root: Path | str | None = None,
        allowed: bool = False,
        requester: str | None = None,
        mark=None,
    ) -> dict:
        """Read another session's memory. Refused unless the user granted it."""
        if not allowed:
            if mark is not None:
                try:
                    mark(
                        "G3-cross-session",
                        target_session_id,
                        f"read refused for {requester or 'unknown'} without a user grant",
                    )
                except Exception:  # noqa: BLE001 - marking must never change the answer
                    pass
            raise PermissionRequired(
                f"reading session {target_session_id} requires the user's explicit permission"
            )
        data = json.loads(_path_for(target_session_id, root).read_text(encoding="utf-8"))
        return dict(data.get("memory", {}))

    @staticmethod
    def list_sessions(root: Path | str | None = None) -> list[SessionIdentity]:
        directory = Path(root) if root else default_root()
        if not directory.exists():
            return []
        found: list[SessionIdentity] = []
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                found.append(
                    SessionIdentity(
                        id=data["id"],
                        name=data["name"],
                        created_at=data["created_at"],
                        renamed_at=data.get("renamed_at"),
                    )
                )
            except (OSError, KeyError, ValueError):
                continue
        return sorted(found, key=lambda identity: identity.created_at)


def _path_for(session_id: str, root: Path | str | None) -> Path:
    directory = Path(root) if root else default_root()
    return directory / f"{_safe_filename(session_id)}.json"


def _private_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
