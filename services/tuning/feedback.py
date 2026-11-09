"""The consent channel: thumbs-up, thumbs-down and "teach" corrections.

Nothing from a chat is ever used for training unless the user marked that
exact turn here. A thumbs-up says "this answer is good, learn from it"; a
correction ("teach") says "the right answer is this" and the correction text
replaces the model's answer in the training sample. A thumbs-down without a
correction is recorded (so the UI can show it) but never trained on.
``rating: "clear"`` withdraws consent for a turn.

Feedback is stored next to the session's transcript, append-only, at
``<sessions_root>/feedback/<session_id>.jsonl``; the latest line for a turn
wins. The directory and files are private (0700 / 0600 where the OS allows).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

RATINGS = ("up", "down", "teach", "clear")
MAX_CORRECTION = 20_000


def _safe(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id)


@dataclass(frozen=True)
class Feedback:
    session_id: str
    turn_id: str
    rating: str
    correction: str = ""
    at: float = 0.0

    @property
    def consented(self) -> bool:
        return self.rating == "up" or (self.rating in ("teach", "down") and bool(self.correction.strip()))


class FeedbackStore:
    def __init__(self, sessions_root: Path | str | None) -> None:
        root = Path(sessions_root) if sessions_root else Path(os.path.expanduser("~/.imurun/sessions"))
        self.sessions_root = root
        self.root = root / "feedback"
        self._lock = threading.Lock()

    def _path(self, session_id: str) -> Path:
        return self.root / f"{_safe(session_id)}.jsonl"

    def find_session(self, turn_id: str) -> str:
        """Which session holds an assistant message for ``turn_id`` ("" if none)."""
        if not turn_id or not self.sessions_root.is_dir():
            return ""
        needle = f'"turn_id": "{turn_id}"'
        for path in self.sessions_root.glob("*.jsonl"):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if needle in text:
                return path.stem
        return ""

    def record(self, session_id: str, turn_id: str, rating: str, correction: str = "") -> Feedback:
        rating = str(rating or "").strip().lower()
        correction = str(correction or "")
        if correction.strip() and rating in ("", "down"):
            rating = "teach"
        if rating not in RATINGS:
            raise ValueError(f"rating must be one of {', '.join(RATINGS)}")
        if rating == "teach" and not correction.strip():
            raise ValueError("a teach rating needs the corrected answer")
        if len(correction) > MAX_CORRECTION:
            raise ValueError(f"correction cannot exceed {MAX_CORRECTION} characters")
        if not session_id or not turn_id:
            raise ValueError("session_id and turn_id are required")
        item = Feedback(session_id=session_id, turn_id=turn_id, rating=rating,
                        correction=correction.strip(), at=time.time())
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            _private(self.root, 0o700)
            path = self._path(session_id)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(item), ensure_ascii=False) + "\n")
            _private(path, 0o600)
        return item

    def for_session(self, session_id: str) -> dict[str, Feedback]:
        """Latest feedback per turn for one session."""
        path = self._path(session_id)
        latest: dict[str, Feedback] = {}
        if not path.is_file():
            return latest
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return latest
        for line in lines:
            try:
                data = json.loads(line)
                item = Feedback(
                    session_id=str(data["session_id"]), turn_id=str(data["turn_id"]),
                    rating=str(data["rating"]), correction=str(data.get("correction", "")),
                    at=float(data.get("at", 0.0)),
                )
            except (ValueError, KeyError, TypeError):
                continue
            if item.rating == "clear":
                latest.pop(item.turn_id, None)
            else:
                latest[item.turn_id] = item
        return latest

    def sessions(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(path.stem for path in self.root.glob("*.jsonl"))

    def all(self) -> list[Feedback]:
        out: list[Feedback] = []
        for session_id in self.sessions():
            out.extend(self.for_session(session_id).values())
        return out

    def counts(self) -> dict:
        items = self.all()
        return {
            "up": sum(1 for f in items if f.rating == "up"),
            "down": sum(1 for f in items if f.rating == "down" and not f.correction),
            "teach": sum(1 for f in items if f.correction),
            "consented": sum(1 for f in items if f.consented),
        }


def _private(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        if os.name != "nt":
            raise
