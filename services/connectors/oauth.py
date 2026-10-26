"""OAuth 2.0 + PKCE for /connect: state is one-time, redirect is loopback.

The authorization URL never carries a client secret. The state is popped
before the exchange, so a replayed callback cannot reuse it. Tokens go to
the OS keychain and are never returned in a URL.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urlsplit

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class OAuthError(RuntimeError):
    """The flow was refused or the provider did not complete it."""


class OAuthTransport(Protocol):
    def post_form(self, url: str, data: dict) -> dict: ...


@dataclass(frozen=True)
class OAuthProvider:
    id: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id: str
    client_secret: str = ""


@dataclass
class _Pending:
    verifier: str
    expires_at: float


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthFlow:
    def __init__(
        self,
        provider: OAuthProvider,
        transport: OAuthTransport,
        tokens,
        redirect_uri: str,
        *,
        ttl: float = 600.0,
        mark=None,
        clock=time.time,
    ) -> None:
        if not provider.id:
            raise ValueError("the provider id is required")
        if not provider.client_id:
            raise ValueError("the client id is required")
        for url in (provider.authorize_url, provider.token_url):
            if urlsplit(url).scheme != "https":
                raise ValueError("OAuth endpoints must use https")
        redirect_host = urlsplit(redirect_uri).hostname
        if redirect_host not in LOOPBACK:
            raise ValueError("the OAuth redirect must be a loopback address")
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self._provider = provider
        self._transport = transport
        self._tokens = tokens
        self._redirect = redirect_uri
        self._ttl = ttl
        self._mark = mark
        self._clock = clock
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def begin(self) -> dict:
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        with self._lock:
            self._pending[state] = _Pending(
                verifier=verifier,
                expires_at=self._clock() + self._ttl,
            )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._provider.client_id,
                "redirect_uri": self._redirect,
                "scope": " ".join(self._provider.scopes),
                "state": state,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
            }
        )
        return {"url": f"{self._provider.authorize_url}?{query}", "state": state}

    def complete(self, state: str, code: str) -> str:
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or pending.expires_at <= self._clock():
            self._deny("unknown or expired state")
            raise OAuthError("the OAuth state is unknown or expired")
        if not code:
            self._deny("no authorization code")
            raise OAuthError("the authorization code is required")

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self._provider.client_id,
            "redirect_uri": self._redirect,
            "code_verifier": pending.verifier,
        }
        if self._provider.client_secret:
            data["client_secret"] = self._provider.client_secret
        try:
            response = self._transport.post_form(self._provider.token_url, data)
        except Exception as exc:  # noqa: BLE001 - provider failures become OAuthError
            self._deny(f"exchange failed: {type(exc).__name__}")
            raise OAuthError(f"the token exchange failed: {type(exc).__name__}") from exc

        token = response.get("access_token") if isinstance(response, dict) else None
        if not token:
            self._deny("no access token")
            raise OAuthError("the provider returned no access token")
        self._tokens.set(self._provider.id, str(token))
        if self._mark is not None:
            try:
                self._mark("G3-oauth", self._provider.id, "connected")
            except Exception:  # noqa: BLE001 - marking must never break the flow
                pass
        return str(token)

    def _deny(self, why: str) -> None:
        if self._mark is None:
            return
        try:
            self._mark("G3-oauth", self._provider.id, f"refused: {why}")
        except Exception:  # noqa: BLE001 - marking must never change the answer
            pass
