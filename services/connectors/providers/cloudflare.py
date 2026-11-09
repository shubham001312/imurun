"""Cloudflare connector: zones, DNS records (read/write/delete), Workers, cache purge."""
from __future__ import annotations

import re

from ..base import READ, WRITE, Capability, ConnectorError, require_token
from ._rest import BearerConnector, clamp, query_string

RECORD_TYPE = re.compile(r"^[A-Za-z]{1,10}$")


def _result(response) -> dict:
    if isinstance(response, dict) and response.get("success") is False:
        errors = response.get("errors") or [{}]
        raise ConnectorError(f"cloudflare refused: {str((errors[0] or {}).get('message') or 'unknown')[:160]}")
    return response


class CloudflareConnector(BearerConnector):
    id = "cloudflare"
    label = "Cloudflare"
    base = "https://api.cloudflare.com/client/v4"
    capabilities = (
        Capability("list_accounts", READ),
        Capability("list_zones", READ),
        Capability("list_dns_records", READ),
        Capability("list_workers", READ),
        Capability("create_dns_record", WRITE),
        Capability("update_dns_record", WRITE),
        Capability("delete_dns_record", WRITE),
        Capability("purge_cache", WRITE),
    )

    def _cf(self, method: str, path: str, body=None) -> dict:
        return _result(self._request(method, path, body))

    def verify_token(self, token: str) -> dict:
        """``/user/tokens/verify``, then the accounts and (if readable) the policies."""
        def call(path: str) -> dict:
            return _result(self._send("GET", f"{self.base}{path}", token, None, None))

        verified = (call("/user/tokens/verify") or {}).get("result") or {}
        if str(verified.get("status") or "").lower() != "active":
            raise ConnectorError(f"cloudflare says the token is {verified.get('status') or 'not active'}")
        token_id = str(verified.get("id") or "")
        accounts: list[str] = []
        try:
            accounts = [str(a.get("name")) for a in (call("/accounts?per_page=20").get("result") or [])
                        if isinstance(a, dict) and a.get("name")]
        except ConnectorError:
            pass
        if not accounts:
            try:
                user = call("/user").get("result") or {}
                accounts = [str(user.get("email") or "")] if user.get("email") else []
            except ConnectorError:
                pass
        permissions: list[str] = []
        if token_id:
            try:
                # Only readable when the token also has "API Tokens:Read".
                details = call(f"/user/tokens/{require_token(token_id, 'token id')}").get("result") or {}
                for policy in details.get("policies") or []:
                    for group in policy.get("permission_groups") or []:
                        if group.get("name") and group["name"] not in permissions:
                            permissions.append(str(group["name"]))
            except ConnectorError:
                permissions = []
        if not permissions:
            permissions = ["unknown: the token cannot read its own permissions (add API Tokens:Read to list them)"]
        expires = verified.get("expires_on") or None
        expires_at = None
        if expires:
            from datetime import datetime

            try:
                expires_at = datetime.fromisoformat(str(expires).replace("Z", "+00:00")).timestamp()
            except ValueError:
                expires_at = None
        return {"account": ", ".join(accounts[:5]), "permissions": permissions, "expires": expires,
                "expires_at": expires_at, "token_id": token_id}

    def execute(self, action: str, params: dict) -> dict:
        params = params or {}
        if action == "list_accounts":
            return self._cf("GET", "/accounts?per_page=20")
        if action == "list_zones":
            query = query_string({"per_page": clamp(params.get("limit"), 20, 1, 50),
                                  "name": str(params.get("name") or "").strip()})
            return self._cf("GET", f"/zones{query}")
        if action == "list_workers":
            account = require_token(params.get("account", ""), "account")
            return self._cf("GET", f"/accounts/{account}/workers/scripts")
        zone = require_token(params.get("zone", ""), "zone")
        if action == "list_dns_records":
            query = query_string({"per_page": clamp(params.get("limit"), 50, 1, 100),
                                  "type": str(params.get("type") or "").upper()})
            return self._cf("GET", f"/zones/{zone}/dns_records{query}")
        if action in ("create_dns_record", "update_dns_record"):
            body: dict = {}
            record_type = str(params.get("type", "")).strip().upper()
            if record_type:
                if not RECORD_TYPE.fullmatch(record_type):
                    raise ConnectorError("type is not a DNS record type")
                body["type"] = record_type
            for key in ("name", "content"):
                value = str(params.get(key, "")).strip()
                if value:
                    body[key] = value[:2048]
            if "ttl" in params and params.get("ttl") not in (None, ""):
                body["ttl"] = clamp(params.get("ttl"), 1, 1, 86400)
            if params.get("proxied") is not None and params.get("proxied") != "":
                body["proxied"] = bool(params.get("proxied"))
            if action == "create_dns_record":
                if not all(body.get(k) for k in ("type", "name", "content")):
                    raise ConnectorError("type, name and content are required")
                return self._cf("POST", f"/zones/{zone}/dns_records", body)
            record = require_token(params.get("record_id", ""), "record_id")
            if not body:
                raise ConnectorError("nothing to change")
            return self._cf("PATCH", f"/zones/{zone}/dns_records/{record}", body)
        if action == "delete_dns_record":
            record = require_token(params.get("record_id", ""), "record_id")
            return self._cf("DELETE", f"/zones/{zone}/dns_records/{record}")
        if action == "purge_cache":
            return self._cf("POST", f"/zones/{zone}/purge_cache", {"purge_everything": True})
        raise ConnectorError(f"cloudflare has no action {action!r}")
