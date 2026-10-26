"""Loopback API for the frontends: token-gated, body-capped, localhost only.

The browser and the TUI never see a model choice (V10) and never reach the
network surface directly: every call carries the per-run bearer token, and
the server refuses to bind anything but a loopback address.

Routes
------
GET  /                     static shell, per-run token injected
GET  /health               liveness
GET  /api/status           server, queue, sessions, connectors, model state
GET  /api/events           SSE stream (Bearer auth; the client uses fetch)
GET  /api/persona          persona view
GET  /api/connectors       provider cards
GET  /api/sessions         session identities
POST /api/sessions         start a session from the first command
GET  /api/sessions/{id}/messages
POST /api/sessions/{id}/rename
GET/POST /api/skills
POST /api/turns            start an agent turn
GET  /api/turns/{id}       turn state
POST /api/turns/{id}/cancel
POST /api/connectors/{id}/connect
GET  /oauth/callback       one-time state, then the token goes to the keychain
"""
from __future__ import annotations

import html
import json
import secrets
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..connectors.base import ConnectorError
from ..connectors.gate import ConnectorGate
from ..connectors.oauth import OAuthError, OAuthFlow
from ..connectors.registry import ConnectorRegistry
from ..persona.store import Persona, PersonaStore
from ..persona.tool import PersonaTool
from ..prompts.queue import PromptQueue
from ..safety.decisions import PermissionRequired
from ..sessions.session import Session
from ..sessions.transcript import TranscriptStore
from ..skills.store import SkillsStore
from .events import CLOSE, EventBroker
from .turns import TurnRunner

MAX_BODY = 1_000_000
MAX_READ = 1_000
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
}


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
        transcripts: TranscriptStore | None = None,
        events: EventBroker | None = None,
        skills: SkillsStore | None = None,
        turns: TurnRunner | None = None,
        model=None,
        static_root: Path | str | None = None,
        oauth_flows: dict[str, OAuthFlow] | None = None,
        persona=None,
        kill=None,
        version: str = "0.2",
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
        self._static_root = Path(static_root) if static_root is not None else None
        self._version = version
        self._started_at = time.time()
        self._skills = skills or SkillsStore(mark=self._skill_mark)
        self._transcripts = transcripts or TranscriptStore(
            sessions_root, mark=self._transcript_mark
        )
        self._events = events or EventBroker(mark=self._event_mark)
        self._turns = turns or TurnRunner(
            model,
            queue=self._queue,
            transcripts=self._transcripts,
            events=self._events,
            kill=kill,
            persona=persona,
            mark=mark,
        )
        self._oauth_flows = dict(oauth_flows or {})
        self._oauth_states: dict[str, tuple[str, OAuthFlow]] = {}
        self._oauth_lock = threading.Lock()
        self._httpd = ThreadingHTTPServer((host, port), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def token(self) -> str:
        return self._token

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def add_oauth_flow(self, connector_id: str, flow: OAuthFlow) -> None:
        if not connector_id:
            raise ValueError("connector_id is required")
        with self._oauth_lock:
            self._oauth_flows[connector_id] = flow

    def start(self) -> "ApiServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._events.close()
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

    def _transcript_mark(self, rule: str, subject: str, why: str) -> None:
        self._record(subject, f"{rule}: {why}")

    def _skill_mark(self, rule: str, subject: str, why: str) -> None:
        self._record(subject, f"{rule}: {why}")

    def _event_mark(self, rule: str, subject: str, why: str) -> None:
        self._record(subject, f"{rule}: {why}")

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
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _html(self, code: int, title: str, message: str) -> None:
                body = (
                    "<!doctype html><meta charset='utf-8'>"
                    f"<title>{html.escape(title)}</title>"
                    f"<h1>{html.escape(title)}</h1>"
                    f"<p>{html.escape(message)}</p>"
                ).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(body)

            def _local(self) -> bool:
                host = self.headers.get("Host", "")
                if host.startswith("["):
                    name = host.split("]")[0][1:]
                elif ":" in host:
                    name = host.rsplit(":", 1)[0]
                else:
                    name = host
                if name in LOOPBACK:
                    return True
                server._record(self.path, f"denied: host {name!r}")
                self._reply(403, {"error": "forbidden host"})
                return False

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                expected = f"Bearer {server.token}"
                if not secrets.compare_digest(header, expected):
                    server._record(self.path, "denied: bad token")
                    self._reply(401, {"error": "unauthorized"})
                    return False
                return True

            def _drain(self, length: int) -> None:
                """Consume a refused body so the client can read the reply.

                Windows resets the connection when unread request bytes are
                still in flight, which hides the 413 from the client.
                """
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 8192))
                    if not chunk:
                        break
                    remaining -= len(chunk)

            def _body(self) -> dict | None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._reply(400, {"error": "invalid content length"})
                    return None
                if length > server._max_body:
                    server._record(self.path, "denied: body too large")
                    if length <= server._max_body + 65536:
                        self._drain(length)
                    else:
                        self.close_connection = True
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

            def _static(self, path: str) -> bool:
                root = server._static_root
                if root is None:
                    return False
                relative = "index.html" if path in ("", "/") else path.lstrip("/")
                target = (root / relative).resolve()
                try:
                    target.relative_to(root.resolve())
                except ValueError:
                    self._reply(404, {"error": "not found"})
                    return True
                if not target.is_file() or target.suffix.lower() not in STATIC_TYPES:
                    self._reply(404, {"error": "not found"})
                    return True
                try:
                    data = target.read_bytes()
                except OSError:
                    self._reply(404, {"error": "not found"})
                    return True
                content_type = STATIC_TYPES[target.suffix.lower()]
                if target.name == "index.html":
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        self._reply(500, {"error": "shell is not utf-8"})
                        return True
                    data = text.replace("__IMURUN_TOKEN__", server.token).encode("utf-8")
                    content_type = "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(data)
                return True

            def _stream(self) -> None:
                sub = server._events.subscribe()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    self.wfile.write(b"retry: 3000\n\n")
                    self.wfile.flush()
                    while True:
                        event = sub.get(timeout=15.0)
                        if event is None:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        if event.type == CLOSE:
                            break
                        payload = json.dumps(event.data, ensure_ascii=False)
                        frame = f"event: {event.type}\ndata: {payload}\n\n"
                        self.wfile.write(frame.encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    server._events.unsubscribe(sub)
                    self.close_connection = True

            def _callback(self) -> None:
                query = parse_qs(urlsplit(self.path).query)
                state = (query.get("state") or [""])[0]
                code = (query.get("code") or [""])[0]
                error = (query.get("error") or [""])[0]
                with server._oauth_lock:
                    entry = server._oauth_states.pop(state, None)
                if entry is None:
                    self._html(400, "Connection failed", "This link is unknown or already used.")
                    return
                connector_id, flow = entry
                if error or not code:
                    self._html(400, "Connection cancelled", "The provider returned no code.")
                    return
                try:
                    flow.complete(state, code)
                except OAuthError as exc:
                    self._html(400, "Connection failed", str(exc))
                    return
                self._html(
                    200,
                    "Connected",
                    f"{connector_id} is connected. You can close this window.",
                )

            def do_GET(self) -> None:  # noqa: N802 - http.server contract
                if not self._local():
                    return
                path = unquote(urlsplit(self.path).path)
                if path == "/oauth/callback":
                    self._callback()
                    return
                if not path.startswith("/api/") and path != "/health":
                    if self._static(path):
                        return
                if not self._authorized():
                    return
                if path == "/health":
                    self._reply(200, {"ok": True})
                    return
                if path == "/api/status":
                    connectors = server._connectors.list()
                    identities = (
                        Session.list_sessions(
                            server._sessions_root,
                            mark=lambda rule, subject, why: server._record(
                                subject, f"{rule}: {why}"
                            ),
                        )
                        if server._sessions_root is not None
                        else []
                    )
                    self._reply(
                        200,
                        {
                            "ok": True,
                            "version": server._version,
                            "uptime": round(time.time() - server._started_at, 3),
                            "sessions": len(identities),
                            "queue": {"pending": server._queue.pending_count()},
                            "connectors": {
                                "total": len(connectors),
                                "connected": sum(
                                    1 for item in connectors if item.get("connected")
                                ),
                            },
                            "model": server._turns.available,
                            "runtime": "ready" if server._turns.available else "provisioning",
                        },
                    )
                    return
                if path == "/api/events":
                    self._stream()
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
                if path == "/api/skills":
                    self._reply(
                        200,
                        {
                            "skills": [
                                {
                                    "id": skill.id,
                                    "name": skill.name,
                                    "description": skill.description,
                                    "origin": skill.origin,
                                    "body": skill.body,
                                }
                                for skill in server._skills.list()
                            ]
                        },
                    )
                    return
                if path == "/api/sessions":
                    identities = Session.list_sessions(
                        server._sessions_root,
                        mark=lambda rule, subject, why: server._record(
                            subject, f"{rule}: {why}"
                        ),
                    )
                    self._reply(200, {"sessions": [asdict(identity) for identity in identities]})
                    return
                parts = [part for part in path.split("/") if part]
                if (
                    len(parts) == 4
                    and parts[:2] == ["api", "sessions"]
                    and parts[3] == "messages"
                ):
                    self._messages(parts[2])
                    return
                if len(parts) == 3 and parts[:2] == ["api", "turns"]:
                    turn = server._turns.get(parts[2])
                    if turn is None:
                        self._reply(404, {"error": "unknown turn"})
                    else:
                        self._reply(200, turn.view())
                    return
                self._reply(404, {"error": "not found"})

            def _messages(self, session_id: str) -> None:
                if server._sessions_root is not None:
                    try:
                        Session.load(session_id, server._sessions_root)
                    except (OSError, ValueError, KeyError, TypeError):
                        self._reply(404, {"error": "unknown session"})
                        return
                query = parse_qs(urlsplit(self.path).query)
                try:
                    limit = int((query.get("limit") or ["200"])[0])
                except ValueError:
                    limit = 200
                limit = max(1, min(limit, MAX_READ))
                try:
                    messages = server._transcripts.read(session_id, limit)
                except ValueError as exc:
                    self._reply(400, {"error": str(exc)})
                    return
                self._reply(200, {"messages": [asdict(message) for message in messages]})

            def do_PUT(self) -> None:  # noqa: N802
                if not self._local():
                    return
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
                if not self._local():
                    return
                if not self._authorized():
                    return
                path = unquote(urlsplit(self.path).path)
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
                    server._events.publish(
                        "queue",
                        {
                            "session_id": prompt.session_id,
                            "pending": server._queue.pending_count(),
                        },
                    )
                    self._reply(200, {"id": prompt.id})
                    return

                if path == "/api/sessions":
                    payload = self._body()
                    if payload is None:
                        return
                    try:
                        session = Session.start(
                            str(payload.get("first_command", "")),
                            server._sessions_root,
                            mark=lambda rule, subject, why: server._record(
                                subject, f"{rule}: {why}"
                            ),
                        )
                    except ValueError as exc:
                        self._reply(400, {"error": str(exc)})
                        return
                    server._record("session", f"started {session.id}")
                    self._reply(200, asdict(session.identity))
                    return

                if path == "/api/turns":
                    payload = self._body()
                    if payload is None:
                        return
                    session_id = str(payload.get("session_id", ""))
                    prompt = str(payload.get("text") or payload.get("prompt") or "")
                    if not session_id:
                        self._reply(400, {"error": "session_id is required"})
                        return
                    if server._sessions_root is not None:
                        try:
                            Session.load(session_id, server._sessions_root)
                        except (OSError, ValueError, KeyError, TypeError):
                            self._reply(404, {"error": "unknown session"})
                            return
                    try:
                        turn = server._turns.start(session_id, prompt)
                    except RuntimeError:
                        self._reply(503, {"error": "model unavailable"})
                        return
                    except ValueError as exc:
                        self._reply(400, {"error": str(exc)})
                        return
                    self._reply(200, {"turn_id": turn.id})
                    return

                if path == "/api/skills":
                    payload = self._body()
                    if payload is None:
                        return
                    try:
                        skill = server._skills.create(
                            str(payload.get("name", "")),
                            str(payload.get("description", "")),
                            str(payload.get("body", "")),
                        )
                    except FileExistsError as exc:
                        self._reply(409, {"error": str(exc)})
                        return
                    except (ValueError, TypeError) as exc:
                        self._reply(400, {"error": str(exc)})
                        return
                    server._record("skills", f"created {skill.id}")
                    self._reply(
                        200,
                        {
                            "id": skill.id,
                            "name": skill.name,
                            "description": skill.description,
                            "origin": skill.origin,
                        },
                    )
                    return

                if (
                    len(parts) == 4
                    and parts[:2] == ["api", "sessions"]
                    and parts[3] == "rename"
                ):
                    payload = self._body()
                    if payload is None:
                        return
                    try:
                        session = Session.load(parts[2], server._sessions_root)
                    except (OSError, ValueError, KeyError, TypeError):
                        self._reply(404, {"error": "unknown session"})
                        return
                    try:
                        identity = session.rename(str(payload.get("name", "")))
                    except ValueError as exc:
                        self._reply(400, {"error": str(exc)})
                        return
                    server._record("session", f"renamed {identity.id}")
                    self._reply(200, asdict(identity))
                    return

                if (
                    len(parts) == 4
                    and parts[:2] == ["api", "turns"]
                    and parts[3] == "cancel"
                ):
                    turn_id = parts[2]
                    if server._turns.get(turn_id) is None:
                        self._reply(404, {"error": "unknown turn"})
                        return
                    self._reply(200, {"cancelled": server._turns.cancel(turn_id)})
                    return

                if (
                    len(parts) == 4
                    and parts[:2] == ["api", "connectors"]
                    and parts[3] == "connect"
                ):
                    connector_id = parts[2]
                    connector = server._connectors.get(connector_id)
                    if connector is None:
                        self._reply(404, {"error": "unknown connector"})
                        return
                    flow = server._oauth_flows.get(connector_id)
                    if flow is None:
                        self._reply(404, {"error": "no oauth flow for this connector"})
                        return
                    if connector.connected():
                        self._reply(409, {"error": "already connected"})
                        return
                    begun = flow.begin()
                    with server._oauth_lock:
                        server._oauth_states[begun["state"]] = (connector_id, flow)
                    self._reply(200, begun)
                    return

                if (
                    len(parts) == 5
                    and parts[:2] == ["api", "connectors"]
                    and parts[3] == "actions"
                ):
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
