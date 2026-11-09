"""Local usage counters for the Usage panel.

Counters are recorded per local day and persisted as JSON. Nothing leaves the
machine. The view matches the shell contract: today's turns and tokens, the
p50 queue wait for the week, and a seven-day token history.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path


def _day(offset: int = 0) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


class UsageTracker:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def record_turn(self, mode: str = "build", wait_seconds: float = 0.0) -> None:
        with self._lock:
            data = self._load()
            bucket = data.setdefault(_day(), {"build": 0, "plan": 0, "tokens": 0, "waits": []})
            key = "plan" if mode == "plan" else "build"
            bucket[key] = int(bucket.get(key, 0)) + 1
            if wait_seconds > 0:
                waits = bucket.setdefault("waits", [])
                waits.append(round(float(wait_seconds), 3))
                bucket["waits"] = waits[-500:]
            self._save(data)

    def record_tokens(self, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            data = self._load()
            bucket = data.setdefault(_day(), {"build": 0, "plan": 0, "tokens": 0, "waits": []})
            bucket["tokens"] = int(bucket.get("tokens", 0)) + int(count)
            self._save(data)

    def view(self) -> dict:
        with self._lock:
            data = self._load()

        today = data.get(_day(), {})
        history: list[int] = []
        history_turns: list[int] = []
        labels: list[str] = []
        waits: list[float] = []
        for offset in range(6, -1, -1):
            bucket = data.get(_day(offset), {})
            history.append(int(bucket.get("tokens", 0)))
            history_turns.append(
                int(bucket.get("build", 0)) + int(bucket.get("plan", 0))
            )
            labels.append((date.today() - timedelta(days=offset)).strftime("%a"))
            waits.extend(bucket.get("waits", []))

        peak = max(history) or 1
        bars = [round((value / peak) * 100) for value in history]
        waits.sort()
        p50 = 0.0
        if waits:
            p50 = waits[len(waits) // 2]

        return {
            "turnsToday": int(today.get("build", 0)) + int(today.get("plan", 0)),
            "buildTurns": int(today.get("build", 0)),
            "planTurns": int(today.get("plan", 0)),
            "tokensToday": int(today.get("tokens", 0)),
            "queueWaitP50": round(p50, 2),
            "history": bars,
            "historyLabels": labels,
            "historyTokens": history,
            "historyTurns": history_turns,
            "generatedAt": time.time(),
        }
