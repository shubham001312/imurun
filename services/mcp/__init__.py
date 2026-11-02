"""MCP (Model Context Protocol) client: any MCP server's tools become model tools.

* ``client``   - JSON-RPC 2.0 over stdio and Streamable HTTP, standard library only;
* ``registry`` - servers from the ``mcp`` settings section, lazy connect,
  tool discovery into the ``ToolRegistry`` as ``mcp_<server>_<tool>``;
* ``routes``   - ``/api/mcp/*``;
* ``servers/gmail_mcp.py`` - a small stdio MCP server exposing the Gmail connector.

See docs/connectors/MCP.md.
"""
from .client import CallResult, HttpTransport, McpClient, McpError, McpTimeout, StdioTransport
from .registry import McpManager, ServerConfig

__all__ = [
    "CallResult",
    "HttpTransport",
    "McpClient",
    "McpError",
    "McpManager",
    "McpTimeout",
    "ServerConfig",
    "StdioTransport",
]
