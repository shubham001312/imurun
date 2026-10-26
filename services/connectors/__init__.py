"""Connectors: linked accounts, capability-gated, audited."""
from .base import ADMIN, READ, WRITE, Capability, Connector, ConnectorError, HttpTransport
from .gate import ConnectorGate
from .registry import ConnectorRegistry
from .tokens import KeyringTokenStore, MemoryTokenStore, TokenStoreUnavailable, redact
from .providers import GitHubConnector, GmailConnector

__all__ = [
    "ADMIN",
    "READ",
    "WRITE",
    "Capability",
    "Connector",
    "ConnectorError",
    "ConnectorGate",
    "ConnectorRegistry",
    "GitHubConnector",
    "GmailConnector",
    "HttpTransport",
    "KeyringTokenStore",
    "MemoryTokenStore",
    "TokenStoreUnavailable",
    "redact",
]
