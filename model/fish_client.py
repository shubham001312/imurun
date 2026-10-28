"""FISH model client implementing the ModelClient protocol with async SSE streaming."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

import aiohttp

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
    """Parameters for FISH generation."""
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 2048
    context_size: int = 4096

    def to_dict(self) -> dict:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "context_size": self.context_size,
        }


class FishModelClient:
    """Async client for the FISH engine with SSE streaming support."""

    def __init__(self, config: Optional[FishConfig] = None):
        self.config = config or FishConfig()
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._native = None
        if FishNativeClient is not None:
            candidate = FishNativeClient(self.config)
            if candidate.configured:
                self._native = candidate

    @property
    def backend(self) -> str:
        """Which path is serving: "native" or "http"."""
        return "native" if self._native is not None else "http"

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session."""
        async with self._session_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
                self._session = aiohttp.ClientSession(timeout=timeout)
            return self._session

    async def close(self) -> None:
        """Close the HTTP session and release the native engine."""
        if self._native is not None:
            self._native.close()
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                await self._session.close()
                self._session = None

    async def health(self) -> bool:
        """Check if the FISH engine is healthy."""
        if self._native is not None:
            return self._native.health()
        try:
            session = await self._get_session()
            async with session.get(self.config.health_url) as response:
                return response.status == 200
        except Exception as e:
            logger.warning(f"FISH health check failed: {e}")
            return False

    async def stats(self) -> dict:
        """Get statistics from the FISH engine."""
        if self._native is not None:
            return self._native.stats()
        try:
            session = await self._get_session()
            async with session.get(self.config.stats_url) as response:
                if response.status == 200:
                    return await response.json()
                return {"error": f"HTTP {response.status}"}
        except Exception as e:
            logger.warning(f"FISH stats request failed: {e}")
            return {"error": str(e)}

    async def generate(
        self,
        prompt: str,
        params: Optional[FishParams] = None,
    ) -> AsyncGenerator[str, None]:
        """Generate tokens via SSE stream from FISH engine.

        Yields:
            Individual token strings as they arrive from the stream.
        """
        if params is None:
            params = FishParams(
                temperature=self.config.temperature,
                top_p=self.config.top_p,
                max_tokens=self.config.max_tokens,
                context_size=self.config.context_size,
            )

        if self._native is not None:
            # Raw prompt: the caller already rendered it (see chat_prompt).
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()
            done = object()

            def _cb(text, _token_id, _user):
                if text:
                    loop.call_soon_threadsafe(
                        queue.put_nowait, text.decode("utf-8", "replace")
                    )

            from .fish_native import FISH_TOKEN_CB as _CB  # type: ignore
            callback = _CB(_cb)

            def _work():
                try:
                    self._native._generate(prompt, params, callback)
                except BaseException as exc:  # noqa: BLE001
                    loop.call_soon_threadsafe(queue.put_nowait, exc)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, done)

            task = loop.run_in_executor(None, _work)
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
                    self._native.cancel()
                await task
            return

        payload = {
            "prompt": prompt,
            "params": params.to_dict(),
        }

        session = await self._get_session()
        try:
            async with session.post(
                self.config.stream_url,
                json=payload,
                headers={"Accept": "text/event-stream"},
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(f"FISH engine error {response.status}: {error_text}")

                async for line in response.content:
                    line = line.decode("utf-8").strip()
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data = line[6:]  # Remove "data: " prefix
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            token = chunk.get("token", "")
                            if token:
                                yield token
                        except json.JSONDecodeError:
                            logger.debug(f"Failed to parse SSE data: {data}")
                            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"FISH generation failed: {e}")
            raise RuntimeError(f"FISH generation failed: {e}") from e

    async def generate_full(self, prompt: str, params: Optional[FishParams] = None) -> str:
        """Generate full response (collects all tokens).

        This is a convenience method for synchronous compatibility.
        """
        tokens = []
        async for token in self.generate(prompt, params):
            tokens.append(token)
        return "".join(tokens)

    async def generate_stream(
        self,
        messages: list[dict],
        params: Optional[FishParams] = None,
    ) -> AsyncGenerator[str, None]:
        """Generate tokens via SSE stream from FISH engine, accepting messages format.

        Args:
            messages: List of message dicts with 'role' and 'content' keys.
            params: Optional generation parameters.

        Yields:
            Individual token strings as they arrive from the stream.
        """
        # Convert messages to a single prompt string
        prompt = self._messages_to_prompt(messages)
        async for token in self.generate(prompt, params):
            yield token

    def _messages_to_prompt(self, messages: list[dict]) -> str:
        """Render the conversation.

        With the native engine the model's own chat template is used (Llama 3
        headers, ChatML, Phi-3, ...). The flat "User:/Assistant:" form is only a
        fallback for an HTTP engine that does not expose a template.
        """
        if self._native is not None:
            try:
                return self._native.chat_prompt(messages)
            except Exception as e:  # noqa: BLE001 - fall back, never fail
                logger.warning(f"chat template failed, using plain roles: {e}")
        parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                parts.append(f"System: {content}")
            elif role == "user":
                parts.append(f"User: {content}")
            elif role == "assistant":
                parts.append(f"Assistant: {content}")
            else:
                parts.append(f"{role}: {content}")
        return "\n\n".join(parts) + "\n\nAssistant:"


class SyncFishModelClient:
    """Synchronous adapter for FishModelClient to implement ModelClient protocol.

    This wraps the async client and runs the event loop to provide a synchronous
    generate() method compatible with the existing AgentLoop.
    """

    def __init__(self, config: Optional[FishConfig] = None):
        self._async_client = FishModelClient(config)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._health_at = 0.0
        self._health_ok = False

    @property
    def available(self) -> bool:
        """True only when the engine answers its health endpoint.

        The probe is cached briefly so status polling does not hammer the
        engine, and it never raises: an unreachable engine is unavailable.
        """
        return self.health()

    def health(self, ttl: float = 5.0) -> bool:
        now = time.time()
        if now - self._health_at < ttl:
            return self._health_ok
        ok = False
        native = getattr(self._async_client, "_native", None)
        if native is not None:
            # In-process engine: no socket involved.
            ok = native.health()
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

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        """Get or create an event loop for synchronous calls."""
        if self._loop is None or self._loop.is_closed():
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
        return self._loop

    def generate(self, messages: list[dict]) -> str:
        """Synchronous generate method for ModelClient protocol.

        Converts messages to a prompt and returns the full response.
        """
        # Convert messages to a single prompt string
        prompt = self._messages_to_prompt(messages)

        # Run the async generation in the event loop
        loop = self._get_loop()
        if loop.is_running():
            # If loop is already running (e.g., in async context), we need a different approach
            # Create a new thread with its own loop
            import concurrent.futures

            def run_in_new_loop():
                new_loop = asyncio.new_event_loop()
                try:
                    return new_loop.run_until_complete(
                        self._async_client.generate_full(prompt)
                    )
                finally:
                    new_loop.close()

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(run_in_new_loop)
                return future.result()
        else:
            return loop.run_until_complete(self._async_client.generate_full(prompt))

    async def ahealth(self) -> bool:
        """Async health check."""
        return await self._async_client.health()

    async def astats(self) -> dict:
        """Async stats."""
        return await self._async_client.stats()

    async def generate_stream(
        self,
        messages: list[dict],
        params: Optional[FishParams] = None,
    ) -> AsyncGenerator[str, None]:
        """Async streaming generation for messages format.

        Delegates to the async client's generate_stream method.
        """
        async for token in self._async_client.generate_stream(messages, params):
            yield token

    def _messages_to_prompt(self, messages: list[dict]) -> str:
        """Delegate to the async client so the model's chat template applies."""
        return self._async_client._messages_to_prompt(messages)

    def close(self) -> None:
        """Close the async client."""
        import concurrent.futures

        def close_in_new_loop():
            new_loop = asyncio.new_event_loop()
            try:
                new_loop.run_until_complete(self._async_client.close())
            finally:
                new_loop.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(close_in_new_loop)
            future.result(timeout=10)