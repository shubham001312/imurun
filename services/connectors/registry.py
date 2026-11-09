"""Connector registry: what is connected and what it can do."""
from __future__ import annotations

from .base import Connector


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        if connector.id in self._connectors:
            raise ValueError(f"connector {connector.id!r} is already registered")
        self._connectors[connector.id] = connector

    def get(self, connector_id: str) -> Connector | None:
        return self._connectors.get(connector_id)

    def ids(self) -> list[str]:
        return sorted(self._connectors)

    def list(self) -> list[dict]:
        return [connector.status() for connector in self._connectors.values()]
