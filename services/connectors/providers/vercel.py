"""Vercel: read projects and deployments, create a deployment (gated)."""
from __future__ import annotations

from ..base import READ, WRITE, Capability, ConnectorError
from ._rest import BearerConnector


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
            project = str(params.get("project", "")).strip()
            if not project:
                raise ConnectorError("project is required")
            return self._request("GET", f"/v6/deployments?projectId={project}")

        if action == "create_deployment":
            name = str(params.get("name", "")).strip()
            if not name:
                raise ConnectorError("name is required")
            body = {"name": name}
            if params.get("project"):
                body["project"] = str(params["project"])
            if params.get("target"):
                body["target"] = str(params["target"])
            return self._request("POST", "/v13/deployments", body)

        raise ConnectorError(f"vercel has no action {action!r}")
