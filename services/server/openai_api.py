"""OpenAI-compatible API on the loopback server (``/v1/*``).

Other apps (OpenClaw, the ``openai`` Python package, curl, anything that takes
"a base URL plus an API key") reach the local model through:

    GET  /v1/models                 the served model, OpenAI list shape
    GET  /v1/models/{id}            one model
    POST /v1/chat/completions       messages, stream, max_tokens, temperature,
                                    top_p, stop (SSE chunks end with [DONE])

and the shell's "Use by API" panel reads and regenerates the key through:

    GET  /api/openai                {baseUrl, apiKey, model, keyFile, ...}
    POST /api/openai/rotate         a new key; the old one stops working now

The key is persistent (``<data_root>/secrets/openai_api_key``, 0600 on POSIX)
so a key pasted into another app survives restarts, unlike the per-run shell
token. ``/v1`` also accepts the per-run token and scoped client tokens
(``POST /api/clients``) that carry the ``chat`` or ``admin`` scope.

Generation goes through the same model client the chat uses; the native
engine serialises every call on ``FishNativeClient._gen_lock``, so an API call
waits for a running chat turn instead of touching the engine context.

The key and request headers are never logged; a refusal marks only the path.
No CORS: OpenClaw and SDK clients are not browsers, and a browser page on
another origin has no business driving the local model (see the README).
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:  # run as ``imurun.services.server.openai_api``
    from ...model.fish_client import FishParams
except (ImportError, ValueError):  # run as ``services.server.openai_api``
    try:
        from model.fish_client import FishParams
    except ImportError:  # pragma: no cover - the bindings are always shipped
        FishParams = None

V1_PREFIX = "/v1/"
UI_PATHS = ("/api/openai", "/api/openai/rotate")
KEY_PREFIX = "sk-imurun-"
OWNED_BY = "imurun"
ROLES = {"system", "user", "assistant", "developer", "tool", "function"}
MAX_STOP = 4
# /v1 generations waiting on (or holding) the engine at once. The engine runs
# one generation at a time; more than this queued means a runaway client, and
# the user's own chat would wait behind all of them.
MAX_PENDING = 4
_PENDING = threading.BoundedSemaphore(MAX_PENDING)


# --------------------------------------------------------------------- key


class ApiKeyStore:
    """One persistent API key in a private file under ``<root>/secrets``."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._key: str | None = None

    @staticmethod
    def _new() -> str:
        return KEY_PREFIX + secrets.token_urlsafe(32)

    def _write(self, key: str) -> None:
        folder = self.path.parent
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
        temporary = self.path.with_name(self.path.name + ".tmp")
        # A leftover tmp (crash) keeps its old mode and a planted symlink
        # would be followed: remove it, then create a fresh 0600 file.
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(temporary), flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self) -> str:
        """The key, created on first use."""
        with self._lock:
            if self._key:
                return self._key
            try:
                key = self.path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                key = ""
            # Any other OSError (a lock held by antivirus, a permission
            # problem) propagates: silently minting a new key would break
            # every app the user configured.
            if not key.startswith(KEY_PREFIX) or len(key) < len(KEY_PREFIX) + 20:
                key = self._new()
                self._write(key)
            else:
                for target, mode in ((self.path, 0o600), (self.path.parent, 0o700)):
                    try:
                        os.chmod(target, mode)
                    except OSError:
                        pass
            self._key = key
            return key

    def rotate(self) -> str:
        with self._lock:
            key = self._new()
            self._write(key)
            self._key = key
            return key

    def created_at(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def matches(self, candidate: str) -> bool:
        try:
            return secrets.compare_digest(candidate, self.get())
        except TypeError:  # non-ASCII input can never match
            return False
        except OSError:  # the key file cannot be read: nothing matches
            return False


_STORE_LOCK = threading.Lock()


def key_store(server) -> ApiKeyStore:
    """The server's key store, attached on first use (keeps api.py untouched)."""
    store = getattr(server, "_openai_keys", None)
    if store is not None:
        return store
    with _STORE_LOCK:
        store = getattr(server, "_openai_keys", None)
        if store is None:
            home = Path(getattr(server, "_home", None) or (Path.home() / ".imurun"))
            store = ApiKeyStore(home / "secrets" / "openai_api_key")
            server._openai_keys = store
        return store


# ------------------------------------------------------------------- model


def _model(server):
    turns = getattr(server, "_turns", None)
    return getattr(turns, "_model", None) or getattr(server, "_model", None)


def _available(server) -> bool:
    if _model(server) is None:
        return False
    turns = getattr(server, "_turns", None)
    if turns is not None and hasattr(turns, "available"):
        try:
            return bool(turns.available)
        except Exception:  # noqa: BLE001 - an unreachable model is unavailable
            return False
    return True


def model_id(server) -> str:
    """The id clients send as ``model``: the served file name without .gguf."""
    model = _model(server)
    name = ""
    getter = getattr(model, "selected_name", None)
    if callable(getter):
        try:
            name = str(getter() or "")
        except Exception:  # noqa: BLE001
            name = ""
    if name.lower().endswith(".gguf"):
        name = name[:-5]
    return name or "imurun"


def _model_card(server) -> dict:
    return {
        "id": model_id(server),
        "object": "model",
        "created": int(getattr(server, "_started_at", time.time())),
        "owned_by": OWNED_BY,
    }


def base_url(server) -> str:
    return f"http://127.0.0.1:{server.port}/v1"


def _context_window(server) -> int:
    limiter = getattr(_model(server), "context_limit", None)
    if callable(limiter):
        try:
            return int(limiter())
        except Exception:  # noqa: BLE001
            pass
    return 4096


def ui_view(server) -> dict:
    store = key_store(server)
    available = _available(server)
    model = _model(server)
    max_tokens = getattr(getattr(model, "config", None), "max_tokens", None)
    return {
        "baseUrl": base_url(server),
        "apiKey": store.get(),
        "model": model_id(server) if available else "",
        "available": available,
        "contextWindow": _context_window(server),
        "maxTokens": max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else 2048,
        "keyFile": str(store.path),
        "keyCreatedAt": store.created_at(),
        "endpoints": ["/v1/models", "/v1/chat/completions"],
    }


# ---------------------------------------------------------------- replies


def _error(handler, status: int, message: str, kind: str, code: str | None = None, param: str | None = None) -> None:
    _json(handler, status, {"error": {"message": message, "type": kind, "param": param, "code": code}})


def _json(handler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _bad(handler, message: str, param: str | None = None) -> None:
    _error(handler, 400, message, "invalid_request_error", None, param)


# ------------------------------------------------------------------- auth


def _authorized_v1(server, handler) -> bool:
    header = handler.headers.get("Authorization", "").strip()
    candidate = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if candidate:
        if key_store(server).matches(candidate):
            return True
        try:
            if secrets.compare_digest(candidate, server.token):
                return True
        except TypeError:
            pass
        clients = getattr(server, "_clients", None)
        if clients is not None and candidate.startswith("imu_"):
            try:
                client = clients.authenticate(candidate)
            except Exception:  # noqa: BLE001 - a broken store refuses, never crashes
                client = None
            if client and ({"chat", "admin"} & set(client.get("scopes") or [])):
                return True
    server._record(urlsplit(handler.path).path, "denied: bad api key")
    if candidate:
        message = "Incorrect API key provided. Copy the key from IMURUN Settings > Use by API."
    else:
        message = "You didn't provide an API key. Send it as 'Authorization: Bearer <key>'."
    _error(handler, 401, message, "invalid_request_error", "invalid_api_key")
    return False


# ------------------------------------------------------------------ body


def _read_body(server, handler) -> dict | None:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        _bad(handler, "Invalid Content-Length header.")
        return None
    limit = int(getattr(server, "_max_body", 1_000_000))
    if length > limit:
        server._record(urlsplit(handler.path).path, "denied: body too large")
        if length <= limit + 65536:
            handler._drain(length)
        else:
            handler.close_connection = True
        _error(handler, 413, f"Request body is larger than {limit} bytes.", "invalid_request_error", "request_too_large")
        return None
    if length <= 0:
        _bad(handler, "A JSON request body is required.")
        return None
    raw = handler.rfile.read(length)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        _bad(handler, "We could not parse the JSON body of your request.")
        return None
    if not isinstance(payload, dict):
        _bad(handler, "The request body must be a JSON object.")
        return None
    return payload


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text_of(content, index: int) -> str:
    """OpenAI content: a string, null, or a list of parts. Text only."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
                continue
            if not isinstance(part, dict):
                raise ValueError(f"messages[{index}].content has an invalid part")
            kind = part.get("type")
            if kind in ("text", "input_text"):
                out.append(str(part.get("text") or ""))
            else:
                raise ValueError(
                    f"messages[{index}].content part type {kind!r} is not supported: IMURUN serves text-only models"
                )
        return "".join(out)
    raise ValueError(f"messages[{index}].content must be a string or a list of text parts")


def parse_request(body: dict) -> dict:
    """Validate a chat.completions body; ``ValueError(message, param)`` on bad input."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("'messages' must be a non-empty array", "messages")
    converted = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"messages[{index}] must be an object", "messages")
        role = message.get("role")
        if role not in ROLES:
            raise ValueError(f"messages[{index}].role {role!r} is not one of {sorted(ROLES)}", "messages")
        try:
            text = _text_of(message.get("content"), index)
        except ValueError as exc:
            raise ValueError(str(exc), "messages") from None
        if role == "developer":
            role = "system"
        elif role in ("tool", "function"):
            # Chat templates know system/user/assistant; a tool result is
            # handed back as user text so the model still sees it.
            name = message.get("name") or message.get("tool_call_id") or "tool"
            text, role = f"Tool result ({name}):\n{text}", "user"
        converted.append({"role": role, "content": text})

    out = {"messages": converted, "stream": False, "max_tokens": None, "temperature": None,
           "top_p": None, "stop": [], "include_usage": False}
    stream = body.get("stream", False)
    if stream is not None and not isinstance(stream, bool):
        raise ValueError("'stream' must be a boolean", "stream")
    out["stream"] = bool(stream)
    options = body.get("stream_options")
    if isinstance(options, dict):
        out["include_usage"] = bool(options.get("include_usage"))

    for name in ("max_tokens", "max_completion_tokens"):
        value = body.get(name)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"'{name}' must be a positive integer", name)
        out["max_tokens"] = value
    temperature = body.get("temperature")
    if temperature is not None:
        if not _number(temperature) or not 0 <= temperature <= 2:
            raise ValueError("'temperature' must be a number between 0 and 2", "temperature")
        out["temperature"] = float(temperature)
    top_p = body.get("top_p")
    if top_p is not None:
        if not _number(top_p) or not 0 < top_p <= 1:
            raise ValueError("'top_p' must be a number in (0, 1]", "top_p")
        out["top_p"] = float(top_p)
    stop = body.get("stop")
    if stop is not None:
        stops = [stop] if isinstance(stop, str) else stop
        if not isinstance(stops, list) or len(stops) > MAX_STOP or not all(isinstance(s, str) for s in stops):
            raise ValueError("'stop' must be a string or an array of up to 4 strings", "stop")
        out["stop"] = [s for s in stops if s]
    n = body.get("n")
    if n is not None and n != 1:
        raise ValueError("Only n=1 is supported", "n")
    return out


# -------------------------------------------------------------- generation


class StopFilter:
    """Cut the stream at the first stop string, holding back only as many
    characters as a stop string could still span."""

    def __init__(self, stops: list[str]) -> None:
        self.stops = [s for s in stops if s]
        self.hold = max((len(s) for s in self.stops), default=1) - 1
        self.buffer = ""
        self.hit = False

    def feed(self, text: str) -> str:
        if not self.stops:
            return text
        self.buffer += text
        cut = min((i for i in (self.buffer.find(s) for s in self.stops) if i >= 0), default=-1)
        if cut >= 0:
            self.hit = True
            out, self.buffer = self.buffer[:cut], ""
            return out
        safe = len(self.buffer) - self.hold
        if safe <= 0:
            return ""
        out, self.buffer = self.buffer[:safe], self.buffer[safe:]
        return out

    def flush(self) -> str:
        out, self.buffer = self.buffer, ""
        return out


def _accepts_params(fn) -> bool:
    try:
        parameters = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = [p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2 or any(p.kind == p.VAR_POSITIONAL for p in parameters)


async def _tokens(model, messages: list[dict], params):
    """Yield text chunks from whatever the model client offers."""
    streamer = getattr(model, "generate_stream", None)
    if callable(streamer):
        agen = streamer(messages, params) if params is not None and _accepts_params(streamer) else streamer(messages)
        async with contextlib.aclosing(agen) as chunks:
            async for chunk in chunks:
                if chunk:
                    yield str(chunk)
        return
    text = await asyncio.get_running_loop().run_in_executor(None, model.generate, [dict(m) for m in messages])
    if text:
        yield str(text)


def _params(request: dict):
    if FishParams is None:
        return None
    return FishParams(
        temperature=request["temperature"],
        top_p=request["top_p"],
        max_tokens=request["max_tokens"],
    )


def _effective_max(model, request: dict) -> int | None:
    if request["max_tokens"]:
        return request["max_tokens"]
    value = getattr(getattr(model, "config", None), "max_tokens", None)
    return value if isinstance(value, int) and value > 0 else None


def _usage(model, messages: list[dict], chunks: int) -> dict:
    """Engine counters when the native engine reports them, else estimates.

    ``stats()`` describes the last generation on the shared context; a chat
    turn finishing in the same instant could overwrite it, so it is a
    best-effort count, which is all OpenAI clients use it for.
    """
    stats = {}
    getter = getattr(model, "stats", None)
    if callable(getter):
        try:
            stats = getter() or {}
        except Exception:  # noqa: BLE001
            stats = {}
    prompt = completion = 0
    if isinstance(stats, dict) and "error" not in stats:
        prompt = int(stats.get("prompt_tokens") or 0)
        completion = int(stats.get("generated_tokens") or 0)
    if not prompt:
        prompt = max(1, sum(len(m["content"]) for m in messages) // 4)
    if not completion:
        completion = chunks
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        # Finalises the engine's own async generators: an early stop (stop
        # string, client gone) reaches FishNativeClient._stream's `finally`,
        # which cancels the generation and releases _gen_lock.
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


def _record_usage(server, completion: int) -> None:
    usage = getattr(server, "_usage", None)
    if usage is not None and completion > 0:
        try:
            usage.record_tokens(completion)
        except Exception:  # noqa: BLE001 - counters never fail a request
            pass


def _completion(server, handler, request: dict) -> None:
    model = _model(server)
    served = model_id(server)
    created = int(time.time())
    ident = "chatcmpl-" + secrets.token_hex(12)
    messages = request["messages"]
    stops = StopFilter(request["stop"])
    limit = _effective_max(model, request)

    def finish(chunks: int) -> str:
        if stops.hit:
            return "stop"
        if limit and chunks >= limit:
            return "length"
        return "stop"

    if not request["stream"]:
        async def collect():
            parts: list[str] = []
            chunks = 0
            async with contextlib.aclosing(_tokens(model, messages, _params(request))) as gen:
                async for chunk in gen:
                    chunks += 1
                    parts.append(stops.feed(chunk))
                    if stops.hit:
                        break
            parts.append(stops.flush() if not stops.hit else "")
            return "".join(parts), chunks

        try:
            text, chunks = _run(collect())
        except Exception as exc:  # noqa: BLE001 - the client gets an OpenAI error
            server._record("/v1/chat/completions", f"generation failed: {type(exc).__name__}")
            _error(handler, 500, "Generation failed in the local engine; see IMURUN for details.",
                   "server_error", "generation_failed")
            return
        usage = _usage(model, messages, chunks)
        _record_usage(server, usage["completion_tokens"])
        _json(handler, 200, {
            "id": ident,
            "object": "chat.completion",
            "created": created,
            "model": served,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text, "refusal": None},
                "logprobs": None,
                "finish_reason": finish(chunks),
            }],
            "usage": usage,
            "system_fingerprint": None,
        })
        return

    def chunk(delta: dict, reason=None) -> bytes:
        payload = {
            "id": ident, "object": "chat.completion.chunk", "created": created, "model": served,
            "system_fingerprint": None,
            "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": reason}],
        }
        return b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    handler.close_connection = True

    def send(data: bytes) -> None:
        handler.wfile.write(data)
        handler.wfile.flush()

    async def pump():
        chunks = 0
        send(chunk({"role": "assistant", "content": ""}))
        async with contextlib.aclosing(_tokens(model, messages, _params(request))) as gen:
            async for piece in gen:
                chunks += 1
                shown = stops.feed(piece)
                if shown:
                    send(chunk({"content": shown}))
                if stops.hit:
                    break
        if not stops.hit:
            tail = stops.flush()
            if tail:
                send(chunk({"content": tail}))
        return chunks

    try:
        chunks = _run(pump())
    except (BrokenPipeError, ConnectionResetError):
        return  # the client went away; the engine was cancelled on close
    except Exception as exc:  # noqa: BLE001 - reported in-band, then [DONE]
        server._record("/v1/chat/completions", f"generation failed: {type(exc).__name__}")
        try:
            error = {"error": {"message": "Generation failed in the local engine; see IMURUN for details.",
                               "type": "server_error", "param": None, "code": "generation_failed"}}
            send(b"data: " + json.dumps(error).encode("utf-8") + b"\n\n")
            send(b"data: [DONE]\n\n")
        except OSError:
            pass
        return
    usage = _usage(model, messages, chunks)
    _record_usage(server, usage["completion_tokens"])
    try:
        send(chunk({}, finish(chunks)))
        if request["include_usage"]:
            final = {"id": ident, "object": "chat.completion.chunk", "created": created, "model": served,
                     "system_fingerprint": None, "choices": [], "usage": usage}
            send(b"data: " + json.dumps(final).encode("utf-8") + b"\n\n")
        send(b"data: [DONE]\n\n")
    except OSError:
        pass


# ---------------------------------------------------------------- routing


def handles(path: str) -> bool:
    return path == "/v1" or path.startswith(V1_PREFIX) or path in UI_PATHS


def dispatch(server, handler, method: str) -> None:
    """Entry point from ``api.py``; ``handler._local()`` has already passed."""
    path = unquote(urlsplit(handler.path).path).rstrip("/") or "/"
    if path in UI_PATHS:
        # The shell token in the Authorization header only: api.py also takes
        # ?token= on GET (for EventSource), and a URL that lands in history
        # or a log must not be able to yield the long-lived key.
        if not handler.headers.get("Authorization", "").strip().lower().startswith("bearer "):
            server._record(path, "denied: header token required")
            _json(handler, 401, {"error": "unauthorized"})
            return
        if not handler._authorized():
            return
        try:
            if method == "GET" and path == "/api/openai":
                _json(handler, 200, ui_view(server))
            elif method == "POST" and path == "/api/openai/rotate":
                key_store(server).rotate()
                _json(handler, 200, ui_view(server))
            else:
                _json(handler, 405, {"error": "method not allowed"})
        except OSError:
            server._record(path, "api key file unreadable")
            _json(handler, 503, {"error": "key_unreadable",
                                 "message": "The API key file cannot be read; check its permissions."})
        return

    if not _authorized_v1(server, handler):
        return
    if path == "/v1/models":
        if method != "GET":
            _error(handler, 405, "Use GET /v1/models.", "invalid_request_error", "method_not_allowed")
            return
        data = [_model_card(server)] if _available(server) else []
        _json(handler, 200, {"object": "list", "data": data})
        return
    if path.startswith("/v1/models/"):
        wanted = path[len("/v1/models/"):]
        if method == "GET" and _available(server) and wanted == model_id(server):
            _json(handler, 200, _model_card(server))
        else:
            _error(handler, 404, f"The model '{wanted}' does not exist.", "invalid_request_error", "model_not_found", "model")
        return
    if path == "/v1/chat/completions":
        if method != "POST":
            _error(handler, 405, "Use POST /v1/chat/completions.", "invalid_request_error", "method_not_allowed")
            return
        body = _read_body(server, handler)
        if body is None:
            return
        try:
            request = parse_request(body)
        except ValueError as exc:
            message, param = (exc.args + (None,))[:2]
            _bad(handler, str(message), param)
            return
        if not _available(server):
            _error(handler, 503, "No model is loaded in IMURUN. Choose one in the Models tab, then retry.",
                   "server_error", "model_not_available")
            return
        # `model` is accepted as given: IMURUN serves exactly one model, and
        # tools that insist on their own id (an alias in their config) must
        # not fail. The reply's `model` names what actually answered.
        # A reply can never be longer than the context; a huge max_tokens
        # would pin the engine (and the user's chat behind it) for nothing.
        if request["max_tokens"]:
            request["max_tokens"] = min(request["max_tokens"], _context_window(server))
        if not _PENDING.acquire(blocking=False):
            _error(handler, 429, f"IMURUN is already handling {MAX_PENDING} API requests; retry shortly.",
                   "rate_limit_error", "too_many_requests")
            return
        try:
            _completion(server, handler, request)
        finally:
            _PENDING.release()
        return
    _error(handler, 404, f"Unknown endpoint {path}. IMURUN serves /v1/models and /v1/chat/completions.",
           "invalid_request_error", "unknown_url")
