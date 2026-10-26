"""Canva: read designs, create a design or start an export (gated)."""
from __future__ import annotations

from ..base import READ, WRITE, Capability, ConnectorError
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
            design_type = str(params.get("design_type", "")).strip()
            if not design_type:
                raise ConnectorError("design_type is required")
            return self._request("POST", "/designs", {"design_type": design_type})

        if action == "export_design":
            design_id = str(params.get("design_id", "")).strip()
            fmt = str(params.get("format", "")).strip()
            if not design_id or not fmt:
                raise ConnectorError("design_id and format are required")
            return self._request(
                "POST",
                "/exports",
                {"design_id": design_id, "format": {"type": fmt}},
            )

        raise ConnectorError(f"canva has no action {action!r}")
