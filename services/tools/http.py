"""A tiny HTTP client for the tools.

The tools talk to a handful of public endpoints. They all need the same
three things: a short timeout, a size cap so a huge page cannot exhaust
memory, and a clear error when the machine is offline. The standard library
is enough for that, and a standard-library client keeps the dependency
list of the product short.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .network import OfflineError, network_available

USER_AGENT = "IMURUN/0.2 (+local-first assistant)"
MAX_BYTES = 2 * 1024 * 1024


class HttpError(RuntimeError):
    """A request failed. The message is short and safe to show."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def require_network() -> None:
    """Raise :class:`OfflineError` when the machine cannot reach the network."""
    if not network_available():
        raise OfflineError("No network access right now.")


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    max_bytes: int = MAX_BYTES,
    data: bytes | None = None,
    check_network: bool = True,
) -> bytes:
    """Fetch a URL and return the body, capped in size.

    Raises :class:`OfflineError` when there is no network and
    :class:`HttpError` for every other failure.
    """
    if check_network:
        require_network()
    if not url.lower().startswith(("http://", "https://")):
        raise HttpError("Only http and https addresses are allowed.")

    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise HttpError(f"The server answered {exc.code}.", exc.code) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            raise HttpError("The request timed out.") from exc
        raise HttpError(f"The server could not be reached: {reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise HttpError("The request timed out.") from exc
    if len(body) > max_bytes:
        raise HttpError("The response was too large to read.")
    return body


def request_json(url: str, **kwargs: Any) -> Any:
    """Fetch a URL and parse the body as JSON."""
    body = request(url, **kwargs)
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HttpError("The server did not answer with JSON.") from exc


def build_url(base: str, params: dict[str, Any]) -> str:
    """Append query parameters to a URL."""
    query = urllib.parse.urlencode(
        {key: value for key, value in params.items() if value not in (None, "")}
    )
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{query}" if query else base
