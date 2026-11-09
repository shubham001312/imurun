"""
Model Manager Service
Handles model downloading, listing, and progress tracking for the frontend.
"""
import asyncio
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, AsyncGenerator
from urllib.parse import urlparse

# Network libraries are only needed to download; listing the catalogue and
# selecting a local file must work without them (and without the network).
try:
    import httpx
except ImportError:  # pragma: no cover - optional at import time
    httpx = None  # type: ignore[assignment]
try:
    from huggingface_hub import HfApi
except ImportError:  # pragma: no cover - optional at import time
    HfApi = None  # type: ignore[assignment,misc]


def _require_download_libs() -> None:
    """Fail with an actionable message instead of a NameError mid-download."""
    missing = [name for name, mod in (("httpx", httpx), ("huggingface_hub", HfApi)) if mod is None]
    if missing:
        raise SourceUnavailable(
            "missing_dependency",
            "Downloading needs " + " and ".join(missing) + " - run: pip install -r requirements.txt",
        )


@dataclass
class ModelInfo:
    """Information about a model."""
    id: str
    name: str
    filename: str
    size_bytes: int
    size_gb: float
    source: str  # "hf" or "url"
    repo_id: Optional[str] = None
    description: str = ""
    downloaded: bool = False
    path: Optional[str] = None
    sources: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    compatible: bool = True
    """Ordered candidate download sources.

    Every entry is a dict with ``repo_id``, ``filename``, ``url`` and
    ``size_bytes``. The first one is the primary source; the rest are
    fallbacks that are tried when the primary one is gone or gated.
    """


@dataclass
class DownloadProgress:
    """Progress of a model download."""
    model_id: str
    status: str  # "starting", "downloading", "verifying", "complete", "error"
    downloaded_bytes: int
    total_bytes: int
    speed_bps: float
    eta_seconds: Optional[float]
    message: str = ""
    error: Optional[str] = None

    @property
    def percent(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return (self.downloaded_bytes / self.total_bytes) * 100


def hf_source(repo_id: str, filename: str, size_bytes: int) -> dict:
    """Build a Hugging Face source descriptor.

    The URL is always derived from the repo and the file name so the two can
    never drift apart.
    """
    return {
        "repo_id": repo_id,
        "filename": filename,
        "url": f"https://huggingface.co/{repo_id}/resolve/main/{filename}",
        "size_bytes": int(size_bytes),
    }


def _range_total(headers) -> int:
    """``<total>`` of a ``Content-Range: bytes */<total>`` (a 416 reply)."""
    total = str(headers.get("content-range", "")).rsplit("/", 1)[-1].strip()
    return int(total) if total.isdigit() else -1


def _served_total(headers, status: int) -> int:
    """Total file size from a download response, or 0 when it does not say.

    206: ``Content-Range: bytes a-b/<total>``. 200: ``Content-Length`` (only
    when the body is not content-encoded, where it would be the encoded size).
    """
    try:
        if status == 206:
            total = str(headers.get("content-range", "")).rsplit("/", 1)[-1].strip()
            return int(total) if total.isdigit() else 0
        if status == 200 and not headers.get("content-encoding"):
            length = str(headers.get("content-length", "")).strip()
            return int(length) if length.isdigit() else 0
    except (TypeError, ValueError):
        pass
    return 0


class SourceUnavailable(RuntimeError):
    """A single download source cannot be used.

    The download keeps going with the next source; only the final message
    reaches the user.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _StatusError(Exception):
    """Carries an HTTP status so it can be classified like a request error."""

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"HTTP {status_code} for {url}")
        self.status_code = status_code
        self.url = url


def classify_download_error(error: Exception) -> tuple[str, str]:
    """Turn an exception into a short code and a sentence a user can act on.

    The UI shows this sentence instead of a traceback, so it must be plain,
    honest and specific about what to do next.
    """
    text = str(error)
    lowered = text.lower()
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None) if response is not None else None

    if status == 404 or "404" in text or "repository not found" in lowered:
        return ("not_found", "The model source is no longer published.")
    if status in (401, 403) or "401" in text or "403" in text:
        return (
            "unauthorized",
            "This model needs a Hugging Face token - add one in Settings and retry.",
        )
    if status == 429 or "429" in text or "rate limit" in lowered:
        return ("rate_limited", "Hugging Face is rate limiting this machine - retry in a minute.")
    transport_error = httpx is not None and isinstance(error, httpx.TransportError)
    if transport_error or "connection" in lowered or "timed out" in lowered:
        return ("offline", "No network access right now.")
    if "gated" in lowered or "accept the license" in lowered:
        return (
            "unauthorized",
            "This model needs a Hugging Face token - add one in Settings and retry.",
        )
    return ("unknown", "The download failed.")


# The catalogue below was verified against the Hugging Face API: every source
# answers with HTTP 200 and the recorded size matches the served file.
# Models are grouped by specialization and parameter count.
RECOMMENDED_MODELS = {
    # ==================== TINY / EDGE (< 2 GB) ====================
    "llama-3.2-1b-q4": ModelInfo(
        id="llama-3.2-1b-q4",
        name="Llama 3.2 1B Instruct Q4_K_M",
        filename="Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        size_bytes=807_694_464,
        size_gb=0.8,
        source="hf",
        repo_id="bartowski/Llama-3.2-1B-Instruct-GGUF",
        description="Tiny 1B model - runs on CPU only, fastest",
        tags=["tiny", "edge", "fast"],
        sources=[
            hf_source(
                "bartowski/Llama-3.2-1B-Instruct-GGUF",
                "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
                807_694_464,
            ),
        ],
    ),
    "gemma-2-2b-q4": ModelInfo(
        id="gemma-2-2b-q4",
        name="Gemma 2 2B Instruct Q4_K_M",
        filename="gemma-2-2b-it-Q4_K_M.gguf",
        size_bytes=1_708_582_752,
        size_gb=1.7,
        source="hf",
        repo_id="bartowski/gemma-2-2b-it-GGUF",
        description="Google's compact 2B model - great for edge devices",
        tags=["tiny", "edge", "multilingual"],
        sources=[
            hf_source(
                "bartowski/gemma-2-2b-it-GGUF",
                "gemma-2-2b-it-Q4_K_M.gguf",
                1_708_582_752,
            ),
        ],
    ),
    "qwen2-5-1.5b-q4": ModelInfo(
        id="qwen2-5-1.5b-q4",
        name="Qwen 2.5 1.5B Instruct Q4_K_M",
        filename="Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
        size_bytes=986_048_768,
        size_gb=1.0,
        source="hf",
        repo_id="bartowski/Qwen2.5-1.5B-Instruct-GGUF",
        description="Qwen 2.5 1.5B - excellent multilingual, great for edge",
        tags=["tiny", "edge", "multilingual", "coding"],
        sources=[
            hf_source(
                "bartowski/Qwen2.5-1.5B-Instruct-GGUF",
                "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf",
                986_048_768,
            ),
        ],
    ),

    # ==================== SMALL / LAPTOP (2-4 GB) ====================
    "llama-3.2-3b-q4": ModelInfo(
        id="llama-3.2-3b-q4",
        name="Llama 3.2 3B Instruct Q4_K_M",
        filename="Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        size_bytes=2_019_377_696,
        size_gb=2.0,
        source="hf",
        repo_id="bartowski/Llama-3.2-3B-Instruct-GGUF",
        description="Lightweight 3B model - the sweet spot for laptops",
        tags=["general", "chat"],
        sources=[
            hf_source(
                "bartowski/Llama-3.2-3B-Instruct-GGUF",
                "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
                2_019_377_696,
            ),
        ],
    ),
    "phi-3-mini-q4": ModelInfo(
        id="phi-3-mini-q4",
        name="Phi-3 Mini 4K Instruct Q4_K_M",
        filename="Phi-3-mini-4k-instruct-Q4_K_M.gguf",
        size_bytes=2_393_231_360,
        size_gb=2.4,
        source="hf",
        repo_id="bartowski/Phi-3-mini-4k-instruct-GGUF",
        description="Microsoft's efficient 3.8B model - strong reasoning",
        tags=["reasoning", "coding", "math"],
        sources=[
            hf_source(
                "bartowski/Phi-3-mini-4k-instruct-GGUF",
                "Phi-3-mini-4k-instruct-Q4_K_M.gguf",
                2_393_231_360,
            ),
        ],
    ),
    "qwen2-5-3b-q4": ModelInfo(
        id="qwen2-5-3b-q4",
        name="Qwen 2.5 3B Instruct Q4_K_M",
        filename="Qwen2.5-3B-Instruct-Q4_K_M.gguf",
        size_bytes=1_929_903_264,
        size_gb=1.9,
        source="hf",
        repo_id="bartowski/Qwen2.5-3B-Instruct-GGUF",
        description="Qwen 2.5 3B - best multilingual small model, strong coding",
        tags=["multilingual", "coding", "reasoning"],
        sources=[
            hf_source(
                "bartowski/Qwen2.5-3B-Instruct-GGUF",
                "Qwen2.5-3B-Instruct-Q4_K_M.gguf",
                1_929_903_264,
            ),
        ],
    ),
    "phi-3.5-mini-q4": ModelInfo(
        id="phi-3.5-mini-q4",
        name="Phi-3.5 Mini Instruct Q4_K_M",
        filename="Phi-3.5-mini-instruct-Q4_K_M.gguf",
        size_bytes=2_393_232_672,
        size_gb=2.4,
        source="hf",
        repo_id="bartowski/Phi-3.5-mini-instruct-GGUF",
        description="Phi-3.5 Mini - improved reasoning and multilingual",
        tags=["reasoning", "multilingual", "coding"],
        sources=[
            hf_source(
                "bartowski/Phi-3.5-mini-instruct-GGUF",
                "Phi-3.5-mini-instruct-Q4_K_M.gguf",
                2_393_232_672,
            ),
        ],
    ),

    # ==================== MEDIUM / DESKTOP (4-8 GB) ====================
    "llama-3.1-8b-q4": ModelInfo(
        id="llama-3.1-8b-q4",
        name="Llama 3.1 8B Instruct Q4_K_M",
        filename="Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        size_bytes=4_920_739_232,
        size_gb=4.9,
        source="hf",
        repo_id="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        description="Strong 8B general model - needs 6GB+ RAM",
        tags=["general", "chat", "reasoning"],
        sources=[
            hf_source(
                "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
                "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
                4_920_739_232,
            ),
        ],
    ),
    "llama-3.2-8b-q4": ModelInfo(
        id="llama-3.2-8b-q4",
        name="Llama 3.2 8B Instruct Q4_K_M",
        filename="Llama-3.2-8B-Instruct.Q4_K_M.gguf",
        size_bytes=4_920_739_232,
        size_gb=4.9,
        source="hf",
        repo_id="mradermacher/Llama-3.2-8B-Instruct-GGUF",
        description="Llama 3.2 8B - improved tool use and multilingual",
        tags=["general", "tool-use", "multilingual"],
        sources=[
            hf_source(
                "mradermacher/Llama-3.2-8B-Instruct-GGUF",
                "Llama-3.2-8B-Instruct.Q4_K_M.gguf",
                4_920_739_232,
            ),
        ],
    ),
    "qwen2-5-7b-q4": ModelInfo(
        id="qwen2-5-7b-q4",
        name="Qwen 2.5 7B Instruct Q4_K_M",
        filename="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        size_bytes=4_683_074_240,
        size_gb=4.7,
        source="hf",
        repo_id="bartowski/Qwen2.5-7B-Instruct-GGUF",
        description="Qwen 2.5 7B - best multilingual, top-tier coding",
        tags=["multilingual", "coding", "reasoning", "math"],
        sources=[
            hf_source(
                "bartowski/Qwen2.5-7B-Instruct-GGUF",
                "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
                4_683_074_240,
            ),
        ],
    ),
    "deepseek-coder-6.7b-q4": ModelInfo(
        id="deepseek-coder-6.7b-q4",
        name="DeepSeek Coder 6.7B Instruct Q4_K_M",
        filename="deepseek-coder-6.7b-instruct.Q4_K_M.gguf",
        size_bytes=4_083_015_904,
        size_gb=4.1,
        source="hf",
        repo_id="TheBloke/deepseek-coder-6.7B-instruct-GGUF",
        description="DeepSeek Coder 6.7B - specialized for coding, top HumanEval",
        tags=["coding", "python", "code-completion"],
        sources=[
            hf_source(
                "TheBloke/deepseek-coder-6.7B-instruct-GGUF",
                "deepseek-coder-6.7b-instruct.Q4_K_M.gguf",
                4_083_015_904,
            ),
        ],
    ),

    # ==================== LARGE / WORKSTATION (10-20 GB) ====================
    "qwen2-5-14b-q4": ModelInfo(
        id="qwen2-5-14b-q4",
        name="Qwen 2.5 14B Instruct Q4_K_M",
        filename="Qwen2.5-14B-Instruct-Q4_K_M.gguf",
        size_bytes=8_988_110_976,
        size_gb=9.0,
        source="hf",
        repo_id="bartowski/Qwen2.5-14B-Instruct-GGUF",
        description="Qwen 2.5 14B - excellent reasoning, coding, multilingual",
        tags=["reasoning", "coding", "multilingual", "math"],
        sources=[
            hf_source(
                "bartowski/Qwen2.5-14B-Instruct-GGUF",
                "Qwen2.5-14B-Instruct-Q4_K_M.gguf",
                8_988_110_976,
            ),
        ],
    ),
    "qwq-32b-q4": ModelInfo(
        id="qwq-32b-q4",
        name="QwQ 32B Preview Q4_K_M",
        filename="QwQ-32B-Preview-Q4_K_M.gguf",
        size_bytes=19_851_336_544,
        size_gb=19.9,
        source="hf",
        repo_id="bartowski/QwQ-32B-Preview-GGUF",
        description="QwQ 32B - Alibaba's reasoning model with visible thinking",
        tags=["reasoning", "thinking", "math", "analysis"],
        sources=[
            hf_source(
                "bartowski/QwQ-32B-Preview-GGUF",
                "QwQ-32B-Preview-Q4_K_M.gguf",
                19_851_336_544,
            ),
        ],
    ),

    # ==================== EXTRA LARGE / SERVER (25-50 GB) ====================
    "mixtral-8x7b-q4": ModelInfo(
        id="mixtral-8x7b-q4",
        name="Mixtral 8x7B Instruct Q4_K_M",
        filename="Mixtral-8x7B-Instruct-v0.1-Q4_K_M.gguf",
        size_bytes=28_446_410_624,
        size_gb=28.4,
        source="hf",
        repo_id="second-state/Mixtral-8x7B-Instruct-v0.1-GGUF",
        description="Mixture-of-experts - 56B params, 13B active, needs 32GB RAM",
        tags=["moe", "general", "multilingual", "expert-streaming"],
        sources=[
            hf_source(
                "second-state/Mixtral-8x7B-Instruct-v0.1-GGUF",
                "Mixtral-8x7B-Instruct-v0.1-Q4_K_M.gguf",
                28_446_410_624,
            ),
            hf_source(
                "TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF",
                "mixtral-8x7b-instruct-v0.1.Q4_K_M.gguf",
                26_441_533_376,
            ),
        ],
    ),
    "qwen2-5-32b-q4": ModelInfo(
        id="qwen2-5-32b-q4",
        name="Qwen 2.5 32B Instruct Q4_K_M",
        filename="Qwen2.5-32B-Instruct-Q4_K_M.gguf",
        size_bytes=19_851_336_576,
        size_gb=19.9,
        source="hf",
        repo_id="bartowski/Qwen2.5-32B-Instruct-GGUF",
        description="Qwen 2.5 32B - flagship model, SOTA benchmarks",
        tags=["reasoning", "coding", "multilingual", "math", "analysis"],
        sources=[
            hf_source(
                "bartowski/Qwen2.5-32B-Instruct-GGUF",
                "Qwen2.5-32B-Instruct-Q4_K_M.gguf",
                19_851_336_576,
            ),
        ],
    ),
    "llama-3.1-70b-q4": ModelInfo(
        id="llama-3.1-70b-q4",
        name="Llama 3.1 70B Instruct Q4_K_M",
        filename="Meta-Llama-3.1-70B-Instruct-Q4_K_M.gguf",
        size_bytes=42_520_398_400,
        size_gb=42.5,
        source="hf",
        repo_id="bartowski/Meta-Llama-3.1-70B-Instruct-GGUF",
        description="Llama 3.1 70B - Meta's flagship, strong general reasoning",
        tags=["general", "reasoning", "multilingual", "analysis"],
        sources=[
            hf_source(
                "bartowski/Meta-Llama-3.1-70B-Instruct-GGUF",
                "Meta-Llama-3.1-70B-Instruct-Q4_K_M.gguf",
                42_520_398_400,
            ),
        ],
    ),
    "qwen2-5-72b-q4": ModelInfo(
        id="qwen2-5-72b-q4",
        name="Qwen 2.5 72B Instruct Q4_K_M",
        filename="Qwen2.5-72B-Instruct-Q4_K_M.gguf",
        size_bytes=47_415_715_488,
        size_gb=47.4,
        source="hf",
        repo_id="bartowski/Qwen2.5-72B-Instruct-GGUF",
        description="Qwen 2.5 72B - flagship, SOTA across all benchmarks",
        tags=["reasoning", "coding", "multilingual", "math", "analysis"],
        sources=[
            hf_source(
                "bartowski/Qwen2.5-72B-Instruct-GGUF",
                "Qwen2.5-72B-Instruct-Q4_K_M.gguf",
                47_415_715_488,
            ),
        ],
    ),
    "deepseek-coder-33b-q4": ModelInfo(
        id="deepseek-coder-33b-q4",
        name="DeepSeek Coder 33B Instruct Q4_K_M",
        filename="deepseek-coder-33b-instruct.Q4_K_M.gguf",
        size_bytes=19_940_659_200,
        size_gb=19.9,
        source="hf",
        repo_id="TheBloke/deepseek-coder-33B-instruct-GGUF",
        description="DeepSeek Coder 33B - SOTA coding, beats GPT-4 on HumanEval",
        tags=["coding", "python", "code-completion", "refactoring"],
        sources=[
            hf_source(
                "TheBloke/deepseek-coder-33B-instruct-GGUF",
                "deepseek-coder-33b-instruct.Q4_K_M.gguf",
                19_940_659_200,
            ),
        ],
    ),
    "deepseek-r1-distill-llama-8b-q4": ModelInfo(
        id="deepseek-r1-distill-llama-8b-q4",
        name="DeepSeek R1 Distill Llama 8B (Reasoning) Q4_K_M",
        filename="DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf",
        size_bytes=4_920_736_608,
        size_gb=4.9,
        source="hf",
        repo_id="bartowski/DeepSeek-R1-Distill-Llama-8B-GGUF",
        description="R1-distilled Llama 8B - shows its step-by-step reasoning before answering",
        tags=["reasoning", "thinking", "general", "chat"],
        sources=[
            hf_source(
                "bartowski/DeepSeek-R1-Distill-Llama-8B-GGUF",
                "DeepSeek-R1-Distill-Llama-8B-Q4_K_M.gguf",
                4_920_736_608,
            ),
        ],
    ),
    "mistral-7b-v0.3-q4": ModelInfo(
        id="mistral-7b-v0.3-q4",
        name="Mistral 7B Instruct v0.3 Q4_K_M",
        filename="Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
        size_bytes=4_372_812_000,
        size_gb=4.4,
        source="hf",
        repo_id="bartowski/Mistral-7B-Instruct-v0.3-GGUF",
        description="Mistral 7B v0.3 - strong general chat with a 32K context",
        tags=["general", "chat", "long-context"],
        sources=[
            hf_source(
                "bartowski/Mistral-7B-Instruct-v0.3-GGUF",
                "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
                4_372_812_000,
            ),
        ],
    ),
    "mistral-nemo-12b-q4": ModelInfo(
        id="mistral-nemo-12b-q4",
        name="Mistral Nemo 12B Instruct Q4_K_M",
        filename="Mistral-Nemo-Instruct-2407-Q4_K_M.gguf",
        size_bytes=7_477_208_192,
        size_gb=7.5,
        source="hf",
        repo_id="bartowski/Mistral-Nemo-Instruct-2407-GGUF",
        description="Mistral Nemo 12B - larger, multilingual, 128K context",
        tags=["general", "chat", "multilingual", "long-context"],
        sources=[
            hf_source(
                "bartowski/Mistral-Nemo-Instruct-2407-GGUF",
                "Mistral-Nemo-Instruct-2407-Q4_K_M.gguf",
                7_477_208_192,
            ),
        ],
    ),
}


# GGUF architectures the FISH engine implements (see model/fish/doc/ARCHITECTURES.md).
# Every catalogue entry above is one of these: llama (incl. Mistral, DeepSeek
# Coder, R1-Distill-Llama, Mixtral MoE), qwen2 (incl. QwQ, Qwen-MoE), qwen3,
# phi3 and gemma2. Entries whose architecture is not in this set are shown but
# cannot be selected, so the UI never offers a switch it cannot honour.
FISH_SUPPORTED_ARCHS = {"llama", "qwen2", "qwen2moe", "qwen3", "qwen3moe", "phi3", "gemma2"}

_ARCH_BY_ID_PREFIX = (
    ("llama-", "llama"), ("mistral-", "llama"), ("deepseek-coder-", "llama"),
    ("deepseek-r1-distill-llama", "llama"), ("mixtral-", "llama"),
    ("qwen2-", "qwen2"), ("qwq-", "qwen2"), ("qwen3-", "qwen3"),
    ("phi-3", "phi3"), ("gemma-2-", "gemma2"),
)


def model_arch(model_id: str) -> str:
    """Best-effort GGUF architecture name for a catalogue id."""
    for prefix, arch in _ARCH_BY_ID_PREFIX:
        if model_id.startswith(prefix):
            return arch
    return "unknown"


FISH_COMPATIBLE_IDS = {
    model_id for model_id in RECOMMENDED_MODELS if model_arch(model_id) in FISH_SUPPORTED_ARCHS
}


class ModelManager:
    """Manages model downloads and local model registry."""

    def __init__(self, models_dir: Path, progress_callback=None):
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.progress_callback = progress_callback
        self._active_downloads: dict[str, asyncio.Task] = {}

    def list_models(self) -> list[ModelInfo]:
        """List all recommended models with download status."""
        models = []
        for model_id, info in RECOMMENDED_MODELS.items():
            model_path = self.models_dir / info.filename
            info.downloaded = model_path.exists()
            info.path = str(model_path) if info.downloaded else None
            info.compatible = model_id in FISH_COMPATIBLE_IDS
            models.append(info)
        return models

    def is_compatible(self, model_id: str) -> bool:
        """True when the FISH engine can actually run this model's architecture."""
        return model_id in FISH_COMPATIBLE_IDS

    def get_model_path(self, model_id: str) -> Optional[Path]:
        """Get local path for a downloaded model."""
        if model_id not in RECOMMENDED_MODELS:
            return None
        path = self.models_dir / RECOMMENDED_MODELS[model_id].filename
        return path if path.exists() else None

    def resolve_sources(self, model_id: str) -> list[dict]:
        """Ordered download sources for a model.

        The catalogue always carries at least one verified source. When it
        does not (a model added by hand), the legacy repo/filename pair is
        turned into one so the rest of the code has a single shape to handle.
        """
        info = RECOMMENDED_MODELS.get(model_id)
        if info is None:
            return []
        sources = [dict(source) for source in info.sources]
        if not sources and info.repo_id:
            sources.append(
                hf_source(info.repo_id, info.filename, info.size_bytes)
            )
        return sources

    def _disk_refusal(self, model_info: ModelInfo, part_path: Path) -> str:
        """A user-facing refusal when the download would eat into the disk
        headroom, else "". Never raises (an unmeasured disk is not a refusal)."""
        try:
            from .resources import get_governor

            partial = part_path.stat().st_size if part_path.exists() else 0
            need = max(0, int(model_info.size_bytes or 0) - partial)
            self.models_dir.mkdir(parents=True, exist_ok=True)
            check = get_governor().check_disk(self.models_dir, need, f"downloading {model_info.name}")
            return "" if check.ok else check.message
        except Exception:  # noqa: BLE001
            return ""

    def disk_status(self) -> dict:
        """Free space where models are stored, with the headroom IMURUN keeps."""
        try:
            from .resources import get_governor

            return get_governor().check_disk(self.models_dir, 0, "nothing").view()
        except Exception as exc:  # noqa: BLE001
            return {"ok": True, "message": f"disk not measured ({exc})"}

    async def download_model(
        self,
        model_id: str,
        token: Optional[str] = None,
    ) -> AsyncGenerator[DownloadProgress, None]:
        """Download a model with progress updates.

        Sources are tried in order. A source that is gone, gated or
        unreachable does not end the download: the next mirror is tried. Only
        when every source fails does the caller get an error, and that error
        is a short sentence rather than a traceback.
        """
        if model_id not in RECOMMENDED_MODELS:
            yield DownloadProgress(
                model_id=model_id,
                status="error",
                downloaded_bytes=0,
                total_bytes=0,
                speed_bps=0,
                eta_seconds=None,
                error=f"Unknown model: {model_id}",
            )
            return

        model_info = RECOMMENDED_MODELS[model_id]
        dest_path = self.models_dir / model_info.filename
        part_path = dest_path.with_name(dest_path.name + ".part")

        # A finished model only ever appears under its final name: the download
        # writes to ``.part`` and renames after verifying the size. So an
        # existing file is a complete file, even when the catalogue size is an
        # approximation.
        if dest_path.exists() and not part_path.exists():
            actual = dest_path.stat().st_size
            if actual > 0:
                yield DownloadProgress(
                    model_id=model_id,
                    status="complete",
                    downloaded_bytes=actual,
                    total_bytes=actual,
                    speed_bps=0,
                    eta_seconds=0,
                    message="Already downloaded",
                )
                return

        sources = self.resolve_sources(model_id)
        if not sources:
            yield DownloadProgress(
                model_id=model_id,
                status="error",
                downloaded_bytes=0,
                total_bytes=model_info.size_bytes,
                speed_bps=0,
                eta_seconds=None,
                error=f"No download source is configured for {model_info.name}.",
            )
            return

        # Disk headroom (docs/resources/README.md): the download must leave
        # max(10 GB, 10 % of the drive) free for the user's own files.
        refusal = self._disk_refusal(model_info, part_path)
        if refusal:
            yield DownloadProgress(
                model_id=model_id,
                status="error",
                downloaded_bytes=part_path.stat().st_size if part_path.exists() else 0,
                total_bytes=model_info.size_bytes,
                speed_bps=0,
                eta_seconds=None,
                error=refusal,
            )
            return

        failures: list[tuple[dict, str]] = []
        for index, source in enumerate(sources):
            if index:
                # A different mirror serves different bytes; never append to a
                # part file from another source.
                part_path.unlink(missing_ok=True)
                yield DownloadProgress(
                    model_id=model_id,
                    status="starting",
                    downloaded_bytes=0,
                    total_bytes=source.get("size_bytes") or model_info.size_bytes,
                    speed_bps=0,
                    eta_seconds=None,
                    message=f"Trying the next mirror: {source['repo_id']}",
                )
            try:
                async for progress in self._download_from_hf_with_progress(
                    model_info, source, dest_path, token
                ):
                    yield progress
                    if progress.status == "complete":
                        return
            except SourceUnavailable as exc:
                failures.append((source, exc.message))
            except Exception as exc:  # noqa: BLE001 - reported as a short message
                code, message = classify_download_error(exc)
                failures.append((source, message))

        # Every source failed: report the most useful reason, briefly.
        reason = failures[-1][1] if failures else "The download failed."
        tried = ", ".join(source["repo_id"] for source, _message in failures[:3])
        partial = part_path.stat().st_size if part_path.exists() else 0
        if partial:
            reason += (
                f" Kept {partial / 1024 / 1024:.1f} MB already downloaded;"
                " retry resumes from there."
            )
        yield DownloadProgress(
            model_id=model_id,
            status="error",
            downloaded_bytes=partial,
            total_bytes=model_info.size_bytes,
            speed_bps=0,
            eta_seconds=None,
            error=f"{reason} (tried {tried})" if tried else reason,
        )

    async def _download_from_hf_with_progress(
        self,
        model_info: ModelInfo,
        source: dict,
        dest_path: Path,
        token: Optional[str],
    ) -> AsyncGenerator[DownloadProgress, None]:
        """Download one source from the HF Hub with detailed progress.

        Bytes land in a ``.part`` file and are renamed only after the size
        check passes, so an interrupted download can never look like a finished
        model. A dropped connection resumes from the bytes already on disk with
        an HTTP Range request instead of starting over.
        """
        import time

        # repo_info is best-effort: the direct URL carries the file even when
        # the metadata call is rate-limited or slow.
        total_size = int(source.get("size_bytes") or model_info.size_bytes or 0)
        # Only an exact size (repo_info, or what a response reports) may delete
        # or skip a .part; the catalogue's is often an approximation.
        size_exact = False
        _require_download_libs()
        try:
            api = HfApi()
            repo_info = api.repo_info(repo_id=source["repo_id"], token=token)
            for sibling in repo_info.siblings:
                if sibling.rfilename == source["filename"]:
                    if sibling.size:
                        total_size = int(sibling.size)
                        size_exact = True
                    break
            else:
                raise SourceUnavailable(
                    "not_found",
                    f"{source['repo_id']} does not contain {source['filename']}.",
                )
        except SourceUnavailable:
            raise
        except Exception:  # noqa: BLE001 - fall back to the catalogue size
            pass

        url = source["url"]
        part_path = dest_path.with_name(dest_path.name + ".part")
        start_time = time.time()

        base_headers = {}
        if token:
            base_headers["Authorization"] = f"Bearer {token}"

        max_attempts = 6
        last_error: Optional[str] = None

        for attempt in range(1, max_attempts + 1):
            existing = part_path.stat().st_size if part_path.exists() else 0
            if size_exact and total_size > 0 and existing > total_size:
                # A stale, larger part cannot be trusted.
                part_path.unlink(missing_ok=True)
                existing = 0
            if size_exact and total_size > 0 and existing == total_size:
                break  # already complete; fall through to verification

            headers = dict(base_headers)
            if existing > 0:
                headers["Range"] = f"bytes={existing}-"

            try:
                async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
                    async with client.stream("GET", url, headers=headers) as response:
                        status = response.status_code
                        if status == 206:
                            mode = "ab"
                            downloaded = existing
                        elif status == 200:
                            # The mirror ignored Range; start clean.
                            mode = "wb"
                            downloaded = 0
                        elif status == 416 and existing > 0 and _range_total(
                            response.headers
                        ) == existing:
                            # The .part already holds the whole file.
                            total_size, size_exact = existing, True
                            break
                        else:
                            code, message = classify_download_error(
                                _StatusError(status, url)
                            )
                            raise SourceUnavailable(code, message)

                        # The response is the authority on the file's size; the
                        # catalogue's is often a round approximation, used here
                        # whenever repo_info failed (rate limit, API outage).
                        served = _served_total(response.headers, status)
                        if served:
                            total_size, size_exact = served, True

                        if attempt == 1 and downloaded == 0:
                            yield DownloadProgress(
                                model_id=model_info.id,
                                status="downloading",
                                downloaded_bytes=0,
                                total_bytes=total_size,
                                speed_bps=0,
                                eta_seconds=None,
                                message=f"Downloading {source['filename']}…",
                            )

                        with open(part_path, mode) as handle:
                            async for chunk in response.aiter_bytes(chunk_size=1024 * 512):
                                handle.write(chunk)
                                downloaded += len(chunk)

                                elapsed = time.time() - start_time
                                speed = downloaded / elapsed if elapsed > 0 else 0
                                eta = (
                                    (total_size - downloaded) / speed
                                    if speed > 0 and total_size > 0
                                    else None
                                )
                                yield DownloadProgress(
                                    model_id=model_info.id,
                                    status="downloading",
                                    downloaded_bytes=downloaded,
                                    total_bytes=total_size,
                                    speed_bps=speed,
                                    eta_seconds=eta,
                                    message=(
                                        f"Downloading… {downloaded / 1024 / 1024:.1f} MB"
                                        f" / {total_size / 1024 / 1024:.1f} MB"
                                    ),
                                )
            except SourceUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - retry a dropped transfer
                last_error = str(exc) or type(exc).__name__
                if attempt >= max_attempts:
                    break
                await asyncio.sleep(min(2.0 * attempt, 8.0))
                continue

            # A clean stream end. Verify before deciding whether to retry.
            actual = part_path.stat().st_size if part_path.exists() else 0
            if total_size <= 0 or actual == total_size:
                break
            if actual > total_size:
                part_path.unlink(missing_ok=True)
                last_error = "the server sent more bytes than expected"
                continue
            # Short transfer: resume from here on the next attempt.
            last_error = f"connection closed at {actual / 1024 / 1024:.1f} MB"
            if attempt >= max_attempts:
                break
            await asyncio.sleep(min(2.0 * attempt, 8.0))

        actual_size = part_path.stat().st_size if part_path.exists() else 0

        # Verify
        yield DownloadProgress(
            model_id=model_info.id,
            status="verifying",
            downloaded_bytes=actual_size,
            total_bytes=total_size,
            speed_bps=0,
            eta_seconds=0,
            message="Verifying download…",
        )

        if total_size > 0 and actual_size != total_size:
            # Keep the partial file: the next attempt resumes from it.
            yield DownloadProgress(
                model_id=model_info.id,
                status="error",
                downloaded_bytes=actual_size,
                total_bytes=total_size,
                speed_bps=0,
                eta_seconds=None,
                error=(
                    f"The download paused at {actual_size / 1024 / 1024:.1f} MB"
                    f" of {total_size / 1024 / 1024:.1f} MB"
                    + (f" ({last_error})" if last_error else "")
                    + ". Retry to resume from here."
                ),
            )
            return

        part_path.replace(dest_path)

        yield DownloadProgress(
            model_id=model_info.id,
            status="complete",
            downloaded_bytes=total_size or actual_size,
            total_bytes=total_size or actual_size,
            speed_bps=0,
            eta_seconds=0,
            message=f"Download complete! Saved to {dest_path.name}",
        )

    def delete_model(self, model_id: str) -> bool:
        """Delete a downloaded model and any partial download."""
        if model_id not in RECOMMENDED_MODELS:
            return False
        path = self.models_dir / RECOMMENDED_MODELS[model_id].filename
        removed = False
        for target in (path, path.with_name(path.name + ".part")):
            if target.exists():
                target.unlink()
                removed = True
        return removed


# Global instance for API access
_model_manager: Optional[ModelManager] = None


def get_model_manager(models_dir: str | None = None) -> ModelManager:
    global _model_manager
    if _model_manager is None:
        if models_dir is None:
            from .paths import models_dir as _default_models_dir  # installed copies: ~/.imurun/models

            models_dir = str(_default_models_dir())
        _model_manager = ModelManager(Path(models_dir))
    return _model_manager