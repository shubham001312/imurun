"""G-1 limits(): turn a machine profile into safe operating caps.

`conservative` is the default for every install. `full` exists only for a
user who explicitly opts in to using the whole machine (G1).

Budgets come from TOTAL memory with a fixed reserve, not from a transient
"available" figure, so a busy machine does not silently refuse a model and a
fresh machine does not promise memory that apps will later take (H-17).
"""
from __future__ import annotations

from dataclasses import dataclass

from .profile import MachineProfile

GB = 1024**3
MB = 1024**2

CONSERVATIVE_RAM_SHARE = 0.60
FULL_RAM_SHARE = 0.90
CONSERVATIVE_MIN_FREE_DISK = 2 * GB
FULL_MIN_FREE_DISK = 1 * GB
CONSERVATIVE_REQUEST_BYTES = 8 * MB
FULL_REQUEST_BYTES = 64 * MB
CONSERVATIVE_TIMEOUT = 120.0
FULL_TIMEOUT = 300.0

# Rough KV-cache cost per token (128 KiB) and fixed compute buffers.
KV_BYTES_PER_TOKEN = 128 * 1024
COMPUTE_OVERHEAD = 512 * MB


@dataclass(frozen=True)
class Limits:
    mode: str
    max_model_ram: int
    max_threads: int
    min_free_disk: int
    max_request_bytes: int
    request_timeout: float
    kv_bytes_per_token: int
    compute_overhead: int


def limits(profile: MachineProfile, mode: str = "conservative") -> Limits:
    if mode not in ("conservative", "full"):
        raise ValueError(f"unknown limits mode: {mode}")
    conservative = mode == "conservative"
    share = CONSERVATIVE_RAM_SHARE if conservative else FULL_RAM_SHARE
    total = profile.ram_total or profile.ram_available
    return Limits(
        mode=mode,
        max_model_ram=int(total * share),
        max_threads=max(1, profile.cpu_cores - 1) if conservative else max(1, profile.cpu_cores),
        min_free_disk=CONSERVATIVE_MIN_FREE_DISK if conservative else FULL_MIN_FREE_DISK,
        max_request_bytes=CONSERVATIVE_REQUEST_BYTES if conservative else FULL_REQUEST_BYTES,
        request_timeout=CONSERVATIVE_TIMEOUT if conservative else FULL_TIMEOUT,
        kv_bytes_per_token=KV_BYTES_PER_TOKEN,
        compute_overhead=COMPUTE_OVERHEAD,
    )
