"""Malformed requests get a clean 4xx (or a JSON 500), never a dropped
connection, a traceback in the server log or a request that hangs.

Each case was reproduced against a running ``python start.py``:

* ``Content-Length: -5`` -> ``ValueError`` in ``rfile.read``; connection
  dropped with no response.
* ``{"maxSteps": 1e309}`` -> ``json`` parses it as ``inf``, ``int(inf)``
  raises ``OverflowError`` in ``POST /api/agent``; connection dropped.
* a body shorter than its ``Content-Length`` -> the handler thread blocked in
  ``rfile.read`` forever.
* any other unexpected exception in a route -> connection dropped.
"""
from __future__ import annotations

import json
import socket
import time

import pytest

from services.persona.store import PersonaStore
from services.server.api import ApiServer
from services.skills.store import SkillsStore


class _Model:
    available = True

    def generate(self, messages):
        return "ok"


@pytest.fixture()
def server(tmp_path):
    srv = ApiServer(
        persona_store=PersonaStore(tmp_path / "persona.json"),
        sessions_root=tmp_path / "sessions", data_root=tmp_path, model=_Model(),
        skills=SkillsStore(tmp_path / "skills"), static_root=None,
    )
    srv.start()
    yield srv
    srv.stop()


def _raw(server, request: bytes, wait: float = 10.0) -> tuple[int, dict]:
    with socket.create_connection(("127.0.0.1", server.port), timeout=wait) as sock:
        sock.sendall(request)
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    assert data, "the server closed the connection without a response"
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, (json.loads(body) if body else {})


def _post(server, path: str, body: bytes, length: int | None = None) -> bytes:
    n = len(body) if length is None else length
    return (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {server.token}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {n}\r\nConnection: close\r\n\r\n").encode() + body


def test_negative_content_length_is_400(server):
    status, body = _raw(server, _post(server, "/api/sessions", b"{}", length=-5))
    assert status == 400
    assert "content length" in body["error"]


@pytest.mark.parametrize("number", [b"1e309", b"-1e309", b"NaN", b"Infinity", b"-Infinity"])
def test_non_finite_numbers_are_invalid_json(server, number):
    status, body = _raw(server, _post(server, "/api/agent", b'{"maxSteps": ' + number + b"}"))
    assert status == 400
    assert body == {"error": "invalid json"}


def test_body_shorter_than_content_length_times_out_cleanly(server):
    server._request_timeout = 0.5
    start = time.monotonic()
    status, body = _raw(server, _post(server, "/api/sessions", b"{}", length=100), wait=10.0)
    assert time.monotonic() - start < 5.0
    assert status == 408
    assert body["error"] == "request timed out"


def test_unexpected_route_error_is_a_json_500(server):
    class Broken:
        def view(self):
            raise RuntimeError("boom")

    server._usage = Broken()
    status, body = _raw(server, (f"GET /api/usage HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                 f"Authorization: Bearer {server.token}\r\nConnection: close\r\n\r\n").encode())
    assert status == 500
    assert body == {"error": "internal error"}
    # The server keeps serving afterwards.
    status, _ = _raw(server, b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
    assert status == 200


def test_null_text_fields_count_as_missing(server):
    # str(None) used to name the session (and skills, clients...) "None".
    status, body = _raw(server, _post(server, "/api/sessions", b'{"first_command": null}'))
    assert status == 400
    assert "first command" in body["error"]
    status, body = _raw(server, _post(server, "/api/skills", b'{"name": null, "body": null}'))
    assert status == 400
    status, session = _raw(server, _post(server, "/api/sessions", b'{"first_command": "hello"}'))
    assert status == 200
    path = f"/api/sessions/{session['id']}/rename"
    status, body = _raw(server, _post(server, path, b'{"name": null}'))
    assert status == 400


def test_download_stream_finalises_the_download_generator(server, monkeypatch):
    # The route returned from inside ``async for`` and closed the loop without
    # shutdown_asyncgens(): the generator's cleanup (its open .part file and
    # HTTP response) never ran ("Task was destroyed but it is pending!").
    from services.server import api as api_module

    closed = []

    class Manager:
        async def download_model(self, model_id, token=None):
            try:
                yield {"model_id": model_id, "status": "downloading", "percent": 50}
                yield {"model_id": model_id, "status": "complete", "percent": 100}
                yield {"model_id": model_id, "status": "never reached"}
            finally:
                closed.append(model_id)

    monkeypatch.setattr(api_module, "_HAS_MODELS_API", True)
    monkeypatch.setattr(api_module, "get_model_manager", lambda *a, **k: Manager())
    request = _post(server, "/api/models/download", b'{"model_id": "m1"}')
    with socket.create_connection(("127.0.0.1", server.port), timeout=10) as sock:
        sock.sendall(request)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    assert b'"status": "complete"' in data
    assert closed == ["m1"]
