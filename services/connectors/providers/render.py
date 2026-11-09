"""Render connector: services, deploys, logs; trigger a deploy, restart (gated)."""
from __future__ import annotations

from ..base import READ, WRITE, Capability, ConnectorError, require_token
from ._rest import BearerConnector, clamp, query_string


class RenderConnector(BearerConnector):
    id = "render"
    label = "Render"
    base = "https://api.render.com/v1"
    capabilities = (
        Capability("list_owners", READ),
        Capability("list_services", READ),
        Capability("list_deploys", READ),
        Capability("logs", READ),
        Capability("trigger_deploy", WRITE),
        Capability("restart_service", WRITE),
    )

    def verify_token(self, token: str) -> dict:
        """``GET /v1/owners`` with the candidate key: the workspaces it reaches."""
        owners = self._send("GET", f"{self.base}/owners?limit=20", token, None, None)
        names = []
        for item in owners if isinstance(owners, list) else []:
            owner = item.get("owner") if isinstance(item, dict) and "owner" in item else item
            if isinstance(owner, dict):
                names.append(str(owner.get("name") or owner.get("email") or owner.get("id") or ""))
        if not isinstance(owners, list):
            raise ConnectorError("render did not accept the API key")
        return {"account": ", ".join(n for n in names if n)[:200],
                "permissions": ["full access to these workspaces (Render API keys are not scoped)"]}

    def execute(self, action: str, params: dict) -> dict:
        params = params or {}
        if action == "list_owners":
            return {"items": self._request("GET", "/owners?limit=20")}
        if action == "list_services":
            query = query_string({"limit": clamp(params.get("limit"), 20, 1, 100)})
            return {"items": self._request("GET", f"/services{query}")}
        service = require_token(params.get("service", ""), "service")
        if action == "list_deploys":
            query = query_string({"limit": clamp(params.get("limit"), 10, 1, 100)})
            return {"items": self._request("GET", f"/services/{service}/deploys{query}")}
        if action == "logs":
            detail = self._request("GET", f"/services/{service}")
            owner = str((detail or {}).get("ownerId") or "")
            if not owner:
                raise ConnectorError("render did not say which workspace owns this service")
            query = query_string({"ownerId": require_token(owner, "ownerId"), "resource": service,
                                  "limit": clamp(params.get("limit"), 50, 1, 100), "direction": "backward"})
            return self._request("GET", f"/logs{query}")
        if action == "trigger_deploy":
            body = {"clearCache": "clear" if params.get("clear_cache") else "do_not_clear"}
            return self._request("POST", f"/services/{service}/deploys", body)
        if action == "restart_service":
            return self._request("POST", f"/services/{service}/restart", {})
        raise ConnectorError(f"render has no action {action!r}")
