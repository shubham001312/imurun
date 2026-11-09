"""Vercel connector: projects, deployments, build logs; redeploy (gated)."""
from __future__ import annotations

from ..base import READ, WRITE, Capability, ConnectorError, require_token
from ._rest import BearerConnector, clamp, query_string

TARGETS = ("production", "preview")


class VercelConnector(BearerConnector):
    id = "vercel"
    label = "Vercel"
    base = "https://api.vercel.com"
    capabilities = (
        Capability("user", READ),
        Capability("list_projects", READ),
        Capability("list_deployments", READ),
        Capability("deployment_events", READ),
        Capability("redeploy", WRITE),
    )

    def execute(self, action: str, params: dict) -> dict:
        params = params or {}
        team = str(params.get("team_id") or "").strip()
        team_q = {"teamId": require_token(team, "team_id")} if team else {}
        if action == "user":
            return self._request("GET", "/v2/user")
        if action == "list_projects":
            query = query_string({**team_q, "limit": clamp(params.get("limit"), 20, 1, 100)})
            return self._request("GET", f"/v9/projects{query}")
        if action == "list_deployments":
            project = str(params.get("project") or "").strip()
            query = query_string({**team_q, "limit": clamp(params.get("limit"), 10, 1, 100),
                                  "projectId": require_token(project, "project") if project else ""})
            return self._request("GET", f"/v6/deployments{query}")
        if action == "deployment_events":
            deployment = require_token(params.get("deployment", ""), "deployment")
            query = query_string({**team_q, "limit": clamp(params.get("limit"), 100, 1, 1000)})
            return {"events": self._request("GET", f"/v3/deployments/{deployment}/events{query}")}
        if action == "redeploy":
            deployment = require_token(params.get("deployment", ""), "deployment")
            name = str(params.get("name", "")).strip()
            if not name:
                raise ConnectorError("name (the project name) is required")
            target = str(params.get("target") or "preview").strip()
            if target not in TARGETS:
                raise ConnectorError("target must be production or preview")
            body = {"name": name[:100], "deploymentId": deployment, "target": target}
            return self._request("POST", f"/v13/deployments{query_string(team_q)}", body)
        raise ConnectorError(f"vercel has no action {action!r}")
