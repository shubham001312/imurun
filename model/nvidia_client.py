"""NVIDIA-hosted models (build.nvidia.com): the NVIDIA preset of the hosted client.

NVIDIA's API is OpenAI-compatible, so the HTTP work lives in
``model/remote_client.py`` (``OpenAICompatClient``), which every hosted
provider shares. This module keeps the names older code and tests import
(``NvidiaClient``, ``RemoteError``, ``RECOMMENDED`` ...).

Safety (G5): the key is only ever sent to NVIDIA's API host, or to a
loopback address (tests point ``base_url`` at a local fake).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Optional

try:  # run as ``imurun.model...``
    from .remote_client import (CONTEXT_TOKENS, OpenAICompatClient, RemoteError, check_base,
                                display_name, is_chat_model)
except ImportError:  # run from the imurun directory
    from model.remote_client import (CONTEXT_TOKENS, OpenAICompatClient, RemoteError, check_base,
                                     display_name, is_chat_model)

__all__ = ["CONTEXT_TOKENS", "DEFAULT_ENABLED", "NVIDIA_BASE_URL", "NVIDIA_HOST", "NvidiaClient",
           "PROBE_MODEL", "RECOMMENDED", "RemoteError", "display_name", "is_chat_model"]

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_HOST = "integrate.api.nvidia.com"

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
PROBE_MODEL = "meta/llama-3.1-8b-instruct"


def _check_base(base_url: str) -> str:
    return check_base(base_url, (NVIDIA_HOST,), "NVIDIA")


class NvidiaClient(OpenAICompatClient):
    """One NVIDIA-hosted model behind the local engine's interface."""

    def __init__(self, key: Callable[[], Optional[str]] | str | None, model: str = "", *,
                 base_url: str = NVIDIA_BASE_URL, **options) -> None:
        options.setdefault("hosts", (NVIDIA_HOST,))
        options.setdefault("provider", "nvidia")
        options.setdefault("label", "NVIDIA")
        options.setdefault("key_url", "https://build.nvidia.com/")
        options.setdefault("recommended", RECOMMENDED)
        options.setdefault("probe_model", PROBE_MODEL)
        super().__init__(key, model, base_url=base_url, **options)

    def verify(self, key: str | None, mode: str = "chat") -> None:  # NVIDIA's model list is public
        super().verify(key, mode)
