"""GitHub connector: repos, issues, pull requests, file reads; issues and comments (gated)."""
from __future__ import annotations

import re
from urllib.parse import quote

from ..base import READ, WRITE, Capability, ConnectorError, require_repo
from ._rest import BearerConnector, clamp, query_string

STATES = ("open", "closed", "all")
_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_PATH = re.compile(r"^[^\x00-\x1f\\]{1,400}$")


def _number(value) -> str:
    text = str(value or "").strip().lstrip("#")
    if not text.isdigit() or int(text) <= 0:
        raise ConnectorError("number must be a positive integer")
    return text


def _state(value) -> str:
    state = str(value or "open").strip().lower()
    if state not in STATES:
        raise ConnectorError("state must be open, closed or all")
    return state


def _file_path(value) -> str:
    text = str(value or "").strip().strip("/")
    if not text or not _PATH.fullmatch(text) or ".." in text.split("/"):
        raise ConnectorError("path must be a file path inside the repository")
    return "/".join(quote(part, safe="") for part in text.split("/"))


class GitHubConnector(BearerConnector):
    id = "github"
    label = "GitHub"
    base = "https://api.github.com"
    extra_headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    capabilities = (
        Capability("user", READ),
        Capability("list_repos", READ),
        Capability("list_issues", READ),
        Capability("get_issue", READ),
        Capability("list_pulls", READ),
        Capability("get_pull", READ),
        Capability("read_file", READ),
        Capability("create_issue", WRITE),
        Capability("comment", WRITE),
    )

    def execute(self, action: str, params: dict) -> dict:
        params = params or {}
        if action == "user":
            return self._request("GET", "/user")
        if action == "list_repos":
            query = query_string({"per_page": clamp(params.get("limit"), 20, 1, 100),
                                  "sort": "updated", "affiliation": "owner,collaborator,organization_member"})
            return {"items": self._request("GET", f"/user/repos{query}")}
        repo = require_repo(params.get("repo", ""))
        if action == "list_issues":
            query = query_string({"state": _state(params.get("state")),
                                  "per_page": clamp(params.get("limit"), 20, 1, 100)})
            return {"items": self._request("GET", f"/repos/{repo}/issues{query}")}
        if action == "get_issue":
            number = _number(params.get("number"))
            issue = self._request("GET", f"/repos/{repo}/issues/{number}")
            comments = self._request("GET", f"/repos/{repo}/issues/{number}/comments?per_page=20")
            return {"issue": issue, "comments": comments}
        if action == "list_pulls":
            query = query_string({"state": _state(params.get("state")),
                                  "per_page": clamp(params.get("limit"), 20, 1, 100)})
            return {"items": self._request("GET", f"/repos/{repo}/pulls{query}")}
        if action == "get_pull":
            number = _number(params.get("number"))
            pull = self._request("GET", f"/repos/{repo}/pulls/{number}")
            files = self._request("GET", f"/repos/{repo}/pulls/{number}/files?per_page=50")
            return {"pull": pull, "files": files}
        if action == "read_file":
            path = _file_path(params.get("path"))
            ref = str(params.get("ref") or "").strip()
            if ref and (not _REF.fullmatch(ref) or ".." in ref):
                raise ConnectorError("ref is not a valid branch, tag or commit")
            return self._request("GET", f"/repos/{repo}/contents/{path}{query_string({'ref': ref})}")
        if action == "create_issue":
            title = str(params.get("title", "")).strip()
            if not title:
                raise ConnectorError("title is required")
            return self._request("POST", f"/repos/{repo}/issues",
                                 {"title": title[:256], "body": str(params.get("body", ""))[:60000]})
        if action == "comment":
            number = _number(params.get("number"))
            body = str(params.get("body", "")).strip()
            if not body:
                raise ConnectorError("body is required")
            return self._request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body[:60000]})
        raise ConnectorError(f"github has no action {action!r}")
