"""Connectors: linked accounts, capability-gated, audited."""
from .base import ADMIN, READ, WRITE, Capability, Connector, ConnectorError, HttpTransport
from .gate import ConnectorGate
from .registry import ConnectorRegistry
from .tokens import KeyringTokenStore, MemoryTokenStore, TokenStoreUnavailable, redact
from .providers import (
    CanvaConnector,
    CloudflareConnector,
    GitHubConnector,
    GmailConnector,
    RenderConnector,
    SlackConnector,
    SupabaseConnector,
    VercelConnector,
)

__all__ = [
    "ADMIN",
    "READ",
    "WRITE",
    "CanvaConnector",
    "Capability",
    "CloudflareConnector",
    "Connector",
    "ConnectorError",
    "ConnectorGate",
    "ConnectorRegistry",
    "GitHubConnector",
    "GmailConnector",
    "HttpTransport",
    "KeyringTokenStore",
    "MemoryTokenStore",
    "RenderConnector",
    "SlackConnector",
    "SupabaseConnector",
    "TokenStoreUnavailable",
    "VercelConnector",
    "redact",
]
