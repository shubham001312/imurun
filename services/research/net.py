"""HTTP for the research engine: capped, time-boxed, never raises.

Every source and the page fetcher go through one ``Fetcher`` so tests can
swap the whole network for a fake (``FakeFetcher`` in the tests implements
the same ``get``). Rules:

* the client identifies itself honestly (``USER_AGENT``); it never pretends
  to be a browser and never tries to get past a bot check - a refusal is
  reported as a refusal;
* a body is capped at ``max_bytes`` (default 2 MB) and the whole request at
  ``timeout`` seconds, counted across redirects and the body download;
* automatic fetches refuse loopback / private / link-local literal addresses
  and ``localhost`` (a search result must not make the app call itself or
  the LAN).
"""
from __future__ import annotations

import ipaddress
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse

USER_AGENT = "IMURUN/0.3 (local-first assistant; +https://github.com/imutechz/imurun)"
MAX_BYTES = 2 * 1024 * 1024

_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([\w-]+)""", re.IGNORECASE)


@dataclass
class Response:
    url: str
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error: str = ""
    truncated: bool = False
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def text(self) -> str:
        charset = ""
        header = self.headers.get("content-type", "")
        match = re.search(r"charset=([\w-]+)", header, re.IGNORECASE)
        if match:
            charset = match.group(1)
        else:
            sniff = _CHARSET.search(self.body[:4096])
            if sniff:
                charset = sniff.group(1).decode("ascii", "ignore")
        for encoding in (charset, "utf-8"):
            if not encoding:
                continue
            try:
                return self.body.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        import json

        return json.loads(self.body.decode("utf-8", "replace"))


def is_public_url(url: str) -> bool:
    """http(s) and not an obviously local address (literal IPs and localhost)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified)


def with_params(base: str, params: dict[str, Any]) -> str:
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    if not query:
        return base
    return base + ("&" if "?" in base else "?") + query


class Fetcher:
    """A shared httpx client with per-call caps. Thread-safe."""

    def __init__(self, client: Any = None, *, user_agent: str = USER_AGENT) -> None:
        self._client = client
        self._user_agent = user_agent
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is None:
                import httpx

                self._client = httpx.Client(
                    follow_redirects=True, max_redirects=5,
                    limits=httpx.Limits(max_connections=24, max_keepalive_connections=8),
                )
            return self._client

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 8.0,
        max_bytes: int = MAX_BYTES,
        allow_private: bool = False,
    ) -> Response:
        started = time.monotonic()
        if not allow_private and not is_public_url(url):
            return Response(url, error="refused: not a public http(s) address")
        request_headers = {"User-Agent": self._user_agent, "Accept": "*/*",
                           "Accept-Language": "en;q=0.9, *;q=0.5"}
        request_headers.update(headers or {})
        try:
            import httpx
        except ImportError:  # pragma: no cover - httpx is a hard dependency
            return Response(url, error="httpx is not installed")
        try:
            client = self._get_client()
            with client.stream("GET", url, headers=request_headers,
                               timeout=httpx.Timeout(timeout, connect=min(timeout, 4.0))) as response:
                chunks: list[bytes] = []
                size = 0
                truncated = False
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        truncated = True
                        break
                    if time.monotonic() - started > timeout:
                        return Response(str(response.url), response.status_code,
                                        {k.lower(): v for k, v in response.headers.items()},
                                        error="timed out while reading", elapsed=time.monotonic() - started)
                body = b"".join(chunks)[:max_bytes]
                return Response(
                    str(response.url), response.status_code,
                    {k.lower(): v for k, v in response.headers.items()}, body,
                    error="" if response.status_code < 400 else f"HTTP {response.status_code}",
                    truncated=truncated, elapsed=time.monotonic() - started,
                )
        except httpx.TimeoutException:
            return Response(url, error="timed out", elapsed=time.monotonic() - started)
        except httpx.TooManyRedirects:
            return Response(url, error="too many redirects", elapsed=time.monotonic() - started)
        except httpx.HTTPError as exc:
            return Response(url, error=f"unreachable: {type(exc).__name__}", elapsed=time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 - never raise into the pipeline
            return Response(url, error=f"{type(exc).__name__}: {exc}", elapsed=time.monotonic() - started)


_default: Fetcher | None = None
_default_lock = threading.Lock()


def default_fetcher() -> Fetcher:
    global _default
    with _default_lock:
        if _default is None:
            _default = Fetcher()
        return _default
