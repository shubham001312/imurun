"""One model tool per MCP tool: ``mcp_<server>_<tool>``.

``McpManager`` (services/mcp/registry.py) creates an ``McpTool`` for every
tool a connected MCP server lists and registers it in the ``ToolRegistry``.
The tool's JSON schema is the server's ``inputSchema`` (passed through), its
description is the server's, prefixed with the server name so the model
knows where it goes.

Permission (category ``mcp``, decided by ``connectors/tool_policy.py``):

* ``destructiveHint: true``            -> ``always`` (asks in every mode,
  ``auto_approve`` does not override it);
* tool listed in the server's ``auto_approve`` -> ``allow``;
* ``readOnlyHint: true``               -> ``read`` (asks only in mode ``ask``);
* anything else                        -> ``ask`` (asks in every mode, a
  "remember" answer holds for the session).

An MCP server is third-party code: hints are the server's own claims, which
is why the default without hints is to ask.
"""
from __future__ import annotations

import re
from typing import Any

from ..base import Tool, ToolResult, failed

MAX_DESCRIPTION = 600
_NAME_SAFE = re.compile(r"[^a-z0-9_]+")


def slug(text: str, limit: int = 40) -> str:
    value = _NAME_SAFE.sub("_", str(text or "").lower()).strip("_")
    value = re.sub(r"_+", "_", value)
    return value[:limit].strip("_") or "tool"


def bridged_name(server_id: str, tool_name: str) -> str:
    return f"mcp_{slug(server_id, 24)}_{slug(tool_name, 40)}"


def _schema(raw: Any) -> dict:
    schema = dict(raw) if isinstance(raw, dict) else {}
    schema["type"] = "object"
    if not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    required = schema.get("required")
    schema["required"] = [str(r) for r in required if isinstance(r, str)] if isinstance(required, list) else []
    return schema


class McpTool(Tool):
    category = "mcp"

    def __init__(self, manager: Any, server_id: str, server_name: str, spec: dict, name: str,
                 *, network: bool, auto_approve: bool) -> None:
        self.manager = manager
        self.server_id = server_id
        self.server_name = server_name
        self.remote_name = str(spec.get("name"))
        self.name = name
        self.title = str(spec.get("title") or (spec.get("annotations") or {}).get("title") or "")
        text = " ".join(str(spec.get("description") or self.title or self.remote_name).split())
        if len(text) > MAX_DESCRIPTION:
            text = text[:MAX_DESCRIPTION] + "..."
        self.description = f"[MCP {server_name}] {text}"
        self.parameters = _schema(spec.get("inputSchema"))
        self.annotations = dict(spec.get("annotations") or {})
        self.requires_network = bool(network)
        self.toggle = None if network else ()
        self.auto_approve = bool(auto_approve)
        self.retired = False

    def available(self) -> bool:
        return not self.retired and self.manager.tool_ready(self.server_id, self.name)

    def unavailable_reason(self) -> str:
        if self.retired:
            return "the MCP server no longer offers this tool"
        return self.manager.unavailable_reason(self.server_id) or ""

    def permission_level(self, args: dict) -> tuple[str, str]:
        where = f"runs {self.remote_name} on MCP server {self.server_name}"
        if self.annotations.get("destructiveHint") is True:
            return "always", f"{where}; the server marks it as destructive"
        if self.auto_approve:
            return "allow", f"{where}; auto-approved in the MCP settings"
        if self.annotations.get("readOnlyHint") is True:
            return "read", f"{where} (read-only)"
        return "ask", where

    def view(self) -> dict:
        level, _ = self.permission_level({})
        return {"name": self.name, "remote_name": self.remote_name, "title": self.title,
                "description": self.description, "parameters": self.parameters,
                "annotations": self.annotations, "permission_level": level,
                "auto_approve": self.auto_approve, "available": self.available()}

    def run(self, args: dict[str, Any]) -> ToolResult:
        if self.retired:
            return failed(self.name, "This MCP tool is no longer offered by its server.")
        try:
            return self.manager.call(self.server_id, self.remote_name, args, tool_name=self.name)
        except Exception as exc:  # noqa: BLE001 - a tool never raises
            return failed(self.name, f"The MCP call failed: {type(exc).__name__}")
