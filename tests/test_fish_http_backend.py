"""The HTTP engine backend (``model/fish/src/server.cpp``), used when no engine
library is present on this machine.

Two regressions, both seen on a fresh install in the provisioning state:

* ``configured`` was ``bool(server_url)``, which is always true (the default
  is ``http://127.0.0.1:8731``). With no library and nothing listening,
  ``/api/status`` said ``runtime: ready`` and ``POST /api/turns`` was accepted
  (200) instead of answering ``503 model_unavailable``; the turn then failed.
* The aiohttp session was cached across event loops. Every synchronous call
  runs its own ``asyncio.run`` loop, so the second turn reused a session bound
  to a closed loop and failed with "Event loop is closed".
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("aiohttp")

from model.fish_client import SyncFishModelClient  # noqa: E402
from services.config import FishConfig  # noqa: E402


@pytest.fixture(autouse=True)
def _no_engine_library(monkeypatch, tmp_path):
    # A FISH_LIBRARY that does not exist means "no native engine here".
    monkeypatch.setenv("FISH_LIBRARY", str(tmp_path / "missing" / "libfish.so"))
    monkeypatch.delenv("FISH_MODEL", raising=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Engine(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    def do_GET(self):
        if self.path == "/fish/health":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path != "/fish/stream":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for token in ("Hel", "lo"):
            self.wfile.write(f"data: {json.dumps({'token': token})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


@pytest.fixture
def engine():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_unreachable_http_engine_is_not_configured():
    client = SyncFishModelClient(FishConfig(server_url=f"http://127.0.0.1:{_free_port()}"))
    assert client._native is None
    assert client.configured is False
    assert client.available is False


def test_reachable_http_engine_is_configured(engine):
    client = SyncFishModelClient(FishConfig(server_url=engine))
    assert client.configured is True
    assert client.available is True


def test_http_generate_works_on_consecutive_event_loops(engine):
    client = SyncFishModelClient(FishConfig(server_url=engine))
    messages = [{"role": "user", "content": "hi"}]
    try:
        # Each call is its own asyncio.run loop, as in the turn runner.
        assert client.generate(messages) == "Hello"
        assert client.generate(messages) == "Hello"
        assert client.generate(messages) == "Hello"
    finally:
        client.close()


def test_server_refuses_turns_when_no_engine_is_reachable(tmp_path):
    from services.server.app import build_server
    from services.config import load_config

    cfg = load_config()
    cfg.fish.server_url = f"http://127.0.0.1:{_free_port()}"
    cfg.fish.model_path = ""
    server = build_server(root=tmp_path, port=0, config=cfg, connectors={})
    server.start()
    try:
        import http.client

        def call(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
            headers = {"Authorization": f"Bearer {server.token}"}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")

        status, view = call("GET", "/api/status")
        assert status == 200
        assert view["runtime"] == "provisioning"
        assert view["engine"]["available"] is False
        status, session = call("POST", "/api/sessions", {"first_command": "hello"})
        assert status == 200
        status, reply = call("POST", "/api/turns", {"session_id": session["id"], "text": "hi"})
        assert status == 503
        assert reply.get("error") == "model_unavailable"
    finally:
        server.stop()
