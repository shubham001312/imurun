"""Slack: read channels and history, post messages (gated)."""
from __future__ import annotations

from ..base import READ, WRITE, Capability, ConnectorError, require_channel
from ._rest import BearerConnector


class SlackConnector(BearerConnector):
    id = "slack"
    label = "Slack"
    base = "https://slack.com/api"
    capabilities = (
        Capability("list_channels", READ),
        Capability("get_history", READ),
        Capability("post_message", WRITE),
    )

    def execute(self, action: str, params: dict) -> dict:
        if action == "list_channels":
            return self._request("GET", "/conversations.list")

        if action == "get_history":
            channel = require_channel(params.get("channel", ""))
            return self._request("GET", f"/conversations.history?channel={channel}")

        if action == "post_message":
            channel = require_channel(params.get("channel", ""))
            text = str(params.get("text", "")).strip()
            if not text:
                raise ConnectorError("text is required")
            return self._request("POST", "/chat.postMessage", {"channel": channel, "text": text})

        raise ConnectorError(f"slack has no action {action!r}")
