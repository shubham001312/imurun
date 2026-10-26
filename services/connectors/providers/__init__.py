"""Provider adapters. Add one file per service; the gate stays the same."""
from .github import GitHubConnector
from .gmail import GmailConnector

__all__ = ["GitHubConnector", "GmailConnector"]
