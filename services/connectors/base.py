"""Connector base: one shape for every provider, capabilities classified."""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

READ = "read"
WRITE = "write"
ADMIN = "admin"
KINDS = (READ, WRITE, ADMIN)

SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
SAFE_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SAFE_CHANNEL = re.compile(r"^#?[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class ConnectorError(RuntimeError):
    """A connector could not complete the requested action."""


def require_token(value, field: str) -> str:
    text = str(value).strip()
    if not SAFE_TOKEN.fullmatch(text):
        raise ConnectorError(f"{field} is not a valid identifier")
    return text


def require_repo(value, field: str = "repo") -> str:
    text = str(value).strip()
    if not SAFE_REPO.fullmatch(text):
        raise ConnectorError(f"{field} must look like owner/name")
    return text


def require_channel(value) -> str:
    text = str(value).strip()
    if not SAFE_CHANNEL.fullmatch(text):
        raise ConnectorError("channel is not a valid identifier")
    return text


def quote_query(value) -> str:
    """Query values are data, never extra parameters."""
    return quote(str(value), safe="")


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
