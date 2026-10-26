"""Render: read services and deploys, trigger a deploy or restart (gated)."""
from __future__ import annotations

from ..base import READ, WRITE, Capability, ConnectorError, require_token
from ._rest import BearerConnector


class RenderConnector(BearerConnector):
    id = "render"
    label = "Render"
    base = "https://api.render.com/v1"
    capabilities = (
        Capability("list_services", READ),
        Capability("list_deploys", READ),
        Capability("trigger_deploy", WRITE),
        Capability("restart_service", WRITE),
    )

    def execute(self, action: str, params: dict) -> dict:
        if action == "list_services":
            return self._request("GET", "/services")

        service = require_token(params.get("service", ""), "service")
        if action == "list_deploys":
            return self._request("GET", f"/services/{service}/deploys")

        if action == "trigger_deploy":
            return self._request("POST", f"/services/{service}/deploys", {})

        if action == "restart_service":
            return self._request("POST", f"/services/{service}/restart", {})

        raise ConnectorError(f"render has no action {action!r}")
