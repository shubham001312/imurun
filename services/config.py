"""Configuration for the IMURUN server and FISH model client."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class FishConfig:
    """Configuration for the FISH model engine."""
    server_url: str = "http://127.0.0.1:8731"
    stream_endpoint: str = "/fish/stream"
    vram_limit_mb: int = 8192
    context_size: int = 4096
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 2048
    timeout_seconds: float = 60.0
    health_endpoint: str = "/fish/health"
    stats_endpoint: str = "/stats"
    # Native backend: path to a GGUF. Empty means "auto-detect models/*.gguf".
    # The library path comes from FISH_LIBRARY or bin/fish.dll.
    model_path: str = ""

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


def load_config() -> ServerConfig:
    """Load configuration from environment variables with defaults."""
    import os

    fish = FishConfig(
        server_url=os.getenv("FISH_SERVER_URL", "http://127.0.0.1:8731"),
        stream_endpoint=os.getenv("FISH_STREAM_ENDPOINT", "/fish/stream"),
        vram_limit_mb=int(os.getenv("FISH_VRAM_LIMIT_MB", "8192")),
        context_size=int(os.getenv("FISH_CONTEXT_SIZE", "4096")),
        temperature=float(os.getenv("FISH_TEMPERATURE", "0.7")),
        top_p=float(os.getenv("FISH_TOP_P", "0.9")),
        max_tokens=int(os.getenv("FISH_MAX_TOKENS", "2048")),
        timeout_seconds=float(os.getenv("FISH_TIMEOUT_SECONDS", "60.0")),
        health_endpoint=os.getenv("FISH_HEALTH_ENDPOINT", "/fish/health"),
        stats_endpoint=os.getenv("FISH_STATS_ENDPOINT", "/stats"),
    )

    return ServerConfig(
        host=os.getenv("IMURUN_HOST", "127.0.0.1"),
        port=int(os.getenv("IMURUN_PORT", "8732")),
        data_root=Path(os.getenv("IMURUN_DATA_ROOT")) if os.getenv("IMURUN_DATA_ROOT") else None,
        static_root=Path(os.getenv("IMURUN_STATIC_ROOT")) if os.getenv("IMURUN_STATIC_ROOT") else None,
        fish=fish,
        max_body=int(os.getenv("IMURUN_MAX_BODY", "1000000")),
        version=os.getenv("IMURUN_VERSION", "0.2"),
    )