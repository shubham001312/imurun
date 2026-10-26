"""Supabase: read projects and organizations, create a project (admin, gated)."""
from __future__ import annotations

from ..base import ADMIN, READ, Capability, ConnectorError, require_token
from ._rest import BearerConnector


class SupabaseConnector(BearerConnector):
    id = "supabase"
    label = "Supabase"
    base = "https://api.supabase.com/v1"
    capabilities = (
        Capability("list_projects", READ),
        Capability("list_organizations", READ),
        Capability("create_project", ADMIN),
    )

    def execute(self, action: str, params: dict) -> dict:
        if action == "list_projects":
            return self._request("GET", "/projects")

        if action == "list_organizations":
            return self._request("GET", "/organizations")

        if action == "create_project":
            name = str(params.get("name", "")).strip()
            organization = require_token(params.get("organization", ""), "organization")
            db_pass = str(params.get("db_pass", ""))
            if not name or not db_pass:
                raise ConnectorError("name and db_pass are required")
            return self._request(
                "POST",
                "/projects",
                {"name": name, "organization_id": organization, "db_pass": db_pass},
            )

        raise ConnectorError(f"supabase has no action {action!r}")
