"""Hosted models over the OpenAI-compatible chat API, standard library only.

NVIDIA, OpenRouter, DeepSeek, OpenAI, Groq, Together, Mistral and any
self-hosted OpenAI-compatible server all speak the same shape:
``POST <base>/chat/completions`` with ``Authorization: Bearer <key>``,
streamed as server-sent events, and (usually) ``GET <base>/models``. One
client speaks it for all of them; the differences (extra headers, the name
of the output-token limit, whether ``stream_options`` is understood, the
shape of the model list) are constructor options, set from the provider
registry in ``services/remote/providers.py``.

The client offers the same methods the server already uses on the local
engine: ``generate``, ``generate_stream``, ``cancel``, ``stats``,
``context_limit``, ``model_info``, ``selected_name``.

Safety (G5): a key is only ever sent to its own provider's host (the
``hosts`` allow-list, checked when the client is built and again for every
URL it requests), or to a loopback address, which is where tests point a
local fake. Redirects are never followed, so a 3xx cannot forward the
``Authorization`` header to another host, and loopback requests bypass any
configured HTTP proxy for the same reason. The key never appears in an
error message, a log line or a status view: provider error text is scrubbed
of it before it is shown.
"""
from __future__ import annotations

import urllib.parse

import asyncio
import ipaddress
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator, Callable, Iterable
from typing import Optional
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

# What the agent loop budgets for. Hosted windows are usually larger; a model
# whose catalogue entry says it is smaller gets its own, smaller, budget.
CONTEXT_TOKENS = 32768
# Upper bound for a window a provider reports (some publish 1M+).
MAX_CONTEXT_TOKENS = 2_097_152
_CONTEXT_IN_ERROR = re.compile(
    r"(?:maximum|max)\s+(?:context|input)\s+(?:length|window|size)\s+(?:is|of)\s+(\d[\d,]{2,})", re.I)
# A hostile or broken server must not exhaust memory.
MAX_LIST_BODY = 16_000_000     # one /models page (OpenRouter's full list is a few MB)
MAX_ERROR_BODY = 65_536

# Catalogue entries that are not chat models (embeddings, rerankers, safety
# classifiers, image/vision/audio/parsing models, legacy completion models).
_NOT_CHAT = ("embed", "rerank", "retriev", "reward", "guard", "safety", "clip", "parse",
             "paligemma", "detector", "kosmos", "deplot", "neva", "fuyu", "vila", "cosmos",
             "audio", "asr", "tts", "whisper", "pii", "ocr", "segment", "-vl", "vision",
             "content-safety", "topic-control", "jailbreak", "bge", "e5-", "arctic-embed",
             "dall-e", "moderation", "davinci", "babbage", "realtime", "transcribe",
             "image", "sora", "flux", "stable-diffusion", "-search-preview")

# Key shapes worth recognising in provider error text, so a key that a
# provider echoes back (OpenAI quotes a masked one) is never shown in full.
_KEY_PATTERN = re.compile(r"\b(?:sk|nvapi|gsk|tgp|xai|pk)[-_][A-Za-z0-9_\-]{6,}")

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


class RemoteError(RuntimeError):
    """A failed call to a hosted API. The message never contains the key.

    ``kind`` is a stable word the UI and tests can branch on:
    ``key`` (rejected key), ``credits``, ``rate_limit``, ``model_gone``,
    ``no_models`` (the server has no model list), ``unreachable``,
    ``redirect``, ``host`` (G5 refusal), ``server`` or ``other``.
    """

    def __init__(self, message: str, status: int = 0, kind: str = "other") -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind


def is_loopback(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_chat_model(model_id: str) -> bool:
    lowered = (model_id or "").lower()
    return bool(lowered) and not any(word in lowered for word in _NOT_CHAT)


def display_name(model_id: str) -> str:
    """"meta/llama-3.3-70b-instruct" -> "Llama 3.3 70B Instruct"."""
    name = (model_id or "").split("/")[-1].split(":")[0]
    words = []
    for part in name.replace("_", "-").split("-"):
        if not part:
            continue
        if part[:-1].replace(".", "").isdigit() and part[-1:].lower() in ("b", "m"):
            words.append(part[:-1] + part[-1].upper())
        elif part.lower() in ("it", "instruct", "chat", "coder", "base"):
            words.append(part.capitalize() if part.lower() != "it" else "IT")
        else:
            words.append(part[:1].upper() + part[1:])
    label = " ".join(words) or model_id
    if (model_id or "").endswith(":free"):
        label += " (free)"
    return label


def scrub(text: str, key: str | None) -> str:
    """Remove the key, and anything shaped like a key, from outgoing text."""
    text = str(text or "")
    if key:
        text = text.replace(key, "****")
    return _KEY_PATTERN.sub("****", text)


def check_base(base_url: str, hosts: Iterable[str], label: str = "the provider") -> str:
    """The base URL when a key may be sent there, else RemoteError (kind host).

    Allowed: https to a host in ``hosts``, or http/https to loopback.
    """
    parts = urlsplit(base_url or "")
    host = (parts.hostname or "").lower()
    allowed = {h.lower() for h in hosts if h}
    if parts.username or parts.password:
        raise RemoteError("the address must not contain a user name or password", kind="host")
    if host and is_loopback(host) and parts.scheme in ("http", "https"):
        return base_url.rstrip("/")
    if host in allowed and parts.scheme == "https":
        return base_url.rstrip("/")
    names = ", ".join(sorted(allowed)) or "its own host"
    raise RemoteError(f"the {label} key may only be sent to {names}", kind="host")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend Authorization to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401, ANN001
        return None


def _opener(host: str):
    handlers: list = [_NoRedirect()]
    if is_loopback(host):
        # A loopback call must never travel through an HTTP proxy (which would
        # see the Authorization header).
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _context_of(item: dict) -> int | None:
    for field in ("context_length", "context_window", "max_context_length", "max_model_len",
                  "context_size"):
        value = item.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
    top = item.get("top_provider")
    if isinstance(top, dict):
        value = top.get("context_length")
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def _is_chat_entry(item: dict, model_id: str) -> bool:
    """Catalogue metadata first (Together ``type``, Mistral ``capabilities``,
    OpenRouter ``architecture``), then the id heuristics."""
    kind = item.get("type")
    if isinstance(kind, str) and kind and kind.lower() not in ("chat", "language", "model", "llm", "text"):
        return False
    caps = item.get("capabilities")
    if isinstance(caps, dict) and caps.get("completion_chat") is False:
        return False
    arch = item.get("architecture")
    if isinstance(arch, dict):
        outputs = arch.get("output_modalities")
        if isinstance(outputs, list) and outputs and "text" not in outputs:
            return False
    return is_chat_model(model_id)


class OpenAICompatClient:
    """One hosted model behind the local engine's interface."""

    remote = True

    def __init__(
        self,
        key: Callable[[], Optional[str]] | str | None,
        model: str = "",
        *,
        base_url: str,
        hosts: Iterable[str] = (),
        provider: str = "custom",
        label: str = "Hosted",
        key_url: str = "",
        key_required: bool = True,
        headers: dict[str, str] | None = None,
        token_param: str = "max_tokens",
        stream_usage: bool = True,
        no_sampling_prefixes: Iterable[str] = (),
        recommended: Iterable[str] = (),
        probe_model: str = "",
        timeout: float = 120.0,
        temperature: float = 0.5,
        top_p: float = 0.9,
        max_tokens: int = 16384,
    ) -> None:
        self.provider = provider
        self.label = label
        self._key = key if callable(key) else (lambda value=key: value)
        self._model = model
        self._context: int | None = None
        self._on_context = None  # set by services/remote/hosted.py to save a learned window
        self._hosts = tuple(h.lower() for h in hosts if h)
        self._base = check_base(base_url, self._hosts, label)
        self._host = (urlsplit(self._base).hostname or "").lower()
        self._key_url = key_url
        self._key_required = bool(key_required)
        self._headers = dict(headers or {})
        self._token_param = token_param
        self._stream_usage = bool(stream_usage)
        self._no_sampling = tuple(p.lower() for p in no_sampling_prefixes)
        self._recommended = tuple(recommended)
        self._probe_model = probe_model
        self._timeout = float(timeout)
        self._temperature = float(temperature)
        self._top_p = float(top_p)
        self._max_tokens = int(max_tokens)
        self._cancel = threading.Event()
        self._response = None
        self._lock = threading.Lock()
        self._stats: dict = {}

    # -------------------------------------------------------------- state
    @property
    def base_url(self) -> str:
        return self._base

    @property
    def model(self) -> str:
        return self._model

    def set_model(self, model: str, context: int | None = None) -> None:
        self._model = (model or "").strip()
        self._context = int(context) if context else None

    def _has_key(self) -> bool:
        return bool(self._key()) or not self._key_required

    @property
    def configured(self) -> bool:
        return self._has_key() and bool(self._model)

    @property
    def available(self) -> bool:
        return self.configured

    @property
    def loaded(self) -> bool:
        return self.configured

    def context_limit(self) -> int:
        """The prompt budget IMURUN keeps a chat within: the model's window, but
        at most CONTEXT_TOKENS, so a long chat does not send 200k tokens (and
        their cost) on every turn. ``model_info`` reports the real window."""
        if self._context:
            return max(1024, min(self._context, CONTEXT_TOKENS))
        return CONTEXT_TOKENS

    @property
    def context_known(self) -> bool:
        return bool(self._context)

    def learn_context(self, value: int | None) -> bool:
        """Record a window reported by the provider (model details or an error)."""
        if isinstance(value, int) and 1024 <= value <= MAX_CONTEXT_TOKENS and value != self._context:
            self._context = value
            if self._on_context is not None:
                try:
                    self._on_context(self._model, value)
                except Exception:  # noqa: BLE001 - saving is a convenience
                    pass
            return True
        return False

    def fetch_context(self) -> int | None:
        """Ask the provider for this model's details (GET /models/{id}); OpenAI-
        compatible servers that publish a window return it there."""
        if not self._model or self._context:
            return self._context
        try:
            with self._request("GET", "/models/" + urllib.parse.quote(self._model, safe="/:"),
                               timeout=15) as response:
                data = json.loads(response.read(MAX_ERROR_BODY * 50) or b"{}")
        except Exception:  # noqa: BLE001 - many providers do not offer this route
            return None
        item = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
        value = _context_of(item) if isinstance(item, dict) else None
        if value:
            self.learn_context(value)
        return value

    def selected_name(self) -> str:
        return f"{display_name(self._model)} ({self.label})" if self._model else ""

    def model_info(self) -> dict:
        return {"remote": True, "provider": self.provider, "provider_label": self.label,
                "model": self._model, "name": display_name(self._model),
                "context": self.context_limit(),          # what a chat may use (budget)
                "context_window": self._context,          # the model's real window, when known
                "context_known": bool(self._context)}

    def stats(self) -> dict:
        return dict(self._stats)

    # -------------------------------------------------------------- http
    def _url(self, path: str) -> str:
        url = path if path.startswith(("http://", "https://")) else self._base + path
        # Every URL is re-checked: a pagination link or a bad path must never
        # carry the key to another host.
        parts, base = urlsplit(url), urlsplit(self._base)
        if ((parts.hostname or "").lower(), parts.scheme, parts.port) != (self._host, base.scheme, base.port):
            raise RemoteError(f"refused to send the {self.label} key to another host", kind="host")
        return url

    def _request(self, method: str, path: str, body: dict | None = None, *, key: str | None = None,
                 stream: bool = False, timeout: float | None = None):
        token = key if key is not None else self._key()
        if not token and self._key_required:
            raise RemoteError(f"no {self.label} API key is set", 401, kind="key")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "text/event-stream" if stream else "application/json",
                   "Content-Type": "application/json", "User-Agent": "imurun"}
        headers.update(self._headers)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = self._url(path)
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            return _opener(self._host).open(request, timeout=timeout or self._timeout)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, path, token) from None
        except urllib.error.URLError as exc:
            reason = scrub(str(getattr(exc, "reason", exc)), token)
            raise RemoteError(f"cannot reach {self.label} ({reason})", kind="unreachable") from None
        except (ValueError, UnicodeError):
            # http.client refuses a header with a line break or non-latin-1
            # text; its message would quote the whole Authorization value.
            raise RemoteError(f"the {self.label} key has characters a request header cannot carry",
                              kind="key") from None
        except (TimeoutError, OSError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc):
                raise RemoteError(f"{self.label} did not answer in time", kind="unreachable") from None
            raise RemoteError(f"cannot reach {self.label} ({type(exc).__name__})", kind="unreachable") from None

    def _http_error(self, exc: urllib.error.HTTPError, path: str, token: str | None) -> RemoteError:
        detail = ""
        try:
            payload = json.loads(exc.read(MAX_ERROR_BODY) or b"{}")
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    error = error.get("message") or error.get("code")
                detail = str(payload.get("detail") or payload.get("message") or error or "")
        except Exception:  # noqa: BLE001 - an unreadable body just loses the detail
            detail = ""
        detail = scrub(detail, token)[:200]
        code = exc.code
        lowered = detail.lower()
        found = _CONTEXT_IN_ERROR.search(detail)
        if found:
            self.learn_context(int(found.group(1).replace(",", "")))
        where = f" (get one at {self._key_url})" if self._key_url else ""
        if 300 <= code < 400:
            return RemoteError(f"{self.label} answered with a redirect; it was not followed, so the key "
                               "stayed here. Check the address.", code, kind="redirect")
        if code in (401, 403):
            return RemoteError(f"{self.label} rejected the API key{where}", code, kind="key")
        if code == 402:
            return RemoteError(f"{self.label} says this account is out of credits", code, kind="credits")
        if code == 429:
            return RemoteError(f"{self.label} rate limit reached; wait a moment and try again", code,
                               kind="rate_limit")
        if path.endswith("/models") and code in (404, 405, 501):
            return RemoteError(f"{self.label} has no model list at this address", code, kind="no_models")
        model_words = ("does not exist", "not found", "decommission", "deprecated", "invalid model",
                       "unknown model", "no longer", "not available", "no endpoints")
        if code == 404 or (code in (400, 410, 422) and "model" in lowered
                           and any(word in lowered for word in model_words)):
            name = self._model or "that model"
            return RemoteError(f"{self.label} no longer offers {name} to this key; choose another model",
                               code, kind="model_gone")
        if code >= 500:
            return RemoteError(f"{self.label} is having trouble (HTTP {code}); try again shortly", code,
                               kind="server")
        text = f"{self.label} returned HTTP {code}"
        return RemoteError(f"{text}: {detail}" if detail else text, code)

    def list_models(self, key: str | None = None, pages: int = 10) -> list[dict]:
        """The chat models the key can call, recommended ones first.

        Accepts the OpenAI shape ``{"data": [...]}`` and a bare list (Together),
        and follows OpenRouter's ``links.next`` on the same host only."""
        items: list = []
        path = "/models"
        for _ in range(max(1, pages)):
            with self._request("GET", path, key=key, timeout=20) as response:
                raw = response.read(MAX_LIST_BODY + 1)
            if len(raw) > MAX_LIST_BODY:
                raise RemoteError(f"{self.label} sent a model list larger than {MAX_LIST_BODY // 1_000_000} MB")
            payload = json.loads(raw or b"{}")
            if isinstance(payload, list):
                items.extend(payload)
                break
            if not isinstance(payload, dict):
                break
            items.extend(payload.get("data") or [])
            links = payload.get("links")
            nxt = links.get("next") if isinstance(links, dict) else None
            if not isinstance(nxt, str) or not nxt:
                break
            path = urljoin(self._base + "/", nxt) if not nxt.startswith(("http://", "https://")) else nxt
        seen: dict[str, dict] = {}
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            mid = str(item["id"])
            if mid in seen or not _is_chat_entry(item, mid):
                continue
            name = item.get("display_name") or item.get("name")
            seen[mid] = {"id": mid, "name": str(name) if isinstance(name, str) and name else display_name(mid),
                         "owner": str(item.get("owned_by") or item.get("organization") or mid.split("/")[0]),
                         "context": _context_of(item)}
        order = {mid: n for n, mid in enumerate(self._recommended)}
        rows = sorted(seen.values(), key=lambda row: (order.get(row["id"], len(order)), row["id"].lower()))
        for row in rows:
            row["recommended"] = row["id"] in order
        return rows

    def verify(self, key: str | None, mode: str = "models") -> None:
        """Raise RemoteError when ``key`` cannot be used.

        ``models``: one authenticated GET /models (free). ``chat``: a one-token
        completion, for providers whose model list is public."""
        if mode == "chat":
            body = {"model": self._model or self._probe_model, self._token_param: 1, "stream": False,
                    "messages": [{"role": "user", "content": "hi"}]}
            with self._request("POST", "/chat/completions", body, key=key, timeout=30) as response:
                response.read()
            return
        with self._request("GET", "/models", key=key, timeout=20) as response:
            response.read()

    # -------------------------------------------------------------- generation
    def _body(self, messages: Iterable[dict], stream: bool) -> dict:
        clean = []
        for message in messages:
            role = str(message.get("role") or "user")
            if role not in ("system", "user", "assistant"):
                role = "user"
            clean.append({"role": role, "content": str(message.get("content") or "")})
        if not self._model:
            raise RemoteError(f"no {self.label} model is selected")
        body: dict = {"model": self._model, "messages": clean, self._token_param: self._max_tokens,
                      "stream": stream}
        if not self._model.lower().split("/")[-1].startswith(self._no_sampling or ("\0",)):
            body["temperature"] = self._temperature
            body["top_p"] = self._top_p
        if stream and self._stream_usage:
            body["stream_options"] = {"include_usage": True}
        return body

    def _stream_chunks(self, messages: Iterable[dict], on_text: Callable[[str], None]) -> str:
        """Blocking: stream one completion, calling ``on_text`` per chunk.
        Reasoning models send their thoughts separately (``reasoning_content``
        or OpenRouter's ``reasoning``); they are wrapped in <think>...</think>
        so the shell's existing thinking display handles them."""
        self._cancel.clear()
        started = time.monotonic()
        first = None
        parts: list[str] = []
        thinking = False
        usage: dict = {}
        token = self._key()
        response = self._request("POST", "/chat/completions", self._body(messages, True), stream=True)
        with self._lock:
            self._response = response
        try:
            for raw in response:
                if self._cancel.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue  # blank lines and ": keep-alive" comments
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("error"):
                    error = event["error"]
                    message = error.get("message") if isinstance(error, dict) else error
                    raise RemoteError(f"{self.label} stopped the reply: {scrub(str(message), token)[:200]}")
                if event.get("usage"):
                    usage = event["usage"]
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    content = delta.get("content") or ""
                    text = ""
                    if isinstance(reasoning, str) and reasoning:
                        text += ("" if thinking else "<think>") + reasoning
                        thinking = True
                    if isinstance(content, str) and content:
                        text += ("</think>" if thinking else "") + content
                        thinking = False
                    if text:
                        first = first or time.monotonic()
                        parts.append(text)
                        on_text(text)
            if thinking:
                parts.append("</think>")
                on_text("</think>")
        finally:
            with self._lock:
                self._response = None
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
        elapsed = time.monotonic() - started
        generated = int(usage.get("completion_tokens") or len(parts))
        decode = max(1e-6, elapsed - ((first or started) - started))
        self._stats = {"prompt_tokens": int(usage.get("prompt_tokens") or 0), "generated_tokens": generated,
                       "decode_seconds": round(decode, 3), "prefill_seconds": round((first or started) - started, 3),
                       "tokens_per_second": round(generated / decode, 1), "context_size": self.context_limit(),
                       "streaming": True}
        return "".join(parts)

    def generate(self, messages: list[dict]) -> str:
        return self._stream_chunks(messages, lambda _text: None)

    async def generate_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        done = object()

        def work() -> None:
            try:
                self._stream_chunks(list(messages), lambda text: loop.call_soon_threadsafe(queue.put_nowait, text))
            except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        task = loop.run_in_executor(None, work)
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if not task.done():
                self.cancel()
            await task

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self.cancel()
