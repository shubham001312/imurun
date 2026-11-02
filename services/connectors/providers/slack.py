"""Slack connector (bot token): channels, history, post a message (gated).

Slack's Web API answers HTTP 200 with ``{"ok": false, "error": ...}`` on
failure; ``_slack`` turns that into a ``ConnectorError``.
"""
from __future__ import annotations

from ..base import READ, WRITE, Capability, ConnectorError, require_channel
from ._rest import BearerConnector, clamp, query_string


class SlackConnector(BearerConnector):
    id = "slack"
    label = "Slack"
    base = "https://slack.com/api"
    revoke_url = ""
    capabilities = (
        Capability("auth_test", READ),
        Capability("list_channels", READ),
        Capability("get_history", READ),
        Capability("post_message", WRITE),
    )

    def _slack(self, method: str, path: str, body=None) -> dict:
        headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else None
        result = self._request(method, path, body, headers=headers)
        if isinstance(result, dict) and result.get("ok") is False:
            raise ConnectorError(f"slack refused: {str(result.get('error') or 'unknown error')[:120]}")
        return result

    def disconnect(self) -> bool:
        if self.connected():
            try:
                self._slack("POST", "/auth.revoke", {})
            except Exception:  # noqa: BLE001 - forget locally anyway
                pass
        had = self.connected()
        self.forget()
        return had

    def execute(self, action: str, params: dict) -> dict:
        params = params or {}
        if action == "auth_test":
            return self._slack("POST", "/auth.test", {})
        if action == "list_channels":
            query = query_string({"types": "public_channel,private_channel", "exclude_archived": "true",
                                  "limit": clamp(params.get("limit"), 100, 1, 200)})
            return self._slack("GET", f"/conversations.list{query}")
        if action == "get_history":
            channel = require_channel(params.get("channel", "")).lstrip("#")
            query = query_string({"channel": channel, "limit": clamp(params.get("limit"), 20, 1, 100)})
            return self._slack("GET", f"/conversations.history{query}")
        if action == "post_message":
            channel = require_channel(params.get("channel", "")).lstrip("#")
            text = str(params.get("text", "")).strip()
            if not text:
                raise ConnectorError("text is required")
            body = {"channel": channel, "text": text[:40000]}
            thread = str(params.get("thread_ts", "")).strip()
            if thread:
                body["thread_ts"] = thread[:32]
            return self._slack("POST", "/chat.postMessage", body)
        raise ConnectorError(f"slack has no action {action!r}")
