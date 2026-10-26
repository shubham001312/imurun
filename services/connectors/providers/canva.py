"""Canva: read designs, create a design or start an export (gated)."""
from __future__ import annotations

from ..base import READ, WRITE, Capability, ConnectorError, require_token
from ._rest import BearerConnector


class CanvaConnector(BearerConnector):
    id = "canva"
    label = "Canva"
    base = "https://api.canva.com/rest/v1"
    capabilities = (
        Capability("list_designs", READ),
        Capability("create_design", WRITE),
        Capability("export_design", WRITE),
    )

    def execute(self, action: str, params: dict) -> dict:
        if action == "list_designs":
            return self._request("GET", "/designs")

        if action == "create_design":
            design_type = require_token(params.get("design_type", ""), "design_type")
            return self._request("POST", "/designs", {"design_type": design_type})

        if action == "export_design":
            design_id = require_token(params.get("design_id", ""), "design_id")
            fmt = require_token(params.get("format", ""), "format")
            return self._request(
                "POST",
                "/exports",
                {"design_id": design_id, "format": {"type": fmt}},
            )

        raise ConnectorError(f"canva has no action {action!r}")
