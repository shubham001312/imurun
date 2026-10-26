"""Connector base: one shape for every provider, capabilities classified."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol

READ = "read"
WRITE = "write"
ADMIN = "admin"
KINDS = (READ, WRITE, ADMIN)


class ConnectorError(RuntimeError):
    """A connector could not complete the requested action."""


@dataclass(frozen=True)
class Capability:
    action: str
    kind: str


class HttpTransport(Protocol):
    def request(self, method: str, url: str, headers: dict, json: dict | None = None) -> dict: ...


class Connector(ABC):
    id: str
    label: str
    capabilities: tuple[Capability, ...] = ()

    def capability(self, action: str) -> Capability | None:
        for capability in self.capabilities:
            if capability.action == action:
                return capability
        return None

    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def execute(self, action: str, params: dict) -> dict: ...

    def status(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "connected": self.connected(),
            "capabilities": [{"action": cap.action, "kind": cap.kind} for cap in self.capabilities],
        }
