"""Model Context Protocol client: JSON-RPC 2.0 over stdio or Streamable HTTP.

Standard library only (``subprocess``, ``threading``, ``urllib``, ``json``).
Threads rather than asyncio, because the server and the tool executor are
thread based.

* ``StdioTransport`` spawns ``command`` + ``args`` with ``env`` and speaks
  newline-delimited JSON on stdin/stdout. A reader thread matches responses
  to requests by id, answers the server's own ``ping`` requests, and turns a
  dead process into a failed request (with the last lines of stderr), never
  a hang.
* ``HttpTransport`` POSTs each JSON-RPC message to one URL
  (``Accept: application/json, text/event-stream``), reads either a JSON
  body or an SSE stream until the matching response arrives, and keeps the
  ``Mcp-Session-Id`` header the server hands out on ``initialize``.
* ``McpClient`` runs the lifecycle: ``initialize`` (protocol ``2025-06-18``,
  accepting ``2025-03-26`` / ``2024-11-05`` when the server answers with
  one), ``notifications/initialized``, ``tools/list`` with ``nextCursor``
  pagination, ``tools/call`` (text, image, audio, resource content ->
  a text summary for the model, ``isError`` kept), ``ping`` and ``close``.

Every wait has a timeout. Every failure is an ``McpError``; the registry
turns those into failed tool results. Nothing here logs arguments, results,
environment values or headers.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_NAME = "imurun"
CLIENT_VERSION = "0.2"
CONNECT_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0
MAX_LINE = 8 * 1024 * 1024          # one JSON-RPC message from a stdio server
MAX_HTTP_BODY = 8 * 1024 * 1024
MAX_PAGES = 20
MAX_TOOLS = 500
MAX_RESULT_CHARS = 12000
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
_SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSW|API_?KEY|_KEY$|CREDENTIAL|COOKIE|SESSION)", re.IGNORECASE)


class McpError(RuntimeError):
    """The MCP server could not be reached or did not answer properly."""


class McpTimeout(McpError):
    """No answer within the timeout."""


class McpRemoteError(McpError):
    """The server answered with a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"server error {code}: {message}")
        self.code = code
        self.remote_message = message
        self.data = data


def _short(text: Any, limit: int = 300) -> str:
    return " ".join(str(text or "").split())[:limit]


# --------------------------------------------------------------------- stdio
class _Pending:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: dict | None = None


def child_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The parent environment without credential-looking names, plus ``extra``.

    Same rule as the shell tool: an MCP server gets the keys it is configured
    with, not every API key in the user's environment.
    """
    env = {key: value for key, value in os.environ.items() if not _SECRET_ENV.search(key)}
    env.update({str(k): str(v) for k, v in (extra or {}).items()})
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


class StdioTransport:
    kind = "stdio"

    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
                 cwd: str | None = None, *, on_notification: Callable[[str, dict], None] | None = None) -> None:
        if not command:
            raise McpError("a stdio MCP server needs a command")
        self.command = command
        self.args = [str(a) for a in (args or [])]
        self.env = dict(env or {})
        self.cwd = cwd or None
        self.on_notification = on_notification
        self._process: subprocess.Popen | None = None
        self._pending: dict[Any, _Pending] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr: deque[str] = deque(maxlen=20)
        self._closed = False
        self._exit_reason = ""

    # ------------------------------------------------------------ process
    def start(self) -> None:
        executable = shutil.which(self.command) or self.command
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            self._process = subprocess.Popen(
                [executable, *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=child_environment(self.env), cwd=self.cwd, creationflags=flags,
                start_new_session=(os.name != "nt"),
            )
        except (OSError, ValueError) as exc:
            raise McpError(f"could not start {os.path.basename(self.command)!r}: {_short(exc, 200)}") from None
        threading.Thread(target=self._read_stdout, name="mcp-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="mcp-stderr", daemon=True).start()

    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None and not self._closed

    def stderr_tail(self) -> str:
        return " | ".join(list(self._stderr)[-3:])

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for raw in iter(lambda: process.stderr.readline(4096), b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._stderr.append(line[:300])
        except (OSError, ValueError):
            pass

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                raw = process.stdout.readline(MAX_LINE + 1)
                if not raw:
                    break
                if len(raw) > MAX_LINE and not raw.endswith(b"\n"):
                    # An over-long message: skip to the next newline.
                    while raw and not raw.endswith(b"\n"):
                        raw = process.stdout.readline(MAX_LINE)
                    continue
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    continue     # a stray log line on stdout is ignored
                for item in message if isinstance(message, list) else [message]:
                    if isinstance(item, dict):
                        self._dispatch(item)
        except (OSError, ValueError):
            pass
        try:
            code = process.wait(timeout=0.5)   # EOF usually means it is exiting
        except subprocess.TimeoutExpired:
            code = None
        self._exit_reason = f"the MCP server exited (code {code})" if code is not None else "the MCP server closed its output"
        self._fail_all()

    def _dispatch(self, message: dict) -> None:
        if "method" in message:
            method = str(message.get("method"))
            if "id" in message:
                # A request from the server. We answer ping; we offer no
                # sampling / roots / elicitation capabilities.
                reply = ({"jsonrpc": "2.0", "id": message["id"], "result": {}} if method == "ping" else
                         {"jsonrpc": "2.0", "id": message["id"],
                          "error": {"code": -32601, "message": f"method not supported: {method}"}})
                try:
                    self._write(reply)
                except McpError:
                    pass
            elif self.on_notification is not None:
                try:
                    self.on_notification(method, message.get("params") or {})
                except Exception:  # noqa: BLE001 - a callback never kills the reader
                    pass
            return
        with self._lock:
            pending = self._pending.pop(message.get("id"), None)
        if pending is not None:
            pending.response = message
            pending.event.set()

    def _fail_all(self) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.event.set()

    def _write(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise McpError(self._dead())
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (OSError, ValueError):
                raise McpError(self._dead()) from None

    def _dead(self) -> str:
        reason = self._exit_reason
        if not reason and self._process is not None and self._process.poll() is not None:
            reason = f"the MCP server exited (code {self._process.poll()})"
        reason = reason or "the MCP server is not running"
        tail = self.stderr_tail()
        return f"{reason}{': ' + tail if tail else ''}"

    # ------------------------------------------------------------ messages
    def request(self, message: dict, timeout: float) -> dict:
        pending = _Pending()
        with self._lock:
            self._pending[message["id"]] = pending
        try:
            self._write(message)
        except McpError:
            with self._lock:
                self._pending.pop(message["id"], None)
            raise
        if not pending.event.wait(max(0.1, timeout)):
            with self._lock:
                self._pending.pop(message["id"], None)
            try:
                self.notify({"jsonrpc": "2.0", "method": "notifications/cancelled",
                             "params": {"requestId": message["id"], "reason": "timed out"}})
            except McpError:
                pass
            raise McpTimeout(f"no answer to {message.get('method')} within {timeout:g} s")
        if pending.response is None:
            raise McpError(self._dead())
        return pending.response

    def notify(self, message: dict) -> None:
        self._write(message)

    def set_protocol_version(self, version: str) -> None:  # HTTP only
        return None

    def close(self) -> None:
        self._closed = True
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()      # the spec's polite shutdown: close stdin
        except OSError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                pass
        self._fail_all()
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def _kill_tree(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        else:
            import signal

            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------- http
def check_url(url: str) -> str:
    parts = urllib.parse.urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise McpError("the MCP url must be http(s)://host/...")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        raise McpError("plain http is only allowed for a server on this computer; use https")
    return urllib.parse.urlunsplit(parts)


class HttpTransport:
    kind = "http"

    def __init__(self, url: str, headers: dict[str, str] | None = None, *,
                 on_notification: Callable[[str, dict], None] | None = None) -> None:
        self.url = check_url(url)
        self.headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self.on_notification = on_notification
        self.session_id = ""
        self.protocol_version = ""
        self._open = False

    def start(self) -> None:
        self._open = True

    def alive(self) -> bool:
        return self._open

    def stderr_tail(self) -> str:
        return ""

    def set_protocol_version(self, version: str) -> None:
        self.protocol_version = version

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION}"}
        headers.update(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, message: dict, timeout: float):
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, headers=self._headers(), method="POST")
        try:
            return urllib.request.urlopen(request, timeout=max(0.5, timeout))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and self.session_id:
                self.session_id = ""
                raise McpError("the MCP session expired; reconnect") from None
            detail = ""
            try:
                detail = _short(exc.read(400).decode("utf-8", errors="replace"), 160)
            except Exception:  # noqa: BLE001
                pass
            raise McpError(f"HTTP {exc.code} from {urllib.parse.urlsplit(self.url).netloc}"
                           f"{': ' + detail if detail else ''}") from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if "timed out" in str(reason):
                raise McpTimeout(f"no answer from {urllib.parse.urlsplit(self.url).netloc} within {timeout:g} s") from None
            raise McpError(f"network error: {_short(reason, 160)}") from None
        except TimeoutError:
            raise McpTimeout(f"no answer within {timeout:g} s") from None
        except OSError as exc:
            raise McpError(f"network error: {type(exc).__name__}") from None

    def request(self, message: dict, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        response = self._post(message, timeout)
        with response:
            session = response.headers.get("Mcp-Session-Id")
            if session:
                self.session_id = session.strip()[:400]
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in content_type:
                return self._read_sse(response, message["id"], deadline, timeout)
            raw = response.read(MAX_HTTP_BODY + 1)
        if len(raw) > MAX_HTTP_BODY:
            raise McpError("the MCP server answered with more than 8 MB")
        try:
            data = json.loads(raw.decode("utf-8", errors="replace") or "null")
        except ValueError:
            raise McpError("the MCP server answered with something that is not JSON") from None
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("id") == message["id"] and "method" not in item:
                return item
            if isinstance(item, dict):
                self._side_message(item, timeout)
        raise McpError("the MCP server's answer did not contain a response to the request")

    def _read_sse(self, response, request_id, deadline: float, timeout: float) -> dict:
        data_lines: list[str] = []
        total = 0
        while True:
            if time.monotonic() > deadline:
                raise McpTimeout(f"no answer within {timeout:g} s")
            try:
                raw = response.readline(MAX_LINE)
            except (TimeoutError, OSError):
                raise McpTimeout(f"no answer within {timeout:g} s") from None
            if not raw:
                break
            total += len(raw)
            if total > MAX_HTTP_BODY:
                raise McpError("the MCP event stream sent more than 8 MB")
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
                continue
            if line == "" and data_lines:
                payload, data_lines = "\n".join(data_lines), []
                try:
                    message = json.loads(payload)
                except ValueError:
                    continue
                for item in message if isinstance(message, list) else [message]:
                    if not isinstance(item, dict):
                        continue
                    if item.get("id") == request_id and "method" not in item:
                        return item
                    self._side_message(item, timeout)
        raise McpError("the MCP event stream ended before the response arrived")

    def _side_message(self, item: dict, timeout: float) -> None:
        method = item.get("method")
        if not method:
            return
        if "id" in item:
            reply = ({"jsonrpc": "2.0", "id": item["id"], "result": {}} if method == "ping" else
                     {"jsonrpc": "2.0", "id": item["id"],
                      "error": {"code": -32601, "message": f"method not supported: {method}"}})
            try:
                self.notify(reply, timeout=min(timeout, 10.0))
            except McpError:
                pass
        elif self.on_notification is not None:
            try:
                self.on_notification(str(method), item.get("params") or {})
            except Exception:  # noqa: BLE001
                pass

    def notify(self, message: dict, timeout: float = 10.0) -> None:
        response = self._post(message, timeout)
        with response:
            response.read(65536)

    def close(self) -> None:
        if self._open and self.session_id:
            request = urllib.request.Request(self.url, headers=self._headers(), method="DELETE")
            try:
                with urllib.request.urlopen(request, timeout=5.0) as response:
                    response.read(1024)
            except Exception:  # noqa: BLE001 - 405 is allowed by the spec
                pass
        self._open = False
        self.session_id = ""


# -------------------------------------------------------------------- client
@dataclass
class CallResult:
    ok: bool
    text: str
    is_error: bool = False
    data: dict = field(default_factory=dict)


def content_to_text(result: dict, limit: int = MAX_RESULT_CHARS) -> str:
    """MCP ``content`` blocks -> one text for a small model."""
    parts: list[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind in ("image", "audio"):
            size = len(str(block.get("data") or "")) * 3 // 4
            parts.append(f"[{kind}: {block.get('mimeType') or 'unknown type'}, about {size} bytes, not shown]")
        elif kind == "resource":
            resource = block.get("resource") or {}
            uri = resource.get("uri") or "resource"
            if "text" in resource:
                parts.append(f"[resource {uri}]\n{resource.get('text') or ''}")
            else:
                parts.append(f"[binary resource {uri}: {resource.get('mimeType') or 'unknown type'}, not shown]")
        elif kind == "resource_link":
            parts.append(f"[link: {block.get('name') or ''} {block.get('uri') or ''}]".strip())
        else:
            parts.append(f"[{kind or 'unknown'} content not shown]")
    if not any(p.strip() for p in parts) and result.get("structuredContent") is not None:
        parts.append(json.dumps(result.get("structuredContent"), ensure_ascii=False)[: limit])
    text = "\n".join(p for p in parts if p is not None).strip()
    if len(text) > limit:
        text = text[:limit] + f"\n... [result cut, {len(text) - limit} more characters]"
    return text


class McpClient:
    def __init__(self, transport, *, timeout: float = CALL_TIMEOUT,
                 connect_timeout: float = CONNECT_TIMEOUT) -> None:
        self.transport = transport
        self.timeout = float(timeout)
        self.connect_timeout = float(connect_timeout)
        self.protocol_version = ""
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.instructions = ""
        self._ids = itertools.count(1)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self.transport.alive()

    def _rpc(self, method: str, params: dict | None, timeout: float) -> dict:
        message: dict = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            message["params"] = params
        response = self.transport.request(message, timeout)
        error = response.get("error")
        if error:
            if isinstance(error, dict):
                raise McpRemoteError(int(error.get("code") or 0), _short(error.get("message")), error.get("data"))
            raise McpRemoteError(0, _short(error))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def connect(self) -> dict:
        self.transport.start()
        try:
            result = self._rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            }, self.connect_timeout)
            version = str(result.get("protocolVersion") or "")
            if version not in SUPPORTED_VERSIONS:
                raise McpError(f"the server speaks MCP {version or '(unknown)'}; "
                               f"supported: {', '.join(SUPPORTED_VERSIONS)}")
            self.protocol_version = version
            self.server_info = dict(result.get("serverInfo") or {})
            self.capabilities = dict(result.get("capabilities") or {})
            self.instructions = _short(result.get("instructions"), 1000)
            self.transport.set_protocol_version(version)
            self.transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.close()
            raise
        self._connected = True
        return {"protocolVersion": self.protocol_version, "serverInfo": self.server_info,
                "capabilities": self.capabilities}

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(MAX_PAGES):
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {}, self.timeout)
            for tool in result.get("tools") or []:
                if isinstance(tool, dict) and tool.get("name"):
                    tools.append(tool)
            cursor = result.get("nextCursor")
            if not cursor or len(tools) >= MAX_TOOLS:
                break
        return tools[:MAX_TOOLS]

    def call_tool(self, name: str, arguments: dict | None, timeout: float | None = None) -> CallResult:
        result = self._rpc("tools/call", {"name": name, "arguments": dict(arguments or {})},
                           timeout or self.timeout)
        is_error = bool(result.get("isError"))
        text = content_to_text(result) or ("(the tool returned nothing)" if not is_error else "the tool reported an error")
        data = {}
        if isinstance(result.get("structuredContent"), dict):
            data = result["structuredContent"]
        return CallResult(ok=not is_error, text=text, is_error=is_error, data=data)

    def ping(self, timeout: float = 5.0) -> bool:
        self._rpc("ping", None, timeout)
        return True

    def close(self) -> None:
        self._connected = False
        try:
            self.transport.close()
        except Exception:  # noqa: BLE001 - closing never raises
            pass
