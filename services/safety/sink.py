"""Durable audit sink: one JSON line per event, fsync, bounded rotation.

The ring in Audit is for reading; this is for evidence that survives a
restart or a crash (H-14). Rotation caps disk use; a write failure is
counted by Audit, never silently dropped (H-15).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .audit import Event


class JsonlSink:
    def __init__(self, path: Path | str, max_bytes: int = 5_000_000, backups: int = 3) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if backups < 1:
            raise ValueError("backups must be at least 1")
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._backups = backups
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def __call__(self, event: Event) -> None:
        payload = {
            "at": event.at,
            "rule": event.rule,
            "subject": event.subject,
            "why": event.why,
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(encoded))
            with open(self._path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming <= self._max_bytes:
            return
        oldest = self._path.with_name(f"{self._path.name}.{self._backups}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self._backups - 1, 0, -1):
            source = self._path.with_name(f"{self._path.name}.{index}")
            target = self._path.with_name(f"{self._path.name}.{index + 1}")
            if source.exists():
                os.replace(source, target)
        os.replace(self._path, self._path.with_name(f"{self._path.name}.1"))


def default_sink(path: Path | str | None = None) -> JsonlSink:
    if path is None:
        path = Path(os.path.expanduser("~/.imurun/logs/audit.jsonl"))
    return JsonlSink(path)
