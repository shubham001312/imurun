"""GitHub connector: read repos/issues, open issues and comments (gated)."""
from __future__ import annotations

from ..base import (
    READ,
    WRITE,
    Capability,
    Connector,
    ConnectorError,
    HttpTransport,
    require_repo,
)


class GitHubConnector(Connector):
    id = "github"
    label = "GitHub"
    capabilities = (
        Capability("list_repos", READ),
        Capability("list_issues", READ),
        Capability("create_issue", WRITE),
        Capability("comment", WRITE),
    )

    def __init__(self, transport: HttpTransport, tokens) -> None:
        self._transport = transport
        self._tokens = tokens

    def connected(self) -> bool:
        return self._tokens.get("github") is not None

    def _headers(self) -> dict:
        token = self._tokens.get("github")
        if not token:
            raise ConnectorError("github is not connected")
        return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def execute(self, action: str, params: dict) -> dict:
        headers = self._headers()

        if action == "list_repos":
            return self._transport.request("GET", "https://api.github.com/user/repos", headers)

        if action == "list_issues":
            repo = require_repo(params.get("repo", ""))
            return self._transport.request(
                "GET", f"https://api.github.com/repos/{repo}/issues", headers
            )

        if action == "create_issue":
            repo = require_repo(params.get("repo", ""))
            title = str(params.get("title", "")).strip()
            if not title:
                raise ConnectorError("title is required")
            return self._transport.request(
                "POST",
                f"https://api.github.com/repos/{repo}/issues",
                headers,
                {"title": title, "body": str(params.get("body", ""))},
            )

        if action == "comment":
            repo = require_repo(params.get("repo", ""))
            number = str(params.get("number", "")).strip()
            if not number.isdigit():
                raise ConnectorError("number must be a positive integer")
            body = str(params.get("body", "")).strip()
            if not body:
                raise ConnectorError("body is required")
            return self._transport.request(
                "POST",
                f"https://api.github.com/repos/{repo}/issues/{number}/comments",
                headers,
                {"body": body},
            )

        raise ConnectorError(f"github has no action {action!r}")
