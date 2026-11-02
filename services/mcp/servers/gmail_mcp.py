"""The Gmail-only MCP server: ``connectors_mcp.py`` with ``--services gmail``.

Kept as its own entry point because the default ``mcp.servers`` entry
``gmail`` points at it::

    python services/mcp/servers/gmail_mcp.py --home ~/.imurun

Search/read/labels are ``readOnlyHint``; send, trash and modify are
``destructiveHint`` so IMURUN's bridge always shows an approval card.
See ``connectors_mcp.py`` for the protocol handling.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.mcp.servers.connectors_mcp import ConnectorsMcpServer, build_tools, main as _main  # noqa: E402

SERVER_NAME = "imurun-gmail"


def build_gmail_tools(home: Path, **kwargs) -> dict:
    return build_tools(home, ["gmail"], **kwargs)


def GmailMcpServer(tools: dict, out=None) -> ConnectorsMcpServer:  # noqa: N802 - kept for old imports
    return ConnectorsMcpServer(tools, name=SERVER_NAME, out=out)


def main(argv: list[str] | None = None) -> int:
    return _main(argv, default_services="gmail", name=SERVER_NAME)


if __name__ == "__main__":
    raise SystemExit(main())
