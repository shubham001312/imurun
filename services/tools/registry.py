"""The tool registry.

Tools are registered once and looked up by name. ``execute`` never raises:
a broken tool becomes a failed result, because a tool failure must not take
the turn down with it.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterable

from .base import Tool, ToolResult


class ToolRegistry:
    """A thread-safe collection of tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._lock = threading.Lock()

    def register(self, tool: Tool) -> Tool:
        """Add a tool. Registering the same name twice replaces it."""
        if not tool.name:
            raise ValueError("a tool needs a name")
        with self._lock:
            self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> Tool | None:
        """Remove a tool (MCP tools of a removed or disabled server). Returns
        the tool that was removed, or None when there was none."""
        with self._lock:
            return self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        with self._lock:
            return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        with self._lock:
            return list(self._tools.values())

    def names(self) -> list[str]:
        with self._lock:
            return list(self._tools.keys())

    def available_tools(self) -> list[Tool]:
        """Tools that can run right now."""
        ready = []
        for tool in self.list_tools():
            try:
                if tool.available():
                    ready.append(tool)
            except Exception:  # noqa: BLE001 - a broken probe means unavailable
                continue
        return ready

    def execute(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        """Run a tool and always return a result.

        Exceptions become a failed result with a short message; the caller
        never has to guard the call.
        """
        tool = self.get(name)
        if tool is None:
            return ToolResult(
                ok=False,
                error=f"There is no tool called {name}.",
                content=f"There is no tool called {name}.",
            )
        started = time.monotonic()
        try:
            result = tool.run(dict(args or {}))
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            message = f"{type(exc).__name__}: {exc}"
            return ToolResult(
                ok=False,
                error=message,
                content=f"The {name} tool failed: {message}",
                source=name,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        if not isinstance(result, ToolResult):  # pragma: no cover - defensive
            result = ToolResult(ok=False, error="the tool returned no result", source=name)
        result.latency_ms = result.latency_ms or int((time.monotonic() - started) * 1000)
        if not result.source:
            result.source = name
        return result

    def status(self) -> dict[str, Any]:
        """Honest state of every tool, for the API and the shell."""
        tools = []
        for tool in self.list_tools():
            try:
                ready = bool(tool.available())
                reason = "" if ready else tool.unavailable_reason()
            except Exception as exc:  # noqa: BLE001
                ready = False
                reason = str(exc)
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "requires_network": tool.requires_network,
                    "available": ready,
                    "reason": reason,
                    "category": getattr(tool, "category", "info"),
                    "parameters": tool.schema()["parameters"] if hasattr(tool, "schema") else {},
                }
            )
        return {"tools": tools}

    def extend(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def bind_workspace(self, workspace: Any) -> int:
        """Give every workspace-bound tool the same ``Workspace`` object.

        The server calls this with the permission policy's workspace so the
        policy and the tools can never disagree about the root. Returns the
        number of tools bound.
        """
        count = 0
        for tool in self.list_tools():
            if hasattr(tool, "workspace"):
                tool.workspace = workspace
                count += 1
        return count

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.list_tools()]
