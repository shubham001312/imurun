"""Supabase connector: projects (Management API) and table rows (PostgREST).

The OAuth token is a Management API token: it lists organisations and
projects and can read a project's API keys. Table access goes through the
project's PostgREST endpoint (``https://<ref>.supabase.co/rest/v1``) with one
of those keys - the ``anon`` key by default, so Row Level Security applies;
``connectors.supabase.keyRole = "service_role"`` switches to the key that
bypasses RLS. Keys are fetched on demand, kept in memory only, never stored
or logged.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import quote

from ..base import ADMIN, READ, WRITE, Capability, ConnectorError, require_token
from ._rest import BearerConnector, clamp

PROJECT_REF = re.compile(r"^[a-z0-9]{10,40}$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
OPS = ("eq", "neq", "gt", "gte", "lt", "lte", "like", "ilike", "is")
KEY_TTL = 600.0
MAX_ROWS = 100


def _ref(value) -> str:
    text = str(value or "").strip()
    if not PROJECT_REF.fullmatch(text):
        raise ConnectorError("project must be a Supabase project ref (lower-case letters and digits)")
    return text


def _ident(value, field: str) -> str:
    text = str(value or "").strip()
    if not IDENT.fullmatch(text):
        raise ConnectorError(f"{field} must be a plain table/column name")
    return text


def _filters(value) -> list[str]:
    """``["status=eq.open", "age=gt.30"]`` -> safe PostgREST query pairs."""
    if value in (None, "", []):
        return []
    items = value if isinstance(value, list) else str(value).split("&")
    out = []
    for item in items[:10]:
        column, sep, rest = str(item).partition("=")
        op, dot, operand = rest.partition(".")
        if not sep or not dot or op not in OPS:
            raise ConnectorError(f"filter {item!r} must look like column=op.value (op: {', '.join(OPS)})")
        out.append(f"{_ident(column, 'filter column')}={op}.{quote(operand, safe='')}")
    return out


class SupabaseConnector(BearerConnector):
    id = "supabase"
    label = "Supabase"
    base = "https://api.supabase.com/v1"
    capabilities = (
        Capability("list_projects", READ),
        Capability("list_organizations", READ),
        Capability("list_tables", READ),
        Capability("select_rows", READ),
        Capability("insert_rows", WRITE),
        Capability("create_project", ADMIN),
    )

    def __init__(self, transport, tokens, **kwargs) -> None:
        super().__init__(transport, tokens, **kwargs)
        # "anon" or "service_role", or a callable returning one (wiring reads
        # connectors.supabase.keyRole on every call).
        self.key_role = "anon"
        self._keys: dict[tuple[str, str], tuple[float, str]] = {}
        self._key_lock = threading.Lock()

    def forget(self) -> None:
        super().forget()
        with self._key_lock:
            self._keys.clear()

    def _project_key(self, ref: str) -> str:
        role = self.key_role() if callable(self.key_role) else self.key_role
        role = role if role in ("anon", "service_role") else "anon"
        with self._key_lock:
            cached = self._keys.get((ref, role))
            if cached and time.time() - cached[0] < KEY_TTL:
                return cached[1]
        keys = self._request("GET", f"/projects/{ref}/api-keys")
        match = next((k for k in keys or [] if isinstance(k, dict) and k.get("name") == role and k.get("api_key")), None)
        if match is None:
            raise ConnectorError(f"supabase did not return the {role} key for this project")
        with self._key_lock:
            self._keys[(ref, role)] = (time.time(), str(match["api_key"]))
        return str(match["api_key"])

    def _rest(self, method: str, ref: str, path: str, body=None, prefer: str = ""):
        key = self._project_key(ref)
        headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
        if prefer:
            headers["Prefer"] = prefer
        url = f"https://{ref}.supabase.co/rest/v1{path}"
        if body is None:
            return self._transport.request(method, url, headers)
        return self._transport.request(method, url, headers, body)

    def execute(self, action: str, params: dict) -> dict:
        params = params or {}
        if action == "list_projects":
            return {"items": self._request("GET", "/projects")}
        if action == "list_organizations":
            return {"items": self._request("GET", "/organizations")}
        if action == "create_project":
            name = str(params.get("name", "")).strip()
            organization = require_token(params.get("organization", ""), "organization")
            db_pass = str(params.get("db_pass", ""))
            if not name or len(db_pass) < 12:
                raise ConnectorError("name and a db_pass of at least 12 characters are required")
            return self._request("POST", "/projects", {"name": name, "organization_id": organization,
                                                       "db_pass": db_pass, "region": str(params.get("region") or "us-east-1")})
        ref = _ref(params.get("project"))
        if action == "list_tables":
            spec = self._rest("GET", ref, "/")
            paths = (spec or {}).get("paths") or {}
            tables = sorted(p.strip("/") for p in paths if p.strip("/") and "/" not in p.strip("/")
                            and not p.startswith("/rpc"))
            return {"tables": tables}
        table = _ident(params.get("table"), "table")
        if action == "select_rows":
            columns = params.get("columns") or "*"
            if isinstance(columns, list):
                columns = ",".join(columns)
            if columns != "*":
                columns = ",".join(_ident(c, "column") for c in str(columns).split(",") if c.strip())
            query = [f"select={quote(columns, safe=',*')}", f"limit={clamp(params.get('limit'), 20, 1, MAX_ROWS)}"]
            query += _filters(params.get("filters"))
            return {"rows": self._rest("GET", ref, f"/{table}?{'&'.join(query)}")}
        if action == "insert_rows":
            rows = params.get("rows")
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
                raise ConnectorError("rows must be an object or a list of objects")
            if len(rows) > MAX_ROWS:
                raise ConnectorError(f"at most {MAX_ROWS} rows per insert")
            for row in rows:
                for column in row:
                    _ident(column, "column")
            return {"rows": self._rest("POST", ref, f"/{table}", rows, prefer="return=representation")}
        raise ConnectorError(f"supabase has no action {action!r}")
