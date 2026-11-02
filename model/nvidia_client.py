"""NVIDIA-hosted models (build.nvidia.com) as a second compute path.

NVIDIA's API is OpenAI-compatible: ``POST /v1/chat/completions`` with a
``Bearer nvapi-...`` key, streamed as server-sent events, and
``GET /v1/models`` for the catalogue. Many models are free to call with a
developer key. This client speaks that API with the standard library only
(no new dependency) and offers the same methods the server already uses on
the local engine: ``generate``, ``generate_stream``, ``cancel``, ``stats``,
``context_limit``, ``model_info``, ``selected_name``.

Safety (G5): the key is only ever sent to NVIDIA's API host, or to a
loopback address (tests point ``base_url`` at a local fake). The key never
appears in an error message, a log line or a status view.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator, Callable, Iterable
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_HOST = "integrate.api.nvidia.com"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

# Shown first, and switched on in the selector by default when the key's
# catalogue offers them. Anything else from the catalogue can be added.
RECOMMENDED = (
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-8b-instruct",
    "deepseek-ai/deepseek-r1",
    "qwen/qwen2.5-coder-32b-instruct",
    "nvidia/llama-3.1-nemotron-70b-instruct",
    "mistralai/mistral-small-24b-instruct",
)
DEFAULT_ENABLED = RECOMMENDED[:3]

# Catalogue entries that are not chat models (embeddings, rerankers, safety
# classifiers, vision/audio/parsing models).
_NOT_CHAT = ("embed", "rerank", "retriev", "reward", "guard", "safety", "clip", "parse",
             "paligemma", "detector", "kosmos", "deplot", "neva", "fuyu", "vila", "cosmos",
             "audio", "asr", "tts", "whisper", "pii", "ocr", "segment", "-vl", "vision",
             "content-safety", "topic-control", "jailbreak", "bge", "e5-", "arctic-embed")

CONTEXT_TOKENS = 32768   # what the agent loop budgets for; hosted windows are larger
PROBE_MODEL = "meta/llama-3.1-8b-instruct"


class RemoteError(RuntimeError):
    """A failed call to the hosted API. The message never contains the key."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def is_chat_model(model_id: str) -> bool:
    lowered = (model_id or "").lower()
    return bool(lowered) and not any(word in lowered for word in _NOT_CHAT)


def display_name(model_id: str) -> str:
    """"meta/llama-3.3-70b-instruct" -> "Llama 3.3 70B Instruct"."""
    name = (model_id or "").split("/")[-1]
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
    return " ".join(words) or model_id


def _check_base(base_url: str) -> str:
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if host == NVIDIA_HOST and parts.scheme == "https":
        return base_url.rstrip("/")
    if host in _LOOPBACK and parts.scheme in ("http", "https"):
        return base_url.rstrip("/")
    raise RemoteError(f"the NVIDIA key may only be sent to {NVIDIA_HOST}")


class NvidiaClient:
    """One NVIDIA-hosted model behind the local engine's interface."""

    remote = True
    provider = "nvidia"

    def __init__(
        self,
        key: Callable[[], Optional[str]] | str | None,
        model: str = "",
        *,
        base_url: str = NVIDIA_BASE_URL,
        timeout: float = 120.0,
        temperature: float = 0.5,
        top_p: float = 0.9,
        max_tokens: int = 2048,
    ) -> None:
        self._key = key if callable(key) else (lambda value=key: value)
        self._model = model
        self._base = _check_base(base_url)
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
    def model(self) -> str:
        return self._model

    def set_model(self, model: str) -> None:
        self._model = (model or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self._key()) and bool(self._model)

    @property
    def available(self) -> bool:
        return self.configured

    @property
    def loaded(self) -> bool:
        return self.configured

    def context_limit(self) -> int:
        return CONTEXT_TOKENS

    def selected_name(self) -> str:
        return f"{display_name(self._model)} (NVIDIA)" if self._model else ""

    def model_info(self) -> dict:
        return {"remote": True, "provider": self.provider, "model": self._model,
                "name": display_name(self._model), "context": CONTEXT_TOKENS}

    def stats(self) -> dict:
        return dict(self._stats)

    # -------------------------------------------------------------- http
    def _request(self, method: str, path: str, body: dict | None = None, *, key: str | None = None,
                 stream: bool = False, timeout: float | None = None):
        token = key if key is not None else self._key()
        if not token:
            raise RemoteError("no NVIDIA API key is set", 401)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self._base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream" if stream else "application/json",
                     "Content-Type": "application/json", "User-Agent": "imurun"},
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout or self._timeout)  # noqa: S310 - host is pinned
        except urllib.error.HTTPError as exc:
            raise RemoteError(_http_message(exc), exc.code) from None
        except urllib.error.URLError as exc:
            raise RemoteError(f"cannot reach NVIDIA ({getattr(exc, 'reason', exc)})") from None
        except TimeoutError:
            raise RemoteError("NVIDIA did not answer in time") from None

    def list_models(self, key: str | None = None) -> list[dict]:
        """The chat models the key can call, recommended ones first."""
        with self._request("GET", "/models", key=key, timeout=20) as response:
            payload = json.loads(response.read() or b"{}")
        items = payload.get("data") if isinstance(payload, dict) else None
        ids = sorted({str(item.get("id")) for item in items or [] if isinstance(item, dict) and item.get("id")})
        chat = [i for i in ids if is_chat_model(i)]
        order = {mid: n for n, mid in enumerate(RECOMMENDED)}
        chat.sort(key=lambda mid: (order.get(mid, len(order)), mid))
        return [{"id": mid, "name": display_name(mid), "owner": mid.split("/")[0],
                 "recommended": mid in order} for mid in chat]

    def verify(self, key: str) -> None:
        """Raise RemoteError when ``key`` cannot make a chat call. One token."""
        body = {"model": self._model or PROBE_MODEL, "max_tokens": 1, "stream": False,
                "messages": [{"role": "user", "content": "hi"}]}
        with self._request("POST", "/chat/completions", body, key=key, timeout=30) as response:
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
            raise RemoteError("no NVIDIA model is selected")
        body = {"model": self._model, "messages": clean, "temperature": self._temperature,
                "top_p": self._top_p, "max_tokens": self._max_tokens, "stream": stream}
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def _stream_chunks(self, messages: Iterable[dict], on_text: Callable[[str], None]) -> str:
        """Blocking: stream one completion, calling ``on_text`` per chunk.
        Reasoning models send their thoughts separately; they are wrapped in
        <think>...</think> so the shell's existing thinking display handles them."""
        self._cancel.clear()
        started = time.monotonic()
        first = None
        parts: list[str] = []
        thinking = False
        usage: dict = {}
        response = self._request("POST", "/chat/completions", self._body(messages, True), stream=True)
        with self._lock:
            self._response = response
        try:
            for raw in response:
                if self._cancel.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if event.get("usage"):
                    usage = event["usage"]
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content") or ""
                    content = delta.get("content") or ""
                    text = ""
                    if reasoning:
                        text += ("" if thinking else "<think>") + reasoning
                        thinking = True
                    if content:
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
                       "tokens_per_second": round(generated / decode, 1), "context_size": CONTEXT_TOKENS,
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


def _http_message(exc: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        payload = json.loads(exc.read() or b"{}")
        error = payload.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        detail = str(payload.get("detail") or payload.get("message") or error or "")
    except Exception:  # noqa: BLE001 - an unreadable body just loses the detail
        detail = ""
    if exc.code in (401, 403):
        return "NVIDIA rejected the API key (check it on build.nvidia.com)"
    if exc.code == 404:
        return "NVIDIA does not offer that model to this key"
    if exc.code == 429:
        return "NVIDIA rate limit reached; wait a moment and try again"
    text = f"NVIDIA returned HTTP {exc.code}"
    return f"{text}: {detail[:200]}" if detail else text
