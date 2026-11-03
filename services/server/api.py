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
GET  /api/tools            tools, schemas, availability, permission
POST /api/tools/run        run one tool by hand (403 ask -> approved: true)
GET/POST /api/permissions  permission mode, workspace root, toggles
GET  /api/tools/approvals, POST /api/tools/approvals/{id}
GET/POST /api/agent        step budget
(the agentic handlers live in services/tools/routes.py)
"""
from __future__ import annotations

import html
import json
import secrets
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _LoopbackHTTPServer(ThreadingHTTPServer):
    """``HTTPServer.server_bind`` calls ``socket.getfqdn()`` for the Server
    header; on hosts with slow reverse DNS (the macOS CI runner) that lookup
    took longer than 18 s and the app looked hung before its first request.
    We serve loopback only, so the name is the bind address."""

    def server_bind(self) -> None:
        import socketserver

        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..config import load_config
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
from ..tools import routes as tool_routes
from ..voice import routes as voice_routes  # /api/voice/* (docs/voice/README.md)
from ..tuning import routes as tuning_routes  # /api/tuning/* (docs/tuning/README.md)
from ..tools.approvals import ApprovalBroker
from ..tools.executor import ToolExecutor
from ..tools.permissions import PermissionPolicy
from .clients import ClientStore
from .events import CLOSE, EventBroker
from .resources import ResourceSampler, handle_update as resources_update
from ..resources import ResourceGovernor, set_governor, validate_settings as validate_resources
from .settings import SettingsStore, apply_search_env, mask_secrets
from .stack import StackController
from .turns import TurnRunner
from .usage import UsageTracker
try:  # run as ``imurun.services.server.api``
    from ..model.fish_client import SyncFishModelClient
except ImportError:  # run as ``services.server.api`` from the imurun directory
    from model.fish_client import SyncFishModelClient


def _selected_model_name(server) -> str:
    """Name of the model the engine will load, or an empty string."""
    model = getattr(server, "_model", None)
    getter = getattr(model, "selected_name", None)
    if getter is None:
        return ""
    try:
        return str(getter())
    except Exception:  # noqa: BLE001 - a broken picker must not break the list
        return ""


def _tools_summary(server) -> dict:
    """A one-line view of the live-fact tools for /api/status.

    The full record is on /api/tools; this is only what a status line needs:
    how many tools exist, how many can run now, and whether the machine can
    reach the network at all. A missing runner is reported as empty rather
    than as an error.
    """
    runner = getattr(server, "_tools", None)
    if runner is None:
        return {"total": 0, "available": 0, "network": False, "offline": True}
    try:
        status = runner.status()
    except Exception:  # noqa: BLE001 - a status line must never fail
        return {"total": 0, "available": 0, "network": False, "offline": True}
    tools = status.get("tools") or []
    network = bool(status.get("network"))
    return {
        "total": len(tools),
        "available": sum(1 for tool in tools if tool.get("available")),
        "network": network,
        "offline": not network,
    }


def _model_status(server) -> dict:
    """Which compute path is live, and whether it can answer."""
    model = getattr(server, "_model", None)
    reporter = getattr(model, "status", None)
    if reporter is None:
        return {"engine": "none", "available": bool(getattr(model, "available", False))}
    try:
        status = dict(reporter())
    except Exception:  # noqa: BLE001
        return {"engine": "unknown", "available": False}
    status.setdefault("available", bool(getattr(model, "available", False)))
    try:
        from .warmstart import view as warm_view

        status["warm_start"] = warm_view()
    except Exception:  # noqa: BLE001
        pass
    return status

# Optional FastAPI models API (requires huggingface_hub, fastapi)
try:
    from .models_api import DownloadRequest, get_model_manager
    _HAS_MODELS_API = True
except ImportError:
    DownloadRequest = None
    get_model_manager = None
    _HAS_MODELS_API = False

# Fallback model manager if FastAPI not available
if not _HAS_MODELS_API:
    try:
        from services.model_manager import get_model_manager as _get_mm
        get_model_manager = _get_mm
        _HAS_MODELS_API = True
    except ImportError:
        pass

MAX_BODY = 1_000_000
MAX_READ = 1_000
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; media-src 'self' blob:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
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
        tools=None,
        kill=None,
        version: str = "0.2",
        data_root: Path | str | None = None,
        settings: SettingsStore | None = None,
        clients: ClientStore | None = None,
        usage: UsageTracker | None = None,
        resources: ResourceSampler | None = None,
        stack: StackController | None = None,
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
        # Create default FISH model client if no model and no turns provided
        if model is None and turns is None:
            cfg = load_config()
            model = SyncFishModelClient(cfg.fish)
        self._model = model  # Keep reference for cleanup
        self._tools = tools
        home = (
            Path(data_root)
            if data_root is not None
            else (Path(sessions_root).parent if sessions_root is not None else Path.home() / ".imurun")
        )
        self._home = home
        self._usage = usage or UsageTracker(home / "usage.json")
        self._settings = settings or SettingsStore(home / "settings")
        # The agentic layer: one permission policy (mode + workspace from the
        # `permissions` settings section), one approval broker on the event
        # stream, and one executor every tool call goes through. The tools
        # are rebound to the policy's workspace so both agree on the root.
        self._policy = PermissionPolicy(self._settings)
        registry = getattr(tools, "registry", None)

        def _card_levels(tool_name: str, args: dict) -> tuple[str, bool]:
            # Level + "remember" applicability for approval cards requested
            # without them (manual route, tests); the executor passes its own.
            reg = getattr(self._executor, "registry", None) or registry
            tool = reg.get(tool_name) if reg is not None else None
            if tool is None:
                return "", True
            return self._policy.level(tool, args), self._policy.rememberable(tool, args)

        self._approvals = ApprovalBroker(self._events.publish, levels=_card_levels)
        self._executor = None
        if registry is not None:
            registry.bind_workspace(self._policy.workspace)
            if hasattr(tools, "policy"):
                tools.policy = self._policy
            self._executor = ToolExecutor(
                registry, policy=self._policy, approvals=self._approvals,
                publish=self._events.publish, mark=self._tool_mark,
            )
        self._turns = turns or TurnRunner(
            model,
            queue=self._queue,
            transcripts=self._transcripts,
            events=self._events,
            kill=kill,
            persona=persona,
            tools=tools,
            mark=mark,
            usage=self._usage,
            executor=self._executor,
            skills=self._skills,
            settings=self._settings,
        )
        self._oauth_flows = dict(oauth_flows or {})
        self._oauth_states: dict[str, tuple[str, OAuthFlow]] = {}
        self._oauth_lock = threading.Lock()

        self._clients = clients or ClientStore(home / "clients.json")
        self._resources = resources or ResourceSampler(home, home / "models")
        self._stack = stack or StackController()
        # Resource governor (docs/resources/README.md): one budget for the
        # engine, voice, tuning, research and disk; publishes `resources`.
        self._governor = ResourceGovernor(self._settings, data_root=home, publish=self._events.publish)
        self._resources.governor = self._governor
        set_governor(self._governor)
        self._govern_engine(model)

        self._httpd = _LoopbackHTTPServer((host, port), self._handler())
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

    def _govern_engine(self, model) -> None:
        native = getattr(model, "_native", None)
        if native is None and hasattr(model, "resource_hook"):
            native = model
        if native is None or not hasattr(native, "resource_hook"):
            return
        native.resource_hook = self._governor
        governor = self._governor

        def engine_stats() -> dict:
            # Never read stats while a generation holds the context.
            if governor.chat_active() or not getattr(native, "available", False):
                return {}
            return native.stats()

        governor.attach_engine(engine_stats)

    def start(self) -> "ApiServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        tuning_routes.attach(self)  # automation ticker; idle unless tuning.enabled
        self._governor.start()      # resource sampler (2 s), `resources` events
        return self

    def stop(self) -> None:
        tuning_routes.detach(self)  # checkpoint + stop a running training job
        self._governor.stop()
        self._events.close()
        # shutdown() waits for the serve loop to end, so it is only safe once
        # the server was actually started.
        if self._thread is not None:
            self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        # MCP servers are child processes / sessions: close them before the
        # model so a stdio server never outlives the app (connectors REQUESTS).
        mcp = getattr(self, "_mcp", None)
        if mcp is not None:
            try:
                mcp.close_all()
            except Exception:  # noqa: BLE001 - shutdown must finish
                pass
        # Close model client if it has a close method
        if hasattr(self._model, 'close'):
            try:
                self._model.close()
            except Exception:
                pass

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

    def _tool_mark(self, rule: str, subject: str, why: str) -> None:
        # Tool audit goes to the injected mark with its own rule id (G3-tool).
        if self._mark is None:
            return
        try:
            self._mark(rule, subject, why)
        except Exception:  # noqa: BLE001 - a broken sink must not break a tool
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

            def _raw(self, code: int, headers: dict, body: bytes) -> None:
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
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
                server._record(urlsplit(self.path).path, f"denied: host {name!r}")
                self._reply(403, {"error": "forbidden host"})
                return False

            @staticmethod
            def _same_token(candidate: str, expected: str) -> bool:
                try:
                    return secrets.compare_digest(candidate, expected)
                except TypeError:  # non-ASCII input can never match
                    return False

            def _authorized(self) -> bool:
                # The token is never written anywhere: not to a log, not to a
                # debug file. A failure marks only the path.
                header = self.headers.get("Authorization", "").strip()
                if header.lower().startswith("bearer "):
                    if self._same_token(header[7:].strip(), server.token):
                        return True
                if self.command == "GET":
                    # EventSource cannot set headers, so GET accepts ?token=.
                    query = parse_qs(urlsplit(self.path).query)
                    candidate = (query.get("token") or [""])[0]
                    if candidate and self._same_token(candidate, server.token):
                        return True
                server._record(urlsplit(self.path).path, "denied: bad token")
                self._reply(401, {"error": "unauthorized"})
                return False

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
                    # Connector-owned page (account name, auto-close, `connector`
                    # event); the built-in _callback stays as the fallback.
                    page = tool_routes.handle_callback(server, self.path)
                    if page is None:
                        self._callback()
                    else:
                        self._raw(*page)
                    return
                # Liveness probe: no secrets, so it answers before the token gate.
                if path == "/health":
                    self._reply(200, {"ok": True})
                    return
                if not path.startswith("/api/"):
                    if self._static(path):
                        return
                if not self._authorized():
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
                            "engine": _model_status(server),
                            "selected_model": _selected_model_name(server),
                            # A one-line summary of the live-fact tools so the
                            # shell can show honest state without a second call.
                            # The full detail is on /api/tools.
                            "tools": _tools_summary(server),
                            "voice": voice_routes.status_line(server),
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
                # Tools, approvals, permissions, agent limits: honest state
                # (which tools exist, whether they can run right now, and
                # what the permission policy would do with them).
                if path.startswith(voice_routes.PREFIX):  # audio bodies/replies: the voice package answers
                    voice_routes.dispatch(server, self, path)
                    return
                handled = (tuning_routes.handle_get(server, path, query=parse_qs(urlsplit(self.path).query))
                           or tool_routes.handle_get(server, path))
                if handled is not None:
                    self._reply(*handled)
                    return
                parts = [part for part in path.split("/") if part]
                if len(parts) == 3 and parts[:2] == ["api", "skills"]:
                    skill = server._skills.get(parts[2])
                    if skill is None:
                        self._reply(404, {"error": "unknown skill"})
                        return
                    self._reply(200, {"id": skill.id, "name": skill.name,
                                      "description": skill.description,
                                      "origin": skill.origin, "body": skill.body,
                                      "invoke": f"/{skill.id}"})
                    return
                if len(parts) == 3 and parts[:2] == ["api", "connectors"]:
                    connector = server._connectors.get(parts[2])
                    if connector is None:
                        self._reply(404, {"error": "unknown connector"})
                        return
                    view = connector.status()
                    view["oauth"] = parts[2] in server._oauth_flows
                    self._reply(200, view)
                    return
                if path == "/api/clients":
                    self._reply(200, {"clients": server._clients.list()})
                    return
                if path == "/api/usage":
                    self._reply(200, server._usage.view())
                    return
                if path == "/api/resources":
                    view = server._resources.view()
                    view["governor"] = server._governor.view()
                    self._reply(200, view)
                    return
                if path == "/api/resources/budget":
                    self._reply(200, server._governor.view())
                    return
                if path == "/api/stack/status":
                    self._reply(200, server._stack.status())
                    return
                if path == "/api/settings":
                    self._reply(200, {"sections": server._settings.sections()})
                    return
                if len(parts) == 3 and parts[:2] == ["api", "settings"]:
                    try:
                        values = server._settings.read(parts[2])
                    except ValueError as exc:
                        self._reply(400, {"error": str(exc)})
                        return
                    values = mask_secrets(parts[2], values)
                    if parts[2] == "resources":
                        # Live computed numbers, read-only (never stored).
                        values["computed"] = server._governor.view()
                    self._reply(200, {"section": parts[2], "values": values})
                    return
                if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
                    try:
                        session = Session.load(parts[2], server._sessions_root)
                    except (OSError, ValueError, KeyError, TypeError):
                        self._reply(404, {"error": "unknown session"})
                        return
                    self._reply(200, asdict(session.identity))
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
                # Models API
                if path == "/api/models" or path == "/api/models/":
                    if not _HAS_MODELS_API or get_model_manager is None:
                        self._reply(501, {"error": "models API not available"})
                        return
                    manager = get_model_manager()
                    models = manager.list_models()
                    selected = _selected_model_name(server)
                    payload = []
                    for model in models:
                        view = asdict(model)
                        view["selected"] = bool(
                            selected and model.path and Path(model.path).name == selected
                        )
                        payload.append(view)
                    disk = (manager.disk_status() if hasattr(manager, "disk_status")
                            else server._governor.disk_view())
                    self._reply(
                        200,
                        {
                            "models": payload,
                            "selected": selected,
                            "engine": _model_status(server),
                            "disk": disk,
                        },
                    )
                    return
                if path.startswith("/api/models/") and path != "/api/models/download":
                    model_id = path.split("/api/models/")[1]
                    if not _HAS_MODELS_API or get_model_manager is None:
                        self._reply(501, {"error": "models API not available"})
                        return
                    manager = get_model_manager()
                    models = manager.list_models()
                    for model in models:
                        if model.id == model_id:
                            self._reply(200, asdict(model))
                            return
                    self._reply(404, {"error": "Model not found"})
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

            # (A first, dead `do_PUT` that only handled /api/persona used to
            # sit here; the real one is below and handles persona + settings.)

            def do_POST(self) -> None:  # noqa: N802
                if not self._local():
                    return
                if not self._authorized():
                    return
                path = unquote(urlsplit(self.path).path)
                parts = [part for part in path.split("/") if part]
                if path.startswith(voice_routes.PREFIX):
                    voice_routes.dispatch(server, self, path)
                    return
                if path == "/api/resources":
                    payload = self._body()
                    if payload is None:
                        return
                    self._reply(*resources_update(server, payload))
                    return
                if tuning_routes.is_post_route(path) or tool_routes.is_post_route(path):
                    payload = self._body()
                    if payload is None:
                        return
                    routes = tuning_routes if tuning_routes.is_post_route(path) else tool_routes
                    self._reply(*routes.handle_post(server, path, payload))
                    return
                if path == "/api/system/pick-file":
                    # Native "choose a file" dialog for the page (filedialog.py).
                    payload = self._body()
                    if payload is None:
                        return
                    from . import filedialog

                    self._reply(*filedialog.handle(payload))
                    return
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
                        turn = server._turns.start(
                            session_id, prompt, mode=str(payload.get("mode") or "build"),
                            language=str(payload.get("language") or ""),
                        )
                    except ValueError as exc:
                        self._reply(400, {"error": str(exc)})
                        return
                    except RuntimeError as exc:
                        if "model unavailable" in str(exc).lower():
                            # Say which of the two things is missing: the engine
                            # library (an install problem) or a selected model
                            # (a choice the user makes in the Models tab).
                            fish = getattr(getattr(server, "_model", None), "_fish", None)
                            native = getattr(fish, "_native", None)
                            has_library = bool(getattr(native, "library_path", None))
                            if not has_library:
                                reason, message = "no_engine", (
                                    "The FISH engine library was not found (bin/fish.dll or "
                                    "libfish.so). Build it or run `python install.py --check`.")
                            else:
                                reason, message = "no_model", (
                                    "No model is selected. Choose one in the Models tab.")
                            self._reply(503, {"error": "model_unavailable", "reason": reason,
                                              "message": message})
                        else:
                            self._reply(500, {"error": str(exc)})
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

                if (
                    len(parts) == 4
                    and parts[:2] == ["api", "connectors"]
                    and parts[3] == "disconnect"
                ):
                    connector = server._connectors.get(parts[2])
                    if connector is None:
                        self._reply(404, {"error": "unknown connector"})
                        return
                    disconnected = False
                    if hasattr(connector, "disconnect"):
                        disconnected = bool(connector.disconnect())
                    server._events.publish(
                        "connector",
                        {
                            "connector": parts[2],
                            "connected": False,
                            "message": "Disconnected",
                        },
                    )
                    self._reply(200, {"disconnected": disconnected})
                    return

                # Stack control endpoints
                if path == "/api/stack/build":
                    result = server._stack.build()
                    self._reply(200 if result.get("ok") else 500, result)
                    return

                if path == "/api/stack/start":
                    result = server._stack.start()
                    self._reply(200 if result.get("ok") else 500, result)
                    return

                if path == "/api/stack/ensure":
                    result = server._stack.ensure()
                    self._reply(200 if result.get("ok") else 500, result)
                    return

                if path == "/api/stack/stop":
                    result = server._stack.stop()
                    self._reply(200 if result.get("ok") else 500, result)
                    return

                # Select the model the engine should use.
                if path == "/api/models/select":
                    if not _HAS_MODELS_API or get_model_manager is None:
                        self._reply(501, {"error": "models API not available"})
                        return
                    payload = self._body()
                    if payload is None:
                        return
                    model_id = str(payload.get("id") or payload.get("model_id") or "")
                    if not model_id:
                        self._reply(400, {"error": "id is required"})
                        return
                    manager = get_model_manager()
                    target = manager.get_model_path(model_id)
                    if target is None:
                        self._reply(404, {"error": "that model is not downloaded"})
                        return
                    if hasattr(manager, "is_compatible") and not manager.is_compatible(model_id):
                        self._reply(400, {
                            "error": "FISH cannot run this model's architecture yet. "
                                     "Pick a Llama-family model (for example Llama 3.2 3B "
                                     "or DeepSeek R1 Distill Llama 8B)."
                        })
                        return
                    # Use the queued model switch mechanism
                    result = server._turns.request_model_switch(target)
                    if not result.get("ok"):
                        self._reply(500, {"error": result.get("error", "model switch failed")})
                        return
                    # Persist the selected model so it loads on next startup
                    try:
                        from services.config import save_last_model
                        # `server.data_root` never existed: the AttributeError
                        # was swallowed and the choice was never persisted.
                        save_last_model(server._home, model_id)
                    except Exception:
                        pass
                    self._reply(200, {
                        "selected": target.name, 
                        "id": model_id,
                        "queued": result.get("queued", False)
                    })
                    return

                # Models API - POST /api/models/download (SSE progress)
                if path == "/api/models/download":
                    if not _HAS_MODELS_API or get_model_manager is None:
                        self._reply(501, {"error": "models API not available"})
                        return
                    payload = self._body()
                    if payload is None:
                        return
                    model_id = str(payload.get("model_id", ""))
                    token = payload.get("token")
                    if not model_id:
                        self._reply(400, {"error": "model_id is required"})
                        return

                    manager = get_model_manager()

                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    self.wfile.write(b"retry: 3000\n\n")
                    self.wfile.flush()

                    import asyncio
                    import dataclasses

                    async def stream_progress() -> None:
                        async for progress in manager.download_model(model_id, token):
                            data = (
                                dataclasses.asdict(progress)
                                if dataclasses.is_dataclass(progress)
                                else dict(progress)
                            )
                            if not data.get("percent") and hasattr(progress, "percent"):
                                data["percent"] = progress.percent
                            self.wfile.write(
                                f"data: {json.dumps(data)}\n\n".encode("utf-8")
                            )
                            self.wfile.flush()
                            if data.get("status") in ("complete", "error"):
                                return

                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(stream_progress())
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    except Exception as exc:  # noqa: BLE001 - report, never crash
                        try:
                            self.wfile.write(
                                f"data: {json.dumps({'status': 'error', 'error': str(exc)})}\n\n".encode("utf-8")
                            )
                            self.wfile.flush()
                        except OSError:
                            pass
                    finally:
                        loop.close()
                        self.close_connection = True
                    return

                # API clients
                if path == "/api/clients":
                    payload = self._body()
                    if payload is None:
                        return
                    try:
                        client = server._clients.create(
                            str(payload.get("name", "")),
                            payload.get("scopes") or [],
                        )
                    except ValueError as exc:
                        self._reply(400, {"error": str(exc)})
                        return
                    self._reply(201, client)
                    return

                # The browser shares its position so local questions can be
                # answered without an IP lookup.
                if path == "/api/tools/location":
                    payload = self._body()
                    if payload is None:
                        return
                    try:
                        from ..tools.builtin.location import set_client_location

                        if payload:
                            set_client_location(
                                {
                                    "name": str(payload.get("name") or "") or None,
                                    "region": str(payload.get("region") or "") or None,
                                    "country": str(payload.get("country") or "") or None,
                                    "latitude": payload.get("latitude"),
                                    "longitude": payload.get("longitude"),
                                    "timezone": str(payload.get("timezone") or "") or None,
                                }
                            )
                        else:
                            set_client_location(None)
                    except Exception as exc:  # noqa: BLE001 - reported, not raised
                        self._reply(500, {"error": str(exc)})
                        return
                    self._reply(200, {"ok": True})
                    return

                # Token rotation
                if path == "/api/auth/rotate":
                    server._token = secrets.token_urlsafe(24)
                    server._record("auth", "token rotated")
                    self._reply(200, {"token": server._token})
                    return

                # Resources
                if path == "/api/resources/clear":
                    result = server._resources.clear_cache()
                    self._reply(200, result)
                    return

                self._reply(404, {"error": "not found"})

            def do_PUT(self) -> None:  # noqa: N802
                if not self._local():
                    return
                if not self._authorized():
                    return
                path = unquote(urlsplit(self.path).path)
                if path == "/api/resources":
                    payload = self._body()
                    if payload is None:
                        return
                    self._reply(*resources_update(server, payload))
                    return
                if path == "/api/persona":
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
                    return

                parts = [part for part in path.split("/") if part]
                if len(parts) == 3 and parts[:2] == ["api", "settings"]:
                    payload = self._body()
                    if payload is None:
                        return
                    if parts[2] == "search":
                        # A client echoing the mask back must not overwrite a key.
                        payload = {k: v for k, v in dict(payload).items() if v != "••••••••"}
                    if parts[2] == "resources":
                        payload = {k: v for k, v in dict(payload).items() if k != "computed"}
                        try:
                            payload.update(validate_resources(payload))
                        except ValueError as exc:
                            self._reply(400, {"error": str(exc)})
                            return
                    try:
                        merged = server._settings.write(parts[2], payload)
                    except ValueError as exc:
                        self._reply(400, {"error": str(exc)})
                        return
                    if parts[2] == "search":
                        apply_search_env(merged)
                        server._record("settings", "search providers updated")
                    values = mask_secrets(parts[2], merged)
                    if parts[2] == "resources":
                        server._governor.refresh()
                        values = dict(values)
                        values["computed"] = server._governor.view()
                    self._reply(200, {"section": parts[2], "values": values})
                    return

                self._reply(404, {"error": "not found"})

            def do_DELETE(self) -> None:  # noqa: N802
                if not self._local():
                    return
                if not self._authorized():
                    return
                path = unquote(urlsplit(self.path).path)
                parts = [part for part in path.split("/") if part]
                handled = tool_routes.handle_delete(server, path)  # DELETE /api/mcp/servers/{id}
                if handled is not None:
                    self._reply(*handled)
                    return

                if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
                    try:
                        Session.delete(parts[2], server._sessions_root)
                    except (OSError, ValueError):
                        self._reply(404, {"error": "unknown session"})
                        return
                    server._record("session", f"deleted {parts[2]}")
                    self._reply(200, {"deleted": parts[2]})
                    return

                if len(parts) == 3 and parts[:2] == ["api", "clients"]:
                    if server._clients.revoke(parts[2]):
                        self._reply(200, {"revoked": parts[2]})
                    else:
                        self._reply(404, {"error": "unknown client"})
                    return

                if len(parts) == 3 and parts[:2] == ["api", "models"]:
                    if not _HAS_MODELS_API or get_model_manager is None:
                        self._reply(501, {"error": "models API not available"})
                        return
                    if get_model_manager().delete_model(parts[2]):
                        self._reply(200, {"status": "deleted", "model_id": parts[2]})
                    else:
                        self._reply(404, {"error": "Model not found or not downloaded"})
                    return

                if len(parts) == 3 and parts[:2] == ["api", "skills"]:
                    if server._skills.delete(parts[2]):
                        server._record("skills", f"deleted {parts[2]}")
                        self._reply(200, {"deleted": parts[2]})
                    else:
                        self._reply(404, {"error": "unknown skill"})
                    return

                self._reply(404, {"error": "not found"})


        return Handler
