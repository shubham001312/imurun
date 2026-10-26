"""G-2 limits(): turn a machine profile into safe operating caps.

`conservative` is the default for every install. `full` exists only for a
user who explicitly opts in to using the whole machine (G1).
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


@dataclass(frozen=True)
class Limits:
    mode: str
    max_model_ram: int
    max_threads: int
    min_free_disk: int
    max_request_bytes: int


def limits(profile: MachineProfile, mode: str = "conservative") -> Limits:
    if mode not in ("conservative", "full"):
        raise ValueError(f"unknown limits mode: {mode}")
    conservative = mode == "conservative"
    share = CONSERVATIVE_RAM_SHARE if conservative else FULL_RAM_SHARE
    return Limits(
        mode=mode,
        max_model_ram=int(profile.ram_available * share),
        max_threads=max(1, profile.cpu_cores - 1) if conservative else max(1, profile.cpu_cores),
        min_free_disk=CONSERVATIVE_MIN_FREE_DISK if conservative else FULL_MIN_FREE_DISK,
        max_request_bytes=CONSERVATIVE_REQUEST_BYTES if conservative else FULL_REQUEST_BYTES,
    )
