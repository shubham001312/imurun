"""Shared shape for bearer-token REST providers: token in, error out, no secrets."""
from __future__ import annotations

from ..base import Connector, ConnectorError


class BearerConnector(Connector):
    base: str = ""

    def __init__(self, transport, tokens) -> None:
        self._transport = transport
        self._tokens = tokens

    def connected(self) -> bool:
        return self._tokens.get(self.id) is not None

    def _headers(self) -> dict:
        token = self._tokens.get(self.id)
        if not token:
            raise ConnectorError(f"{self.id} is not connected")
        return {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = self._headers()
        url = f"{self.base}{path}"
        if body is None:
            return self._transport.request(method, url, headers)
        return self._transport.request(method, url, headers, body)
