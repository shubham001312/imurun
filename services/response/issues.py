"""Agent issue log: one JSON line per thing that went wrong in a turn.

``<data root>/logs/agent-issues.log`` records each failed or retried step
(an unreadable tool call, a "I cannot run commands" refusal while tools were
on offer, a tool that failed, a retry budget that ran out, a bridged command
that could not start), so the cause can be found and fixed later instead of
being lost in the chat. It is a diagnostic file for the owner of this
computer:

* never fatal - a log that cannot be written is ignored;
* secrets are scrubbed (API keys, bearer tokens, ``key=value`` credentials)
  and every field is length-capped;
* the file is rotated at 1 MB (one ``.1`` generation is kept).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_BYTES = 1_000_000
FIELD_CHARS = 600
_LOCK = threading.Lock()

_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{12,}"),          # OpenAI-style keys
    re.compile(r"\bnvapi-[A-Za-z0-9_\-]{12,}"),                   # NVIDIA
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),                  # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),               # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                          # AWS access key id
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-~+/]{12,}=*"),
    re.compile(r"(?i)\b((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[=:]\s*)\S+"),
)


def scrub(text: Any) -> str:
    """``text`` as a single capped line with anything key-like replaced."""
    value = str(text if text is not None else "")
    for pattern in _SECRET_PATTERNS:
        # Patterns with a group keep that prefix ("Bearer ", "api_key=").
        value = pattern.sub(lambda m: (m.group(1) if m.re.groups else "") + "****", value)
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", value)
    if len(value) > FIELD_CHARS:
        value = value[:FIELD_CHARS] + f"... [{len(value) - FIELD_CHARS} more]"
    return value


def log_path() -> Path:
    """``IMURUN_ISSUES_LOG`` overrides the file (the test suite points it at a temp file)."""
    import os

    override = os.environ.get("IMURUN_ISSUES_LOG")
    if override:
        return Path(override)
    from ..paths import data_root

    return data_root() / "logs" / "agent-issues.log"


def record(kind: str, *, session: str = "", turn: str = "", tool: str = "", error: Any = "",
           attempt: int = 0, detail: Any = "", path: Path | None = None) -> None:
    """Append one issue. ``kind`` is a short token such as ``tool-failed``."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": scrub(kind)[:40],
        "session": scrub(session)[:80],
        "turn": scrub(turn)[:40],
        "tool": scrub(tool)[:60],
        "attempt": int(attempt or 0),
        "error": scrub(error),
        "detail": scrub(detail),
    }
    target = path or log_path()
    try:
        with _LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size > MAX_BYTES:
                target.replace(target.with_name(target.name + ".1"))
            with target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.debug("agent issue log not written: %s", exc)
