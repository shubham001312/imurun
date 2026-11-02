"""The model the server talks to: the local FISH engine, or a hosted model.

A thin facade over ``SyncFishModelClient`` that the server composes. It
exists so the server never imports engine details and so a second compute
path can slot in behind the same methods. That second path is now
``NvidiaClient`` (``model/nvidia_client.py``): when the user selects an
NVIDIA-hosted model, generation, status and the model name route there;
selecting a local model routes back to FISH. ``remote`` tells the agent loop
which kind of model it is (a hosted model always gets the full prompt).
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path


class EngineModelClient:
    """FISH engine, or the selected hosted model. No silent fallback between them."""

    def __init__(self, fish=None, remote=None) -> None:
        self._fish = fish
        self._remote = remote          # NvidiaClient (or None)
        self._use_remote = False

    # ------------------------------------------------------------- hosted path
    @property
    def remote(self) -> bool:
        """True while a hosted model is selected and usable."""
        return bool(self._use_remote and self._remote is not None and self._remote.configured)

    @property
    def remote_client(self):
        return self._remote

    def attach_remote(self, client) -> None:
        self._remote = client

    def select_remote(self, model_id: str) -> bool:
        """Route generation to the hosted model ``model_id``."""
        if self._remote is None or not model_id:
            return False
        self._remote.set_model(model_id)
        self._use_remote = bool(self._remote.configured)
        return self._use_remote

    def use_local(self) -> None:
        self._use_remote = False

    def _active(self):
        return self._remote if self.remote else self._fish

    # ----------------------------------------------------------- capability

    @property
    def fish_available(self) -> bool:
        """True when an engine is configured, even if weights are unloaded.

        Uses ``configured`` (library + model file present) rather than
        ``available`` (weights resident) so that the idle-unload watchdog
        can actually free memory between sessions.
        """
        if self._fish is None:
            return False
        try:
            return bool(self._fish.configured)
        except Exception:  # noqa: BLE001 - an unreachable engine is unavailable
            return False

    @property
    def available(self) -> bool:
        return self.remote or self.fish_available

    @property
    def engine(self) -> str:
        if self.remote:
            return "nvidia"
        return "fish" if self.fish_available else "none"

    @property
    def config(self):
        return getattr(self._fish, "config", None)

    @property
    def loaded(self) -> bool:
        """True when the weights are resident right now (always for hosted)."""
        if self.remote:
            return True
        return bool(getattr(self._fish, "loaded", False))

    def status(self) -> dict:
        if self.remote:
            return {"engine": "nvidia", "available": True, "loaded": True, "remote": True,
                    "model": self._remote.model_info(), "last_generation": self._remote.stats()}
        status = {"engine": self.engine, "available": self.available, "loaded": self.loaded}
        # Only describe the model when it is already resident: status polling
        # must never be what loads a multi-gigabyte file.
        if self.loaded:
            info = self.model_info()
            if info and "error" not in info:
                status["model"] = info
            stats = self.stats()
            if stats and "error" not in stats:
                status["last_generation"] = stats
        return status

    def stats(self) -> dict:
        """Last-generation counters from the active compute path."""
        if self.remote:
            return self._remote.stats()
        if self._fish is not None and self.fish_available:
            getter = getattr(self._fish, "stats", None)
            if callable(getter):
                try:
                    return dict(getter() or {})
                except Exception:  # noqa: BLE001
                    return {}
        return {}

    def model_info(self) -> dict:
        if self.remote:
            return self._remote.model_info()
        getter = getattr(self._fish, "model_info", None)
        if callable(getter):
            try:
                return dict(getter() or {})
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def context_limit(self) -> int:
        if self.remote:
            return self._remote.context_limit()
        getter = getattr(self._fish, "context_limit", None)
        if callable(getter):
            try:
                return int(getter())
            except Exception:  # noqa: BLE001
                pass
        return 4096

    # ------------------------------------------------------------- selection

    def select(self, path: Path | str) -> bool:
        """Switch to a local model on the FISH backend (leaves any hosted model)."""
        self._use_remote = False
        target = Path(path)
        fish_select = getattr(self._fish, "select", None)
        if callable(fish_select):
            try:
                return bool(fish_select(target))
            except Exception:  # noqa: BLE001
                pass
        return False

    def selected_name(self) -> str:
        """The model the active path serves."""
        if self.remote:
            return self._remote.selected_name()
        getter = getattr(self._fish, "selected_name", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:  # noqa: BLE001
                pass
        return ""

    # ------------------------------------------------------------ generation

    def cancel(self) -> None:
        fn = getattr(self._active(), "cancel", None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    async def generate_stream(self, messages: list[dict]) -> AsyncGenerator[str, None]:
        if self.remote:
            async for token in self._remote.generate_stream(messages):
                yield token
            return
        if self._fish is not None and self.fish_available:
            async for token in self._fish.generate_stream(messages):
                yield token
            return
        raise RuntimeError("FISH engine unavailable")

    def generate(self, messages: list[dict]) -> str:
        if self.remote:
            return self._remote.generate(messages)
        if self._fish is not None and self.fish_available:
            return self._fish.generate(messages)
        raise RuntimeError("FISH engine unavailable")

    def close(self) -> None:
        closer = getattr(self._fish, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
