"""Configuration for the IMURUN server and FISH model client."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class FishConfig:
    """Configuration for the FISH model engine."""
    server_url: str = "http://127.0.0.1:8731"
    stream_endpoint: str = "/fish/stream"
    vram_limit_mb: int = 8192
    # 0 = let the engine's specs planner size the context from the measured
    # machine (free RAM, model KV cost). A positive value is a hard budget.
    context_size: int = 0
    temperature: float = 0.35
    top_p: float = 0.9
    max_tokens: int = 2048
    timeout_seconds: float = 60.0
    health_endpoint: str = "/fish/health"
    stats_endpoint: str = "/stats"
    # Native backend: path to a GGUF. Empty means no model configured.
    # The library path comes from FISH_LIBRARY or bin/fish.dll.
    model_path: str = ""
    # Unload model weights after this many seconds of inactivity; 0 keeps the
    # model loaded (default). The old 300 s default forced a full cold load
    # (8B: tens of seconds of read + re-quantisation + upload) every time the
    # owner came back after five minutes, which read as "slow generation".
    idle_timeout_seconds: float = 0.0
    # Speculative decoding: a small model with the same vocabulary that drafts
    # tokens for the main one (FISH_DRAFT_MODEL). Empty = off.
    draft_model_path: str = ""
    draft_k: int = 0
    # Resource budget for the engine (services/resources, docs/resources/README.md).
    # 0 = governed: the resource governor computes them from the measured
    # machine and the `resources` settings headroom at every load. A positive
    # value is a hard override; FISH_THREADS / FISH_RAM_BUDGET_MB /
    # FISH_VRAM_HEADROOM_MB exported in the shell override both.
    threads: int = 0
    ram_budget_mb: int = 0
    vram_headroom_mb: int = 0

    @property
    def stream_url(self) -> str:
        return f"{self.server_url.rstrip('/')}{self.stream_endpoint}"

    @property
    def health_url(self) -> str:
        return f"{self.server_url.rstrip('/')}{self.health_endpoint}"

    @property
    def stats_url(self) -> str:
        return f"{self.server_url.rstrip('/')}{self.stats_endpoint}"


@dataclass
class ServerConfig:
    """Main server configuration."""
    host: str = "127.0.0.1"
    port: int = 8732
    data_root: Optional[Path] = None
    static_root: Optional[Path] = None
    fish: FishConfig = field(default_factory=FishConfig)
    max_body: int = 1_000_000
    version: str = "0.2"

    def __post_init__(self):
        if self.data_root is None:
            self.data_root = Path.home() / ".imurun"


def _last_model_path(data_root: Path) -> Path:
    return data_root / "last_model.json"


def load_last_model(data_root: Path) -> Optional[str]:
    """Load the last used model ID from disk."""
    path = _last_model_path(data_root)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("model_id")
    except Exception:
        pass
    return None


def save_last_model(data_root: Path, model_id: str) -> None:
    """Persist the last used model ID."""
    path = _last_model_path(data_root)
    try:
        path.write_text(json.dumps({"model_id": model_id}), encoding="utf-8")
    except Exception:
        pass


def resolve_model_path(data_root: Path, model_id: Optional[str]) -> Optional[str]:
    """Convert a model ID to a filesystem path using the model manager.

    Falls back to the default 1B model if the ID is unknown or not downloaded.
    """
    if not model_id:
        return None
    try:
        from .model_manager import get_model_manager
        manager = get_model_manager(str(data_root / "models"))
        path = manager.get_model_path(model_id)
        return str(path) if path else None
    except Exception:
        return None


def load_config() -> ServerConfig:
    """Load configuration from environment variables with defaults.

    No model is loaded by default. Model is loaded only when explicitly
    selected via the UI or API.
    """
    import os

    data_root = Path(os.getenv("IMURUN_DATA_ROOT")) if os.getenv("IMURUN_DATA_ROOT") else (Path.home() / ".imurun")

    # Only use explicit FISH_MODEL env var; no auto-resolve defaults
    env_model = os.getenv("FISH_MODEL", "")

    fish = FishConfig(
        server_url=os.getenv("FISH_SERVER_URL", "http://127.0.0.1:8731"),
        stream_endpoint=os.getenv("FISH_STREAM_ENDPOINT", "/fish/stream"),
        vram_limit_mb=int(os.getenv("FISH_VRAM_LIMIT_MB", "8192")),
        context_size=int(os.getenv("FISH_CONTEXT_SIZE", "0")),
        temperature=float(os.getenv("FISH_TEMPERATURE", "0.35")),
        top_p=float(os.getenv("FISH_TOP_P", "0.9")),
        max_tokens=int(os.getenv("FISH_MAX_TOKENS", "2048")),
        timeout_seconds=float(os.getenv("FISH_TIMEOUT_SECONDS", "60.0")),
        health_endpoint=os.getenv("FISH_HEALTH_ENDPOINT", "/fish/health"),
        stats_endpoint=os.getenv("FISH_STATS_ENDPOINT", "/stats"),
        model_path=env_model,
        idle_timeout_seconds=float(os.getenv("FISH_IDLE_TIMEOUT_SECONDS", "0") or 0),
        draft_model_path=os.getenv("FISH_DRAFT_MODEL", ""),
        draft_k=int(os.getenv("FISH_DRAFT_K", "0") or 0),
    )

    return ServerConfig(
        host=os.getenv("IMURUN_HOST", "127.0.0.1"),
        port=int(os.getenv("IMURUN_PORT", "8732")),
        data_root=data_root,
        static_root=Path(os.getenv("IMURUN_STATIC_ROOT")) if os.getenv("IMURUN_STATIC_ROOT") else None,
        fish=fish,
        max_body=int(os.getenv("IMURUN_MAX_BODY", "1000000")),
        version=os.getenv("IMURUN_VERSION", "0.2"),
    )