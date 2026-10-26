"""Cloudflare: read zones and DNS, change DNS and purge cache (gated)."""
from __future__ import annotations

from ..base import READ, WRITE, Capability, ConnectorError
from ._rest import BearerConnector


class CloudflareConnector(BearerConnector):
    id = "cloudflare"
    label = "Cloudflare"
    base = "https://api.cloudflare.com/client/v4"
    capabilities = (
        Capability("list_zones", READ),
        Capability("list_dns_records", READ),
        Capability("create_dns_record", WRITE),
        Capability("update_dns_record", WRITE),
        Capability("purge_cache", WRITE),
    )

    def execute(self, action: str, params: dict) -> dict:
        if action == "list_zones":
            return self._request("GET", "/zones")

        zone = str(params.get("zone", "")).strip()
        if action == "list_dns_records":
            if not zone:
                raise ConnectorError("zone is required")
            return self._request("GET", f"/zones/{zone}/dns_records")

        if action == "create_dns_record":
            record_type = str(params.get("type", "")).strip()
            name = str(params.get("name", "")).strip()
            content = str(params.get("content", "")).strip()
            if not zone or not record_type or not name or not content:
                raise ConnectorError("zone, type, name and content are required")
            return self._request(
                "POST",
                f"/zones/{zone}/dns_records",
                {"type": record_type, "name": name, "content": content},
            )

        if action == "update_dns_record":
            record_id = str(params.get("record_id", "")).strip()
            if not zone or not record_id:
                raise ConnectorError("zone and record_id are required")
            body = {
                key: str(params[key])
                for key in ("type", "name", "content")
                if params.get(key)
            }
            return self._request("PATCH", f"/zones/{zone}/dns_records/{record_id}", body)

        if action == "purge_cache":
            if not zone:
                raise ConnectorError("zone is required")
            return self._request("POST", f"/zones/{zone}/purge_cache", {"purge_everything": True})

        raise ConnectorError(f"cloudflare has no action {action!r}")
