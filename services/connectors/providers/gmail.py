"""Gmail connector: read messages, create drafts, send (draft-first, gated)."""
from __future__ import annotations

from ..base import (
    READ,
    WRITE,
    Capability,
    Connector,
    ConnectorError,
    HttpTransport,
    quote_query,
    require_token,
)

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailConnector(Connector):
    id = "gmail"
    label = "Gmail"
    capabilities = (
        Capability("list_messages", READ),
        Capability("get_message", READ),
        Capability("create_draft", WRITE),
        Capability("send", WRITE),
    )

    def __init__(self, transport: HttpTransport, tokens) -> None:
        self._transport = transport
        self._tokens = tokens

    def connected(self) -> bool:
        return self._tokens.get("gmail") is not None

    def _headers(self) -> dict:
        token = self._tokens.get("gmail")
        if not token:
            raise ConnectorError("gmail is not connected")
        return {"Authorization": f"Bearer {token}"}

    def execute(self, action: str, params: dict) -> dict:
        headers = self._headers()

        if action == "list_messages":
            query = str(params.get("q", "")).strip()
            suffix = f"?q={quote_query(query)}" if query else ""
            return self._transport.request("GET", f"{BASE}/messages{suffix}", headers)

        if action == "get_message":
            message_id = require_token(params.get("id", ""), "id")
            return self._transport.request("GET", f"{BASE}/messages/{message_id}", headers)

        if action == "create_draft":
            raw = str(params.get("raw", "")).strip()
            if not raw:
                raise ConnectorError("raw is required")
            return self._transport.request("POST", f"{BASE}/drafts", headers, {"message": {"raw": raw}})

        if action == "send":
            raw = str(params.get("raw", "")).strip()
            if not raw:
                raise ConnectorError("raw is required")
            return self._transport.request("POST", f"{BASE}/messages/send", headers, {"raw": raw})

        raise ConnectorError(f"gmail has no action {action!r}")
