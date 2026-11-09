"""Approval cards for tool calls that need the user's yes.

The connector routes already use the request/answer pattern "POST with
``approved: false`` -> 403 ask -> show a card -> POST again with
``approved: true``". A tool call inside a running turn cannot be re-posted,
so it waits instead:

1. ``request`` publishes ``tool.approval_needed`` with an ``approval_id``;
2. the turn thread blocks in ``wait`` (polling the stop flag, so Stop and
   the kill switch still work while a card is open);
3. the UI answers ``POST /api/tools/approvals/{id}`` with
   ``{"approved": true|false, "remember": bool}``;
4. ``resolve`` publishes ``tool.approved`` or ``tool.denied``.

A card that nobody answers expires (``permissions.approvalTimeout``, default
180 s) and counts as denied. When no client is subscribed to the event
stream nobody could ever answer, so the call is denied at once instead of
blocking the turn for minutes.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Approval:
    id: str
    tool: str
    args: dict[str, Any]
    reasons: tuple[str, ...]
    turn_id: str = ""
    session_id: str = ""
    call_id: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    approved: bool | None = None
    remember: bool = False
    reason: str = ""
    # How the call is gated (permissions.LEVELS) and whether "remember for
    # this session" applies; the card hides the checkbox when it does not.
    level: str = ""
    rememberable: bool = True
    done: threading.Event = field(default_factory=threading.Event)

    def view(self) -> dict[str, Any]:
        return {
            "approval_id": self.id,
            "tool": self.tool,
            "args": self.args,
            "reasons": list(self.reasons),
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "call_id": self.call_id,
            "created_at": self.created_at,
            "expires_in": max(0, round(self.expires_at - time.time())),
            "level": self.level,
            "rememberable": self.rememberable,
        }


class ApprovalBroker:
    """Pending approvals, keyed by id. Thread-safe."""

    def __init__(
        self,
        publish: Callable[[str, dict], int] | None = None,
        levels: Callable[[str, dict], tuple[str, bool]] | None = None,
    ) -> None:
        self._publish = publish
        # ``levels(tool_name, args) -> (level, rememberable)``: fills the card
        # when the caller did not pass them (the manual route, tests).
        self._levels = levels
        self._pending: dict[str, Approval] = {}
        self._lock = threading.Lock()

    def request(
        self,
        tool: str,
        args: dict[str, Any],
        reasons: tuple[str, ...],
        *,
        timeout: float = 180.0,
        turn_id: str = "",
        session_id: str = "",
        call_id: str = "",
        level: str = "",
        rememberable: bool | None = None,
    ) -> Approval:
        if (not level or rememberable is None) and self._levels is not None:
            try:
                found_level, found_remember = self._levels(tool, dict(args or {}))
                level = level or str(found_level or "")
                if rememberable is None:
                    rememberable = bool(found_remember)
            except Exception:  # noqa: BLE001 - the card still shows
                pass
        if rememberable is None:
            rememberable = True
        approval = Approval(
            id=uuid.uuid4().hex[:12],
            tool=tool,
            args=_preview_args(args),
            reasons=tuple(reasons),
            turn_id=turn_id,
            session_id=session_id,
            call_id=call_id,
            expires_at=time.time() + max(1.0, float(timeout)),
            level=level,
            rememberable=bool(rememberable),
        )
        with self._lock:
            self._pending[approval.id] = approval
        listeners = self._emit("tool.approval_needed", approval.view())
        if self._publish is not None and listeners == 0:
            self._finish(approval, False, "no client is connected to approve it")
        return approval

    def wait(self, approval: Approval, stop: Callable[[], bool] | None = None) -> bool:
        """Block until answered, expired or stopped. True only on approval."""
        while not approval.done.is_set():
            if stop is not None and stop():
                self._finish(approval, False, "the turn was stopped")
                break
            if time.time() >= approval.expires_at:
                self._finish(approval, False, "nobody answered in time")
                break
            approval.done.wait(0.25)
        return bool(approval.approved)

    def resolve(self, approval_id: str, approved: bool, *, remember: bool = False) -> Approval | None:
        with self._lock:
            approval = self._pending.get(approval_id)
        if approval is None:
            return None
        approval.remember = bool(remember)
        self._finish(approval, bool(approved), "approved by the user" if approved else "declined by the user")
        return approval

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [a.view() for a in self._pending.values()]

    def _finish(self, approval: Approval, approved: bool, reason: str) -> None:
        with self._lock:
            if approval.done.is_set():
                return
            approval.approved = approved
            approval.reason = reason
            self._pending.pop(approval.id, None)
            approval.done.set()
        self._emit(
            "tool.approved" if approved else "tool.denied",
            {
                "approval_id": approval.id,
                "tool": approval.tool,
                "turn_id": approval.turn_id,
                "call_id": approval.call_id,
                "reason": reason,
                "remember": approval.remember,
            },
        )

    def _emit(self, kind: str, data: dict) -> int:
        if self._publish is None:
            return 0
        try:
            return int(self._publish(kind, data) or 0)
        except Exception:  # noqa: BLE001 - a broken broker must not hang the turn
            return 0


def _preview_args(args: dict[str, Any], limit: int = 4000) -> dict[str, Any]:
    """Arguments for the card: long strings (file bodies) are shortened."""
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if isinstance(value, str) and len(value) > limit:
            out[key] = value[:limit] + f"... [{len(value) - limit} more characters]"
        else:
            out[key] = value
    return out
