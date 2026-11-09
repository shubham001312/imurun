"""MCP servers from Settings -> live clients -> model tools.

Configuration lives in settings section ``mcp``::

    {"servers": [{"id": "files", "name": "Files", "transport": "stdio" | "http",
                  "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/x"],
                  "env": {"KEY": "${env:MY_KEY}"}, "cwd": "",
                  "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${env:TOKEN}"},
                  "enabled": false, "auto_approve": ["read_file"], "timeout": 60}]}

Placeholders in ``command`` / ``args`` / ``cwd`` / ``env`` / ``headers`` /
``url``: ``${python}`` (this interpreter), ``${repo}`` (the imurun folder),
``${home}`` (the data root, ``~/.imurun``) and ``${env:NAME}`` (an
environment variable, so a key does not have to sit in the settings file).

Life cycle: enabled servers are connected in a background thread at start-up;
a server whose connection dropped is reconnected lazily on its next tool
call; ``POST /api/mcp/servers/{id}/connect`` reconnects by hand. Each
discovered tool becomes ``mcp_<id>_<tool>`` in the ``ToolRegistry``
(``services/tools/builtin/mcp_bridge.py``). A config change disconnects the
server and discovers again; ``notifications/tools/list_changed`` re-lists.

Events: ``mcp.connected``, ``mcp.disconnected``, ``mcp.error``.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..tools.base import ToolResult, failed
from ..tools.builtin.mcp_bridge import McpTool, bridged_name
from .client import (
    CALL_TIMEOUT,
    HttpTransport,
    McpClient,
    McpError,
    McpTimeout,
    StdioTransport,
    check_url,
)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TRANSPORTS = ("stdio", "http")
REDACTED = "********"
MAX_SERVERS = 32
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ServerConfig:
    id: str
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = False
    auto_approve: list[str] = field(default_factory=list)
    timeout: float = CALL_TIMEOUT

    @classmethod
    def parse(cls, raw: Any) -> "ServerConfig":
        if not isinstance(raw, dict):
            raise ValueError("a server must be an object")
        server_id = str(raw.get("id") or "").strip().lower()
        if not ID_RE.fullmatch(server_id):
            raise ValueError("id must be 1-32 characters: a-z, 0-9, '-' or '_'")
        transport = str(raw.get("transport") or "stdio").strip().lower()
        if transport not in TRANSPORTS:
            raise ValueError("transport must be stdio or http")
        args = raw.get("args") or []
        if not isinstance(args, list) or len(args) > 64:
            raise ValueError("args must be a list (at most 64 items)")
        env = raw.get("env") or {}
        headers = raw.get("headers") or {}
        if not isinstance(env, dict) or not isinstance(headers, dict):
            raise ValueError("env and headers must be objects")
        for key, value in list(env.items()) + list(headers.items()):
            if "\n" in str(key) + str(value) or "\r" in str(key) + str(value):
                raise ValueError("env and header values must be single lines")
        auto = raw.get("auto_approve") or []
        if not isinstance(auto, list):
            raise ValueError("auto_approve must be a list of tool names")
        try:
            timeout = float(raw.get("timeout") or CALL_TIMEOUT)
        except (TypeError, ValueError):
            raise ValueError("timeout must be a number of seconds") from None
        config = cls(
            id=server_id,
            name=str(raw.get("name") or server_id).strip()[:80] or server_id,
            transport=transport,
            command=str(raw.get("command") or "").strip(),
            args=[str(a) for a in args],
            env={str(k): str(v) for k, v in env.items()},
            cwd=str(raw.get("cwd") or "").strip(),
            url=str(raw.get("url") or "").strip(),
            headers={str(k): str(v) for k, v in headers.items()},
            enabled=bool(raw.get("enabled", False)),
            auto_approve=[str(n) for n in auto][:200],
            timeout=max(1.0, min(600.0, timeout)),
        )
        if transport == "stdio" and not config.command:
            raise ValueError("a stdio server needs a command")
        if transport == "http":
            if not config.url:
                raise ValueError("an http server needs a url")
            if "${" not in config.url:
                try:
                    check_url(config.url)
                except McpError as exc:
                    raise ValueError(str(exc)) from None
        return config

    def stored(self) -> dict:
        return {"id": self.id, "name": self.name, "transport": self.transport, "command": self.command,
                "args": list(self.args), "env": dict(self.env), "cwd": self.cwd, "url": self.url,
                "headers": dict(self.headers), "enabled": self.enabled,
                "auto_approve": list(self.auto_approve), "timeout": self.timeout}

    def view(self) -> dict:
        """For the API: env and header values are never returned."""
        view = self.stored()
        view["env"] = {key: REDACTED for key in self.env}
        view["headers"] = {key: REDACTED for key in self.headers}
        return view


class _State:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.client: McpClient | None = None
        self.status = "disabled" if not config.enabled else "disconnected"
        self.error = ""
        self.info: dict = {}
        self.tools: dict[str, McpTool] = {}
        self.lock = threading.Lock()
        self.connected_at: float | None = None


def expand(value: str, home: Path) -> str:
    def env(match: re.Match) -> str:
        return os.environ.get(match.group(1), "")

    text = str(value)
    text = text.replace("${python}", sys.executable).replace("${repo}", str(REPO_ROOT)).replace("${home}", str(home))
    return re.sub(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}", env, text)


class McpManager:
    def __init__(self, settings: Any, registry: Any, *, publish: Callable[[str, dict], Any] | None = None,
                 mark: Callable[[str, str], Any] | None = None, home: Path | str | None = None,
                 transport_factory: Callable[[ServerConfig, Callable], Any] | None = None) -> None:
        self.settings = settings
        self.registry = registry
        self._publish = publish
        self._mark = mark
        self.home = Path(home) if home else Path.home() / ".imurun"
        self._factory = transport_factory or self._transport
        self._states: dict[str, _State] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ config
    def configs(self) -> list[ServerConfig]:
        raw: list = []
        if self.settings is not None:
            try:
                raw = list((self.settings.read("mcp") or {}).get("servers") or [])
            except Exception:  # noqa: BLE001
                raw = []
        out: list[ServerConfig] = []
        seen: set[str] = set()
        for item in raw[:MAX_SERVERS]:
            try:
                config = ServerConfig.parse(item)
            except ValueError:
                continue
            if config.id not in seen:
                seen.add(config.id)
                out.append(config)
        return out

    def _save(self, configs: list[ServerConfig]) -> None:
        if self.settings is None:
            raise ValueError("there is no settings store")
        self.settings.write("mcp", {"servers": [c.stored() for c in configs]})

    def upsert(self, payload: dict, *, connect: bool = True) -> dict:
        configs = self.configs()
        existing = next((c for c in configs if c.id == str(payload.get("id") or "").strip().lower()), None)
        merged = dict(existing.stored()) if existing else {}
        merged.update(payload)
        if existing is not None:
            # A redacted value coming back from the UI means "unchanged".
            for key in ("env", "headers"):
                values = dict(merged.get(key) or {})
                for name, value in values.items():
                    if value == REDACTED:
                        values[name] = getattr(existing, key).get(name, "")
                merged[key] = values
        config = ServerConfig.parse(merged)
        if existing is None and len(configs) >= MAX_SERVERS:
            raise ValueError(f"at most {MAX_SERVERS} MCP servers")
        configs = [config if c.id == config.id else c for c in configs]
        if existing is None:
            configs.append(config)
        self._save(configs)
        self._note(config.id, "config saved")
        self.sync(background=True, connect=connect)
        return self.server_view(config.id) or config.view()

    def delete(self, server_id: str) -> bool:
        configs = self.configs()
        keep = [c for c in configs if c.id != server_id]
        if len(keep) == len(configs):
            return False
        self._save(keep)
        self._note(server_id, "removed")
        self.sync(background=False)
        return True

    # ------------------------------------------------------------ sync
    def sync(self, *, background: bool = True, connect: bool = True) -> None:
        """Bring live state in line with the settings."""
        configs = {c.id: c for c in self.configs()}
        to_connect: list[str] = []
        with self._lock:
            for server_id in list(self._states):
                state = self._states[server_id]
                new = configs.get(server_id)
                if new is None or new.stored() != state.config.stored():
                    self._shutdown(state, "configuration changed" if new else "server removed")
                    self._retire(state)
                    if new is None:
                        del self._states[server_id]
                        continue
                    self._states[server_id] = _State(new)
            for server_id, config in configs.items():
                if server_id not in self._states:
                    self._states[server_id] = _State(config)
            for server_id, state in self._states.items():
                if not state.config.enabled:
                    if state.client is not None:
                        self._shutdown(state, "disabled")
                    self._retire(state)
                    state.status = "disabled"
                elif state.client is None and state.status in ("disconnected", "disabled"):
                    state.status = "disconnected"
                    to_connect.append(server_id)
        if not connect:
            return
        for server_id in to_connect:
            if background:
                threading.Thread(target=self._safe_connect, args=(server_id,),
                                 name=f"mcp-connect-{server_id}", daemon=True).start()
            else:
                self._safe_connect(server_id)

    def _safe_connect(self, server_id: str) -> None:
        try:
            self.connect(server_id)
        except Exception:  # noqa: BLE001 - recorded in the state and an mcp.error event
            pass

    # ------------------------------------------------------------ connect
    def _transport(self, config: ServerConfig, on_notification: Callable):
        if config.transport == "http":
            return HttpTransport(expand(config.url, self.home),
                                 {k: expand(v, self.home) for k, v in config.headers.items()},
                                 on_notification=on_notification)
        cwd = expand(config.cwd, self.home) if config.cwd else str(REPO_ROOT)
        return StdioTransport(expand(config.command, self.home), [expand(a, self.home) for a in config.args],
                              {k: expand(v, self.home) for k, v in config.env.items()}, cwd,
                              on_notification=on_notification)

    def _state(self, server_id: str) -> _State | None:
        with self._lock:
            return self._states.get(server_id)

    def connect(self, server_id: str) -> dict:
        """Connect (or reconnect) and discover tools. Raises McpError on failure."""
        state = self._state(server_id)
        if state is None:
            raise KeyError(server_id)
        with state.lock:
            if state.client is not None:
                self._shutdown(state, "reconnecting", announce=False)
            state.status = "connecting"
            config = state.config
            try:
                transport = self._factory(config, lambda m, p: self._notification(server_id, m, p))
                client = McpClient(transport, timeout=config.timeout)
                info = client.connect()
                try:
                    specs = client.list_tools() if (client.capabilities.get("tools") is not None
                                                     or not client.capabilities) else []
                except McpError:
                    client.close()
                    raise
            except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised raw
                message = _reason(exc)
                state.status, state.error, state.client = "error", message, None
                self._retire(state)
                self._emit("mcp.error", {"id": server_id, "name": config.name, "error": message})
                self._note(server_id, f"connect failed: {type(exc).__name__}")
                raise McpError(message) from None
            if self._state(server_id) is not state:
                # The config changed while we were connecting: drop this one.
                client.close()
                raise McpError("the server configuration changed while connecting")
            state.client, state.info, state.error = client, info, ""
            state.status, state.connected_at = "connected", time.time()
            self._install_tools(state, specs)
        self._emit("mcp.connected", {"id": server_id, "name": config.name, "tools": len(state.tools),
                                     "protocolVersion": info.get("protocolVersion"),
                                     "server": (info.get("serverInfo") or {}).get("name", "")})
        self._note(server_id, f"connected, {len(state.tools)} tools")
        return self.server_view(server_id) or {}

    def disconnect(self, server_id: str, reason: str = "disconnected by the user") -> bool:
        state = self._state(server_id)
        if state is None:
            return False
        with state.lock:
            had = state.client is not None
            self._shutdown(state, reason)
            if not state.config.enabled:
                state.status = "disabled"
        return had

    def _shutdown(self, state: _State, reason: str, *, announce: bool = True) -> None:
        client, state.client = state.client, None
        if client is not None:
            client.close()
            if announce:
                self._emit("mcp.disconnected", {"id": state.config.id, "name": state.config.name, "reason": reason})
        if state.status == "connected":
            state.status = "disconnected"

    def _notification(self, server_id: str, method: str, params: dict) -> None:
        if method == "notifications/tools/list_changed":
            threading.Thread(target=self._relist, args=(server_id,), daemon=True).start()

    def _relist(self, server_id: str) -> None:
        state = self._state(server_id)
        if state is None or state.client is None:
            return
        with state.lock:
            try:
                specs = state.client.list_tools()
            except McpError:
                return
            self._install_tools(state, specs)

    # ------------------------------------------------------------ tools
    def _install_tools(self, state: _State, specs: list[dict]) -> None:
        config = state.config
        auto = set(config.auto_approve)
        fresh: dict[str, McpTool] = {}
        for spec in specs:
            name = bridged_name(config.id, str(spec.get("name")))
            base, n = name, 2
            while name in fresh:
                name, n = f"{base}_{n}", n + 1
            fresh[name] = McpTool(self, config.id, config.name, spec, name,
                                  network=config.transport == "http",
                                  auto_approve=str(spec.get("name")) in auto)
        for name, old in state.tools.items():
            if name not in fresh:
                old.retired = True
                self._unregister(name)
        state.tools = fresh
        if self.registry is not None:
            for tool in fresh.values():
                self.registry.register(tool)

    def _retire(self, state: _State) -> None:
        for name, tool in state.tools.items():
            tool.retired = True
            self._unregister(name)
        state.tools = {}

    def _unregister(self, name: str) -> None:
        # ``ToolRegistry.unregister`` removes the tool; the ``retired`` flag
        # still guards any reference a running turn kept to the old object.
        remove = getattr(self.registry, "unregister", None)
        if callable(remove):
            try:
                remove(name)
            except Exception:  # noqa: BLE001
                pass

    def tool_ready(self, server_id: str, tool_name: str) -> bool:
        state = self._state(server_id)
        return (state is not None and state.config.enabled and tool_name in state.tools
                and state.status in ("connected", "disconnected"))

    def unavailable_reason(self, server_id: str) -> str:
        state = self._state(server_id)
        if state is None:
            return "the MCP server was removed"
        if not state.config.enabled:
            return "the MCP server is turned off in Settings"
        if state.status == "error":
            return f"the MCP server failed: {state.error}"
        return ""

    def call(self, server_id: str, remote_name: str, args: dict, *, tool_name: str = "") -> ToolResult:
        label = tool_name or remote_name
        state = self._state(server_id)
        if state is None or not state.config.enabled:
            return failed(label, "That MCP server is not enabled.")
        client = state.client
        if client is None or not client.connected:
            try:
                self.connect(server_id)       # lazy reconnect
            except Exception as exc:  # noqa: BLE001
                return failed(label, f"The MCP server {state.config.name} is not reachable: {_reason(exc)}")
            client = state.client
            if client is None:
                return failed(label, "The MCP server is not connected.")
        started = time.monotonic()
        try:
            result = client.call_tool(remote_name, args, timeout=state.config.timeout)
        except McpTimeout as exc:
            self._note(server_id, f"{remote_name}: timed out")
            return failed(label, f"The MCP tool timed out: {exc}")
        except McpError as exc:
            message = _reason(exc)
            if not client.connected:
                with state.lock:
                    if state.client is client:
                        self._shutdown(state, "the server stopped answering")
                self._emit("mcp.error", {"id": server_id, "name": state.config.name, "error": message})
            self._note(server_id, f"{remote_name}: failed")
            return failed(label, f"The MCP tool failed: {message}")
        latency = int((time.monotonic() - started) * 1000)
        self._note(server_id, f"{remote_name}: {'ok' if result.ok else 'tool error'}")
        source = f"mcp:{state.config.name}"
        if not result.ok:
            return ToolResult(ok=False, content=result.text, error=result.text[:500],
                              source=source, latency_ms=latency, data=result.data)
        return ToolResult(ok=True, content=result.text, data=result.data, source=source, latency_ms=latency)

    # ------------------------------------------------------------ views
    def server_view(self, server_id: str) -> dict | None:
        state = self._state(server_id)
        if state is None:
            config = next((c for c in self.configs() if c.id == server_id), None)
            return None if config is None else {**config.view(), "status": "unknown", "tools": 0}
        info = state.info or {}
        return {
            **state.config.view(),
            "status": state.status,
            "error": state.error,
            "tools": len(state.tools),
            "protocolVersion": info.get("protocolVersion", ""),
            "serverInfo": info.get("serverInfo", {}),
            "connectedAt": state.connected_at,
        }

    def servers_view(self) -> list[dict]:
        with self._lock:
            ids = list(self._states)
        for config in self.configs():
            if config.id not in ids:
                ids.append(config.id)
        return [view for view in (self.server_view(i) for i in ids) if view is not None]

    def tools_view(self, server_id: str) -> list[dict] | None:
        state = self._state(server_id)
        if state is None:
            return None
        return [tool.view() for tool in state.tools.values()]

    def close_all(self) -> None:
        with self._lock:
            states = list(self._states.values())
        for state in states:
            with state.lock:
                self._shutdown(state, "IMURUN is shutting down", announce=False)

    # ------------------------------------------------------------ plumbing
    def _emit(self, kind: str, data: dict) -> None:
        if self._publish is None:
            return
        try:
            self._publish(kind, data)
        except Exception:  # noqa: BLE001
            pass

    def _note(self, server_id: str, why: str) -> None:
        if self._mark is None:
            return
        try:
            self._mark(f"mcp:{server_id}", why)
        except Exception:  # noqa: BLE001
            pass


def _reason(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return (text or type(exc).__name__)[:400]
