"""IMURUN tools: live facts, files, commands and sub-agents for the model.

The package is standard-library only. It holds:

* ``base`` - the tool contract, the result shape and permission categories;
* ``network`` / ``http`` - the connectivity probe and a capped HTTP client;
* ``registry`` - the collection of tools; a tool failure never raises;
* ``builtin`` - the tools: clock, weather, web search and page reading,
  locality, workspace files, shell, and ``run_agent``;
* ``workspace`` / ``permissions`` / ``approvals`` - the security model:
  one workspace root, allow / ask / deny per call, approval cards;
* ``calls`` / ``protocol`` - the text format the model uses to call a tool
  and the forgiving parser that reads it;
* ``executor`` - the single gate every call goes through;
* ``policy`` / ``runner`` - the deterministic pre-fetch fast path;
* ``routes`` - the HTTP handlers for ``/api/tools*`` and ``/api/permissions``.

Connector and MCP tools are not in ``default_registry``: they need a live
connector / MCP server, so ``services.connectors.wiring.wire_server``
registers them on the running server (``builtin/gmail.py``: ``gmail_*``;
``builtin/mcp_bridge.py``: ``mcp_<server>_<tool>``). See ``docs/connectors/``.

See ``docs/agentic/README.md`` for the whole picture.
"""
from __future__ import annotations

from .base import Tool, ToolResult
from .builtin import (
    FileCopyTool,
    FileDeleteTool,
    FileListTool,
    FileMoveTool,
    FileReadTool,
    FileWriteTool,
    LocationTool,
    MakeDirTool,
    RunAgentTool,
    ShellTool,
    TimeNowTool,
    WeatherTool,
    WebFetchTool,
    WebSearchTool,
    get_client_location,
    set_client_location,
)
from .builtin.gmail import GmailContext, gmail_tools
from .network import OfflineError, network_available, reset_cache
from .policy import detect_intents, extract_place, format_observation, tool_instructions
from .registry import ToolRegistry
from .runner import ToolRunner

__all__ = [
    "FileCopyTool",
    "FileDeleteTool",
    "FileListTool",
    "FileMoveTool",
    "FileReadTool",
    "FileWriteTool",
    "GmailContext",
    "LocationTool",
    "MakeDirTool",
    "OfflineError",
    "RunAgentTool",
    "ShellTool",
    "TimeNowTool",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "ToolRunner",
    "WeatherTool",
    "WebFetchTool",
    "WebSearchTool",
    "default_registry",
    "detect_intents",
    "extract_place",
    "format_observation",
    "get_client_location",
    "gmail_tools",
    "network_available",
    "reset_cache",
    "set_client_location",
    "tool_instructions",
]


def default_registry(workspace=None) -> ToolRegistry:
    """A registry with every built-in tool in it.

    ``workspace`` is optional: the server rebinds the workspace-bound tools
    to the permission policy's workspace (``ToolRegistry.bind_workspace``).
    """
    registry = ToolRegistry()
    registry.extend(
        [
            TimeNowTool(),
            WeatherTool(),
            WebSearchTool(),
            WebFetchTool(),
            LocationTool(),
            FileReadTool(workspace),
            FileListTool(workspace),
            FileWriteTool(workspace),
            MakeDirTool(workspace),
            FileMoveTool(workspace),
            FileCopyTool(workspace),
            FileDeleteTool(workspace),
            ShellTool(workspace),
            RunAgentTool(),
        ]
    )
    return registry
