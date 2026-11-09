"""FISH model client implementing the ModelClient protocol with async streaming.

Two backends behind one interface:

* native — ``FishNativeClient`` loads the engine library in-process (default
  on a desktop). Conversations are tokenised structurally by the engine.
* http — a ``fish_server`` running as its own service (containers). Messages
  are posted as JSON and tokens arrive as SSE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator, Optional

try:  # run as ``imurun.model.fish_client``
    from ..services.config import FishConfig
    from ..services.response.loop import ModelClient
except ImportError:  # run as ``model.fish_client`` from the imurun directory
    from services.config import FishConfig
    from services.response.loop import ModelClient

logger = logging.getLogger(__name__)

# The native engine is the default on a desktop: it runs in-process from
# bin/fish.dll (or libfish.so / libfish.dylib). The HTTP client stays as the
# fallback for container deployments where FISH runs as its own service.
try:  # run as ``imurun.model.fish_client``
    from .fish_native import FishNativeClient
except ImportError:
    try:  # run as ``model.fish_client``
        from model.fish_native import FishNativeClient
    except ImportError:
        try:
            from fish_native import FishNativeClient
        except ImportError:  # pragma: no cover - native backend absent
            FishNativeClient = None


@dataclass
class FishParams:
    """Parameters for FISH generation. ``None`` means "use the config value"."""
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    top_k: Optional[int] = None
    repeat_penalty: Optional[float] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


class FishModelClient:
    """Async client for the FISH engine with streaming support."""

    def __init__(self, config: Optional[FishConfig] = None):
        self.config = config or FishConfig()
        self._native = None
        if FishNativeClient is not None:
            candidate = FishNativeClient(self.config)
            if candidate.library_path is not None:
                # The library is what makes this machine a native host; the
                # model file is chosen later (Models tab) and may be absent now.
                self._native = candidate

    @property
    def backend(self) -> str:
        """Which path is serving: "native" or "http"."""
        return "native" if self._native is not None else "http"

    # ----------------------------------------------------------------- http

    def _new_session(self):
        """A fresh aiohttp session for one request (imported lazily: the native
        path does not need aiohttp at all).

        Not cached: every synchronous call (``SyncFishModelClient._run``) and
        every streamed turn runs on its own short-lived event loop, and an
        aiohttp session is bound to the loop that created it. A cached one
        made the second turn fail with "Event loop is closed". The engine is
        on loopback, so a connection per request costs nothing measurable.
        """
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        return aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        """Release the native engine (HTTP sessions close per request)."""
        if self._native is not None:
            self._native.close()

    async def health(self) -> bool:
        """Check if the FISH engine can serve."""
        if self._native is not None:
            return self._native.health()
        try:
            async with self._new_session() as session:
                async with session.get(self.config.health_url) as response:
                    return response.status == 200
        except Exception as e:  # noqa: BLE001
            logger.warning("FISH health check failed: %s", e)
            return False

    async def stats(self) -> dict:
        """Get statistics from the FISH engine."""
        if self._native is not None:
            return self._native.stats()
        try:
            async with self._new_session() as session:
                async with session.get(self.config.stats_url) as response:
                    if response.status == 200:
                        return await response.json()
                    return {"error": f"HTTP {response.status}"}
        except Exception as e:  # noqa: BLE001
            logger.warning("FISH stats request failed: %s", e)
            return {"error": str(e)}

    def _request_params(self, params: Optional[FishParams]) -> dict:
        out = {
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_tokens,
            "top_k": 40,
            "repeat_penalty": 1.1,
        }
        if params is not None:
            out.update(params.to_dict())
        return out

    async def _http_stream(self, payload: dict) -> AsyncGenerator[str, None]:
        # ensure_ascii=False keeps non-Latin text readable for the engine's
        # small JSON reader (it decodes \uXXXX too, but plain UTF-8 is cheaper).
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            async with self._new_session() as session, session.post(
                self.config.stream_url,
                data=body,
                headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(f"FISH engine error {response.status}: {error_text}")

                async for raw in response.content:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        token = json.loads(data).get("token", "")
                    except json.JSONDecodeError:
                        logger.debug("Failed to parse SSE data: %s", data)
                        continue
                    if token:
                        yield token
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("FISH generation failed: %s", e)
            raise RuntimeError(f"FISH generation failed: {e}") from e

    # ------------------------------------------------------------- generate

    async def generate(self, prompt: str, params: Optional[FishParams] = None) -> AsyncGenerator[str, None]:
        """Stream tokens for a raw prompt (already rendered by the caller)."""
        if self._native is not None:
            async for token in self._native.generate_prompt_stream(prompt, params):
                yield token
            return
        async for token in self._http_stream({"prompt": prompt, "params": self._request_params(params)}):
            yield token

    async def generate_full(self, prompt: str, params: Optional[FishParams] = None) -> str:
        tokens = [token async for token in self.generate(prompt, params)]
        return "".join(tokens)

    async def generate_stream(self, messages: list[dict], params: Optional[FishParams] = None) -> AsyncGenerator[str, None]:
        """Stream tokens for a conversation.

        The native engine applies the chat template itself; the HTTP engine
        receives the messages and does the same on its side.
        """
        if self._native is not None:
            async for token in self._native.generate_stream(messages, params):
                yield token
            return
        payload = {
            "messages": [{"role": str(m.get("role", "user")), "content": str(m.get("content", ""))} for m in messages],
            "params": self._request_params(params),
        }
        async for token in self._http_stream(payload):
            yield token

    async def generate_messages_full(self, messages: list[dict], params: Optional[FishParams] = None) -> str:
        tokens = [token async for token in self.generate_stream(messages, params)]
        return "".join(tokens)

    def _messages_to_prompt(self, messages: list[dict]) -> str:
        """Render the conversation as text (for display / legacy callers)."""
        if self._native is not None:
            try:
                return self._native.chat_prompt(messages)
            except Exception as e:  # noqa: BLE001 - fall back, never fail
                logger.warning("chat template failed, using plain roles: %s", e)
        parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            label = {"system": "System", "user": "User", "assistant": "Assistant"}.get(role, role)
            parts.append(f"{label}: {content}")
        return "\n\n".join(parts) + "\n\nAssistant:"


class SyncFishModelClient:
    """Synchronous adapter for FishModelClient implementing ModelClient.

    The agent loop calls ``generate(messages)`` from a worker thread; this
    class runs the async client to completion for it. Streaming callers use
    ``generate_stream`` directly.
    """

    def __init__(self, config: Optional[FishConfig] = None):
        self._async_client = FishModelClient(config)
        self._health_at = 0.0
        self._health_ok = False

    @property
    def config(self):
        return self._async_client.config

    @property
    def _native(self):
        return getattr(self._async_client, "_native", None)

    @property
    def configured(self) -> bool:
        """True when an engine is configured, even if its weights are unloaded.

        Availability is about "can this machine serve a model", not "are the
        weights resident right now"; the native client loads on demand.
        """
        native = self._native
        if native is not None:
            return bool(getattr(native, "configured", False))
        # HTTP engine (model/fish/src/server.cpp): configured means it answers
        # its health probe (cached for a few seconds). ``server_url`` alone
        # always has the default value, so it said "ready" on a machine with
        # no engine at all and turns were accepted only to fail.
        return self.health()

    @property
    def loaded(self) -> bool:
        """True when the native engine has the weights resident."""
        return bool(getattr(self._native, "available", False))

    @property
    def last_used(self) -> float:
        return float(getattr(self._native, "last_used", 0.0) or 0.0)

    def unload_if_idle(self, timeout_seconds: float) -> bool:
        """Unload resident weights after inactivity; the next turn reloads them."""
        fn = getattr(self._native, "unload_if_idle", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(timeout_seconds))
        except Exception:  # noqa: BLE001 - keep-alive must never break a turn
            return False

    def select(self, path: Path | str) -> bool:
        """Switch the active model on the FISH backend."""
        fn = getattr(self._native, "select", None)
        if not callable(fn):
            return False
        try:
            return bool(fn(path))
        except Exception:  # noqa: BLE001
            return False

    def cancel(self) -> None:
        """Stop the generation in flight, if any."""
        fn = getattr(self._native, "cancel", None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    def context_limit(self) -> int:
        fn = getattr(self._native, "context_limit", None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:  # noqa: BLE001
                pass
        return int(getattr(self.config, "context_size", 0) or 4096)

    @property
    def available(self) -> bool:
        """True when the engine can answer. Never loads weights as a side effect."""
        return self.health()

    def health(self, ttl: float = 5.0) -> bool:
        now = time.time()
        if now - self._health_at < ttl:
            return self._health_ok
        ok = False
        if self._native is not None:
            ok = self._native.health()
        else:
            url = self._async_client.config.health_url
            try:
                import urllib.request

                with urllib.request.urlopen(url, timeout=0.75) as response:
                    ok = 200 <= response.status < 300
            except Exception:  # noqa: BLE001 - any failure means unavailable
                ok = False
        self._health_at = now
        self._health_ok = ok
        return ok

    def _run(self, coro):
        """Run a coroutine to completion from synchronous code, whether or not
        an event loop is already running in this thread."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    def generate(self, messages: list[dict]) -> str:
        """Synchronous generate for the ModelClient protocol."""
        return self._run(self._async_client.generate_messages_full(messages))

    async def ahealth(self) -> bool:
        return await self._async_client.health()

    async def astats(self) -> dict:
        return await self._async_client.stats()

    def stats(self) -> dict:
        """Last-generation counters from the native engine.

        The HTTP path has no cheap synchronous probe, so it returns an empty
        dict and the caller falls back to counting streamed tokens.
        """
        if self._native is None:
            return {}
        try:
            return dict(self._native.stats() or {})
        except Exception:  # noqa: BLE001 - counters must not break a turn
            return {}

    def model_info(self) -> dict:
        fn = getattr(self._native, "model_info", None)
        if not callable(fn):
            return {}
        try:
            return dict(fn() or {})
        except Exception:  # noqa: BLE001
            return {}

    def selected_name(self) -> str:
        """The model file the native engine serves, or an empty string."""
        path = getattr(self._native, "model_path", None)
        return Path(path).name if path else ""

    async def generate_stream(self, messages: list[dict], params: Optional[FishParams] = None) -> AsyncGenerator[str, None]:
        async for token in self._async_client.generate_stream(messages, params):
            yield token

    def _messages_to_prompt(self, messages: list[dict]) -> str:
        return self._async_client._messages_to_prompt(messages)

    def close(self) -> None:
        try:
            self._run(self._async_client.close())
        except Exception:  # noqa: BLE001 - closing must never raise
            pass
