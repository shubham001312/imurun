"""HTTP transport for connectors and OAuth token exchange.

Standard library only: no requests, no aiohttp, no extra wheel to install.
Every failure becomes a ``ConnectorError`` whose message carries the provider
status and a short reason, never the bearer token or the request body.
"""
from __future__ import annotations

import json as json_lib
import socket
import urllib.error
import urllib.parse
import urllib.request

from .base import ConnectorError

USER_AGENT = "IMURUN/1.0 (+local)"
DEFAULT_TIMEOUT = 20.0


def _short(value: str, limit: int = 200) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _decode(body: bytes, content_type: str):
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    looks_json = "json" in content_type.lower() or text[:1] in "{[" or text[:5].lower() == "ok: {"
    if looks_json:
        try:
            return json_lib.loads(text)
        except ValueError:
            pass
    # GitHub's token endpoint still answers form-encoded without Accept: json.
    if "=" in text and "&" in text:
        parsed = urllib.parse.parse_qs(text)
        return {key: values[-1] for key, values in parsed.items() if values}
    return {"raw": text}


class UrllibTransport:
    """One shared implementation for every provider."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = float(timeout)

    def request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        json_body: dict | None = None,
        *,
        json: dict | None = None,
    ) -> dict:
        """Send a JSON request and return the decoded response.

        ``json`` is accepted as an alias so the ``HttpTransport`` protocol and
        older call sites keep working.
        """
        body = json_body if json_body is not None else json
        payload = None
        request_headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if headers:
            request_headers.update({str(key): str(value) for key, value in headers.items()})
        if body is not None:
            payload = json_lib.dumps(body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")

        request = urllib.request.Request(
            url,
            data=payload,
            headers=request_headers,
            method=str(method).upper(),
        )
        return self._open(request)

    def post_form(self, url: str, data: dict, headers: dict | None = None) -> dict:
        """OAuth token exchange. Form-encoded body, JSON answer preferred.

        ``headers`` carries e.g. HTTP Basic client authentication (Canva).
        """
        payload = urllib.parse.urlencode(data).encode("utf-8")
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        }
        request_headers.update({str(k): str(v) for k, v in (headers or {}).items()})
        request = urllib.request.Request(url, data=payload, headers=request_headers, method="POST")
        return self._open(request)

    def _open(self, request: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type", "")
                return _decode(body, content_type)
        except urllib.error.HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            decoded = _decode(body, content_type)
            reason = ""
            if isinstance(decoded, dict):
                reason = _short(
                    decoded.get("message")
                    or decoded.get("error_description")
                    or decoded.get("error")
                    or decoded.get("raw")
                    or ""
                )
            raise ConnectorError(
                f"{request.get_method()} {urllib.parse.urlsplit(request.full_url).netloc} "
                f"answered {exc.code}{(': ' + reason) if reason else ''}"
            ) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, socket.timeout):
                reason = "timed out"
            raise ConnectorError(f"network error: {_short(reason)}") from None
        except socket.timeout:
            raise ConnectorError("network error: timed out") from None
        except Exception as exc:  # noqa: BLE001 - a transport failure is a connector failure
            raise ConnectorError(f"network error: {type(exc).__name__}") from None

