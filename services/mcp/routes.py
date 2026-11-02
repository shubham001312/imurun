"""HTTP handlers for ``/api/mcp/*`` (dispatched from services/tools/routes.py
and, for DELETE, from api.py ``do_DELETE``).

GET    /api/mcp/servers                 every configured server with live status
GET    /api/mcp/servers/{id}            one server
GET    /api/mcp/servers/{id}/tools      the tools it offers (bridged names, schema, permission level)
POST   /api/mcp/servers                 add or update one server (body = server config)
POST   /api/mcp/servers/{id}/connect    connect now (waits for initialize + tools/list)
POST   /api/mcp/servers/{id}/disconnect close the connection (stays enabled; reconnects on next use)
DELETE /api/mcp/servers/{id}            remove it from Settings
"""
from __future__ import annotations

from typing import Any

from .client import McpError

Reply = tuple[int, dict]
PREFIX = ["api", "mcp", "servers"]


def _parts(path: str) -> list[str]:
    return [p for p in path.split("/") if p]


def _manager(server: Any):
    return getattr(server, "_mcp", None)


def handle_get(server: Any, path: str) -> Reply | None:
    parts = _parts(path)
    if parts[:3] != PREFIX:
        return None
    manager = _manager(server)
    if manager is None:
        return 501, {"error": "MCP is not wired"}
    if len(parts) == 3:
        return 200, {"servers": manager.servers_view()}
    if len(parts) == 4:
        view = manager.server_view(parts[3])
        return (200, view) if view is not None else (404, {"error": "unknown MCP server"})
    if len(parts) == 5 and parts[4] == "tools":
        tools = manager.tools_view(parts[3])
        if tools is None:
            return 404, {"error": "unknown MCP server"}
        view = manager.server_view(parts[3]) or {}
        return 200, {"id": parts[3], "status": view.get("status"), "tools": tools}
    return None


def is_post_route(path: str) -> bool:
    parts = _parts(path)
    if parts == PREFIX:
        return True
    return len(parts) == 5 and parts[:3] == PREFIX and parts[4] in ("connect", "disconnect")


def handle_post(server: Any, path: str, payload: dict) -> Reply:
    manager = _manager(server)
    if manager is None:
        return 501, {"error": "MCP is not wired"}
    parts = _parts(path)
    if parts == PREFIX:
        try:
            view = manager.upsert(payload)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        return 200, view
    server_id, action = parts[3], parts[4]
    if manager.server_view(server_id) is None:
        return 404, {"error": "unknown MCP server"}
    if action == "disconnect":
        manager.disconnect(server_id)
        return 200, manager.server_view(server_id) or {}
    view = manager.server_view(server_id) or {}
    if not view.get("enabled"):
        return 409, {"error": "the server is disabled; enable it first", "server": view}
    try:
        manager.connect(server_id)
    except KeyError:
        manager.sync(background=False, connect=False)
        try:
            manager.connect(server_id)
        except (KeyError, McpError) as exc:
            return 502, {"error": str(exc), "server": manager.server_view(server_id)}
    except McpError as exc:
        return 502, {"error": str(exc), "server": manager.server_view(server_id)}
    return 200, manager.server_view(server_id) or {}


def handle_delete(server: Any, path: str) -> Reply | None:
    parts = _parts(path)
    if len(parts) != 4 or parts[:3] != PREFIX:
        return None
    manager = _manager(server)
    if manager is None:
        return 501, {"error": "MCP is not wired"}
    if not manager.delete(parts[3]):
        return 404, {"error": "unknown MCP server"}
    return 200, {"deleted": parts[3]}
