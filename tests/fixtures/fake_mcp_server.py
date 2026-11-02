"""A tiny stdio MCP server for tests. Behaviour is chosen by argv[1]:

normal   - two pages of tools (echo, add | slow, boom, wipe), answers calls
old      - answers initialize with protocol 2024-11-05
future   - answers initialize with an unsupported protocol version
crash    - exits right after initialize
silent   - never answers initialize
"""
from __future__ import annotations

import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"

PAGE1 = [
    {"name": "echo", "description": "Echo the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "add", "description": "Add two numbers.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
]
PAGE2 = [
    {"name": "slow", "description": "Sleeps.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "boom", "description": "Always fails.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "Wipe-Disk", "description": "Destructive.", "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"destructiveHint": True}},
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main():
    print("fake server starting", file=sys.stderr, flush=True)
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        message = json.loads(raw)
        method, mid, params = message.get("method"), message.get("id"), message.get("params") or {}
        if mid is None:
            continue
        if method == "initialize":
            if MODE == "silent":
                continue
            version = {"old": "2024-11-05", "future": "2099-01-01"}.get(MODE, params.get("protocolVersion"))
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": version, "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "fake", "version": "1"}}})
            if MODE == "crash":
                print("crashing on purpose", file=sys.stderr, flush=True)
                sys.exit(3)
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            if params.get("cursor") == "page2":
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": PAGE2}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": PAGE1, "nextCursor": "page2"}})
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name == "echo":
                # A log line on stdout must not confuse the client.
                sys.stdout.write("not json\n")
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                    {"type": "text", "text": f"echo: {args.get('text')}"},
                    {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
                    {"type": "resource", "resource": {"uri": "file:///x.txt", "text": "inside"}}]}})
            elif name == "add":
                total = float(args.get("a", 0)) + float(args.get("b", 0))
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": str(total)}], "structuredContent": {"sum": total}}})
            elif name == "slow":
                time.sleep(5)
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "late"}]}})
            elif name == "boom":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "it broke"}], "isError": True}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})


if __name__ == "__main__":
    main()
