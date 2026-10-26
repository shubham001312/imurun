"""Loopback API for the frontends: token-gated, body-capped, localhost only.

The browser and the TUI never see a model choice (V10) and never reach the
network surface directly: every call carries the per-run bearer token, and
the server refuses to bind anything but a loopback address.
"""
from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ..connectors.base import ConnectorError
from ..connectors.gate import ConnectorGate
from ..connectors.registry import ConnectorRegistry
from ..persona.store import Persona, PersonaStore
from ..persona.tool import PersonaTool
from ..prompts.queue import PromptQueue
from ..safety.decisions import PermissionRequired
from ..sessions.session import Session

MAX_BODY = 1_000_000
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class ApiServer:
    def __init__(
        self,
        *,
        persona_store: PersonaStore,
        persona_tool: PersonaTool | None = None,
        connectors: ConnectorRegistry | None = None,
        queue: PromptQueue | None = None,
        sessions_root: Path | str | None = None,
        gate: ConnectorGate | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        max_body: int = MAX_BODY,
        mark=None,
        token: str | None = None,
    ) -> None:
        if host not in LOOPBACK:
            raise ValueError("the API may only bind to a loopback address")
        if max_body <= 0:
            raise ValueError("max_body must be positive")
        self._persona_store = persona_store
        self._persona = persona_tool or PersonaTool(persona_store)
        self._connectors = connectors or ConnectorRegistry()
        self._queue = queue or PromptQueue()
        self._sessions_root = sessions_root
        self._gate = gate or ConnectorGate()
        self._max_body = max_body
        self._mark = mark
        self._token = token or secrets.token_urlsafe(24)
        self._httpd = ThreadingHTTPServer((host, port), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def token(self) -> str:
        return self._token

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start(self) -> "ApiServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _record(self, subject: str, why: str) -> None:
        if self._mark is None:
            return
        try:
            self._mark("G3-api", subject, why)
        except Exception:  # noqa: BLE001 - a broken sink must not break the API
            pass

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:  # noqa: D401 - quiet by design
                return

            def _reply(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                expected = f"Bearer {server.token}"
                if not secrets.compare_digest(header, expected):
                    server._record(self.path, "denied: bad token")
                    self._reply(401, {"error": "unauthorized"})
                    return False
                return True

            def _body(self) -> dict | None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._reply(400, {"error": "invalid content length"})
                    return None
                if length > server._max_body:
                    server._record(self.path, "denied: body too large")
                    self._reply(413, {"error": "body too large"})
                    return None
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._reply(400, {"error": "invalid json"})
                    return None
                if not isinstance(payload, dict):
                    self._reply(400, {"error": "a json object is required"})
                    return None
                return payload

            def do_GET(self) -> None:  # noqa: N802 - http.server contract
                if not self._authorized():
                    return
                path = urlsplit(self.path).path
                if path == "/health":
                    self._reply(200, {"ok": True})
                    return
                if path == "/api/persona":
                    view = server._persona.read()
                    self._reply(
                        200,
                        {
                            "configured": view.configured,
                            "name": view.name,
                            "email_role": view.email_role,
                            "description": view.description,
                            "error": view.error,
                        },
                    )
                    return
                if path == "/api/connectors":
                    self._reply(200, {"connectors": server._connectors.list()})
                    return
                if path == "/api/sessions":
                    identities = Session.list_sessions(
                        server._sessions_root,
                        mark=lambda rule, subject, why: server._record(subject, f"{rule}: {why}"),
                    )
                    self._reply(200, {"sessions": [asdict(identity) for identity in identities]})
                    return
                self._reply(404, {"error": "not found"})

            def do_PUT(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                path = urlsplit(self.path).path
                if path != "/api/persona":
                    self._reply(404, {"error": "not found"})
                    return
                payload = self._body()
                if payload is None:
                    return
                try:
                    server._persona_store.save(
                        Persona(
                            name=str(payload.get("name", "")),
                            email_role=str(payload.get("email_role", "")),
                            description=str(payload.get("description", "")),
                        )
                    )
                except (ValueError, TypeError) as exc:
                    self._reply(400, {"error": str(exc)})
                    return
                server._record("persona", "saved")
                self._reply(200, {"ok": True})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                path = urlsplit(self.path).path
                parts = [part for part in path.split("/") if part]
                if path == "/api/prompts":
                    payload = self._body()
                    if payload is None:
                        return
                    try:
                        prompt = server._queue.push(
                            str(payload.get("session_id", "")),
                            str(payload.get("text", "")),
                        )
                    except ValueError as exc:
                        self._reply(400, {"error": str(exc)})
                        return
                    self._reply(200, {"id": prompt.id})
                    return

                if len(parts) == 5 and parts[:2] == ["api", "connectors"] and parts[3] == "actions":
                    connector = server._connectors.get(parts[2])
                    if connector is None:
                        self._reply(404, {"error": "unknown connector"})
                        return
                    action = parts[4]
                    payload = self._body()
                    if payload is None:
                        return
                    params = payload.get("params", {})
                    approved = bool(payload.get("approved", False))
                    if not isinstance(params, dict):
                        self._reply(400, {"error": "params must be an object"})
                        return
                    decision = server._gate.check(connector, action, approved=approved)
                    if decision.kind != "allow":
                        self._reply(
                            403,
                            {
                                "decision": decision.kind,
                                "reasons": list(decision.reasons),
                            },
                        )
                        return
                    try:
                        result = server._gate.execute(
                            connector, action, params, approved=approved
                        )
                    except PermissionRequired as exc:
                        self._reply(403, {"decision": "denied", "reasons": [str(exc)]})
                        return
                    except ConnectorError as exc:
                        self._reply(400, {"error": str(exc)})
                        return
                    self._reply(200, {"result": result})
                    return

                self._reply(404, {"error": "not found"})

        return Handler
