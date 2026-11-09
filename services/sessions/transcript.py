"""Session transcripts: append-only JSONL, one file per session.

The transcript is what the chat screen renders. It is separate from session
memory (which is for facts the agent chooses to keep). Reads are bounded and
a corrupt line is marked and skipped, never silently accepted.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROLES = frozenset({"user", "assistant", "system"})
MAX_CONTENT = 200_000
MAX_READ = 1_000


def _safe_filename(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id)


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    at: float
    turn_id: str = ""


class TranscriptStore:
    def __init__(self, root: Path | str | None = None, *, mark=None) -> None:
        self._root = (
            Path(root) if root else Path(os.path.expanduser("~/.imurun/sessions"))
        )
        self._mark = mark
        self._lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _path(self, session_id: str) -> Path:
        return self._root / f"{_safe_filename(session_id)}.jsonl"

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(session_id, threading.Lock())

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        turn_id: str = "",
    ) -> Message:
        if not session_id:
            raise ValueError("session_id is required")
        if role not in ROLES:
            raise ValueError(f"role must be one of {sorted(ROLES)}")
        if not isinstance(content, str) or not content:
            raise ValueError("content is required")
        if len(content) > MAX_CONTENT:
            raise ValueError(f"content cannot exceed {MAX_CONTENT} characters")
        message = Message(role=role, content=content, at=time.time(), turn_id=turn_id)
        path = self._path(session_id)
        with self._lock_for(session_id):
            self._root.mkdir(parents=True, exist_ok=True)
            _private_dir(self._root)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(message), ensure_ascii=False) + "\n")
            _private_file(path)
        return message

    def read(self, session_id: str, limit: int = 200) -> list[Message]:
        if not session_id:
            raise ValueError("session_id is required")
        if limit <= 0:
            raise ValueError("limit must be positive")
        limit = min(limit, MAX_READ)
        path = self._path(session_id)
        if not path.is_file():
            return []
        messages: list[Message] = []
        with self._lock_for(session_id):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                self._failure(path, f"unreadable transcript: {type(exc).__name__}")
                return []
        for line in lines[-limit:]:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                message = Message(
                    role=str(data["role"]),
                    content=str(data["content"]),
                    at=float(data["at"]),
                    turn_id=str(data.get("turn_id", "")),
                )
            except (ValueError, KeyError, TypeError):
                self._failure(path, "skipped a corrupt transcript line")
                continue
            if message.role not in ROLES:
                self._failure(path, "skipped a transcript line with an unknown role")
                continue
            messages.append(message)
        return messages

    def _failure(self, path: Path, why: str) -> None:
        if self._mark is None:
            return
        try:
            self._mark("G3-transcript", str(path), why)
        except Exception:  # noqa: BLE001 - a broken sink must not hide messages
            pass


def _private_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        if os.name != "nt":
            raise


def _private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        if os.name != "nt":
            raise
