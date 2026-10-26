"""Vercel: read projects and deployments, create a deployment (gated)."""
from __future__ import annotations

from ..base import READ, WRITE, Capability, ConnectorError, require_token
from ._rest import BearerConnector

TARGETS = ("production", "preview")


class VercelConnector(BearerConnector):
    id = "vercel"
    label = "Vercel"
    base = "https://api.vercel.com"
    capabilities = (
        Capability("list_projects", READ),
        Capability("list_deployments", READ),
        Capability("create_deployment", WRITE),
    )

    def execute(self, action: str, params: dict) -> dict:
        if action == "list_projects":
            return self._request("GET", "/v9/projects")

        if action == "list_deployments":
            project = require_token(params.get("project", ""), "project")
            return self._request("GET", f"/v6/deployments?projectId={project}")

        if action == "create_deployment":
            name = require_token(params.get("name", ""), "name")
            body = {"name": name}
            if params.get("project"):
                body["project"] = require_token(params["project"], "project")
            if params.get("target"):
                target = str(params["target"]).strip()
                if target not in TARGETS:
                    raise ConnectorError(f"target must be one of {TARGETS}")
                body["target"] = target
            return self._request("POST", "/v13/deployments", body)

        raise ConnectorError(f"vercel has no action {action!r}")
