"""MCP client: stdio against a fake server script, HTTP against an in-process server."""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from services.mcp.client import (
    HttpTransport,
    McpClient,
    McpError,
    McpRemoteError,
    McpTimeout,
    StdioTransport,
    check_url,
    child_environment,
    content_to_text,
)

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def stdio_client(mode="normal", timeout=10.0, connect_timeout=10.0):
    return McpClient(StdioTransport(sys.executable, [FAKE, mode]), timeout=timeout,
                     connect_timeout=connect_timeout)


def test_stdio_initialize_list_call_and_close():
    client = stdio_client()
    try:
        info = client.connect()
        assert info["protocolVersion"] == "2025-06-18"
        assert info["serverInfo"]["name"] == "fake"
        assert client.connected
        tools = client.list_tools()        # two pages via nextCursor
        assert [t["name"] for t in tools] == ["echo", "add", "slow", "boom", "Wipe-Disk"]
        result = client.call_tool("echo", {"text": "hi"})
        assert result.ok and "echo: hi" in result.text
        assert "[image: image/png" in result.text and "[resource file:///x.txt]\ninside" in result.text
        result = client.call_tool("add", {"a": 2, "b": 3})
        assert result.ok and result.text == "5.0" and result.data == {"sum": 5.0}
        failed = client.call_tool("boom", {})
        assert not failed.ok and failed.is_error and "it broke" in failed.text
        with pytest.raises(McpRemoteError) as err:
            client.call_tool("nope", {})
        assert err.value.code == -32602
        assert client.ping()
    finally:
        client.close()
    assert not client.connected


def test_stdio_timeout_is_an_error_not_a_hang():
    client = stdio_client(timeout=1.0)
    try:
        client.connect()
        started = time.monotonic()
        with pytest.raises(McpTimeout):
            client.call_tool("slow", {}, timeout=0.5)
        assert time.monotonic() - started < 3
        # The connection still works after a timed-out call (the fake server is
        # single-threaded, so this waits for its sleep to end).
        assert client.call_tool("add", {"a": 1, "b": 1}, timeout=10).ok
    finally:
        client.close()


def test_stdio_old_protocol_accepted_future_refused():
    client = stdio_client("old")
    try:
        assert client.connect()["protocolVersion"] == "2024-11-05"
    finally:
        client.close()
    client = stdio_client("future")
    with pytest.raises(McpError, match="2099-01-01"):
        client.connect()
    assert not client.connected


def test_stdio_crash_and_silence_and_missing_command():
    client = stdio_client("crash")
    try:
        client.connect()
        with pytest.raises(McpError) as err:
            client.list_tools()
        assert "exited" in str(err.value) or "not running" in str(err.value)
        assert not client.connected
    finally:
        client.close()
    silent = stdio_client("silent", connect_timeout=1.0)
    started = time.monotonic()
    with pytest.raises(McpTimeout):
        silent.connect()
    assert time.monotonic() - started < 5
    with pytest.raises(McpError, match="could not start"):
        McpClient(StdioTransport("definitely-not-a-command-xyz")).connect()


def test_child_environment_drops_credentials(monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("HARMLESS", "1")
    env = child_environment({"SERVER_TOKEN": "configured"})
    assert "MY_API_KEY" not in env and "GITHUB_TOKEN" not in env
    assert env["HARMLESS"] == "1" and env["SERVER_TOKEN"] == "configured"


def test_content_to_text_variants():
    text = content_to_text({"content": [{"type": "resource_link", "uri": "file:///a", "name": "a"},
                                        {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"}]})
    assert "[link: a file:///a]" in text and "[audio: audio/wav" in text
    assert content_to_text({"content": [], "structuredContent": {"x": 1}}) == '{"x": 1}'
    long = content_to_text({"content": [{"type": "text", "text": "x" * 20000}]}, limit=100)
    assert long.startswith("x" * 100) and "result cut" in long


def test_url_rules():
    assert check_url("https://example.com/mcp")
    assert check_url("http://127.0.0.1:9/mcp")
    with pytest.raises(McpError):
        check_url("http://example.com/mcp")
    with pytest.raises(McpError):
        check_url("ftp://example.com")


# ------------------------------------------------------------------ http
class _Http(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *args):
        return

    def do_DELETE(self):
        self.calls.append(("DELETE", self.headers.get("Mcp-Session-Id")))
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.calls.append((body.get("method"), self.headers.get("Mcp-Session-Id"),
                           self.headers.get("MCP-Protocol-Version"), self.headers.get("Authorization")))
        if "id" not in body:
            self.send_response(202)
            self.end_headers()
            return
        method = body["method"]
        if method == "initialize":
            self._json({"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                "serverInfo": {"name": "http-fake"}}}, session="sess-1")
        elif method == "tools/list":
            self._json({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [
                {"name": "hello", "inputSchema": {"type": "object"}}]}})
        elif method == "tools/call":
            # Streamed answer: a notification first, then the response.
            frames = [
                {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}},
                {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "hi from sse"}]}},
            ]
            data = "".join(f"event: message\ndata: {json.dumps(f)}\n\n" for f in frames).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "no"}})

    def _json(self, payload, session=None):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def http_server():
    _Http.calls = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Http)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    httpd.shutdown()
    httpd.server_close()


def test_http_transport_json_sse_and_session(http_server):
    seen = []
    transport = HttpTransport(http_server, {"Authorization": "Bearer abc"},
                              on_notification=lambda m, p: seen.append(m))
    client = McpClient(transport, timeout=5)
    info = client.connect()
    assert info["protocolVersion"] == "2025-03-26"
    assert transport.session_id == "sess-1"
    assert [t["name"] for t in client.list_tools()] == ["hello"]
    result = client.call_tool("hello", {})
    assert result.ok and result.text == "hi from sse"
    assert seen == ["notifications/progress"]
    client.close()
    methods = [c[0] for c in _Http.calls]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call", "DELETE"]
    # Session id and negotiated version are sent after initialize; the user header always.
    assert _Http.calls[2][1] == "sess-1" and _Http.calls[2][2] == "2025-03-26"
    assert all(c[3] == "Bearer abc" for c in _Http.calls if c[0] != "DELETE")


def test_http_unreachable_is_an_error():
    client = McpClient(HttpTransport("http://127.0.0.1:9/mcp"), connect_timeout=2)
    with pytest.raises(McpError):
        client.connect()
