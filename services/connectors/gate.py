"""The connector gate: reads are free, writes and admin actions need approval.

Every action is audited with provider, action and outcome - never the token
or the payload (V29).
"""
from __future__ import annotations

from ..safety.decisions import Decision, PermissionRequired, require
from .base import READ, Connector, ConnectorError


class ConnectorGate:
    def __init__(self, mark=None) -> None:
        self._mark = mark

    def _record(self, subject: str, why: str) -> None:
        if self._mark is None:
            return
        try:
            self._mark("G3-connector", subject, why)
        except Exception:  # noqa: BLE001 - auditing must never break the action
            pass

    def check(self, connector: Connector, action: str, approved: bool = False) -> Decision:
        capability = connector.capability(action)
        if capability is None:
            return Decision("deny", "G3-connector", (f"{connector.id} has no action {action!r}",))
        if capability.kind == READ:
            return Decision("allow", "G3-connector", (f"{connector.id}:{action} only reads",))
        if approved:
            return Decision("allow", "G3-connector", ("approved by the user for this action",))
        return Decision(
            "ask",
            "G3-connector",
            (f"{connector.id}:{action} changes data and needs your approval",),
        )

    def execute(
        self,
        connector: Connector,
        action: str,
        params: dict,
        *,
        approved: bool = False,
    ) -> dict:
        decision = self.check(connector, action, approved=approved)
        try:
            require(decision, granted=approved)
        except PermissionRequired:
            self._record(f"{connector.id}:{action}", f"refused: {decision.kind}")
            raise
        try:
            result = connector.execute(action, params)
        except Exception as exc:
            self._record(f"{connector.id}:{action}", f"failed: {type(exc).__name__}")
            raise ConnectorError(f"{connector.id}:{action} failed: {exc}") from exc
        self._record(f"{connector.id}:{action}", "ok")
        return result
