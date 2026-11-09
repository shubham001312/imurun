"""Connectivity checks.

The shell has to know whether the machine can reach the internet before it
promises anything about live results. The probe is a TCP connect to a public
DNS server, cached for a short time so a busy turn does not pay for it
again, and safe to call from several threads.
"""
from __future__ import annotations

import socket
import threading
import time

_CACHE_SECONDS = 30.0
_lock = threading.Lock()
_cached_at: float = 0.0
_cached_value: bool = False


class OfflineError(RuntimeError):
    """Raised when a tool needs the network and there is none."""


def network_available(
    host: str = "1.1.1.1",
    port: int = 443,
    timeout: float = 1.5,
    *,
    use_cache: bool = True,
) -> bool:
    """True when a TCP connection to ``host:port`` succeeds.

    The answer is cached for 30 seconds: one turn asks several tools and
    they must not each pay for a probe.
    """
    global _cached_at, _cached_value
    now = time.monotonic()
    if use_cache:
        with _lock:
            if now - _cached_at < _CACHE_SECONDS:
                return _cached_value
    try:
        with socket.create_connection((host, port), timeout=timeout):
            reachable = True
    except OSError:
        reachable = False
    if use_cache:
        with _lock:
            _cached_at = now
            _cached_value = reachable
    return reachable


def reset_cache() -> None:
    """Forget the cached probe. Used by tests and by the Settings screen."""
    global _cached_at, _cached_value
    with _lock:
        _cached_at = 0.0
        _cached_value = False
