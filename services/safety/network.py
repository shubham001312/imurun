"""G5 network: loopback is the default surface; anything else needs a grant.

Two ways out of the machine:
- a short-lived host grant for a direct fetch (user approved, expires);
- a connector pin: the provider may only reach its own hosts, so a GitHub
  token can never be sent to another host.

Every refusal is marked. Nothing here trusts the caller.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class NetworkDenied(PermissionError):
    """The request left the allowed network surface."""


def host_of(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise NetworkDenied(f"invalid url: {url!r}") from exc
    if parts.scheme not in ("http", "https"):
        raise NetworkDenied(f"scheme {parts.scheme!r} is not allowed")
    host = (parts.hostname or "").lower()
    if not host:
        raise NetworkDenied("the url has no host")
    return host


@dataclass(frozen=True)
class HostGrant:
    host: str
    expires_at: float


class NetworkGuard:
    def __init__(self, mark=None, clock=time.monotonic) -> None:
        self._mark = mark
        self._clock = clock
        self._grants: dict[str, HostGrant] = {}
        self._pins: dict[str, frozenset[str]] = {}

    def grant(self, host: str, ttl: float = 300.0) -> HostGrant:
        cleaned = host.strip().lower()
        if not cleaned:
            raise ValueError("host is required")
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        grant = HostGrant(host=cleaned, expires_at=self._clock() + ttl)
        self._grants[cleaned] = grant
        return grant

    def revoke(self, host: str) -> None:
        self._grants.pop(host.strip().lower(), None)

    def pin(self, provider: str, hosts) -> None:
        if not provider:
            raise ValueError("provider is required")
        cleaned = frozenset(host.strip().lower() for host in hosts if host.strip())
        if not cleaned:
            raise ValueError("a pin needs at least one host")
        self._pins[provider] = cleaned

    def check(self, url: str) -> str:
        """Allow loopback, or a host with a live grant."""
        host = host_of(url)
        if host in LOOPBACK_HOSTS:
            return host
        grant = self._grants.get(host)
        if grant is None or grant.expires_at <= self._clock():
            self._deny(host, "no valid grant")
            raise NetworkDenied(f"{host} is not allowed without a live grant")
        return host

    def check_for(self, provider: str, url: str) -> str:
        """A connector may only reach hosts pinned to it."""
        host = host_of(url)
        pinned = self._pins.get(provider)
        if pinned is None or host not in pinned:
            self._deny(host, f"not pinned for {provider}")
            raise NetworkDenied(f"{host} is not a host of {provider}")
        return host

    def _deny(self, host: str, why: str) -> None:
        if self._mark is None:
            return
        try:
            self._mark("G5-network", host, f"denied: {why}")
        except Exception:  # noqa: BLE001 - marking must never change the answer
            pass
