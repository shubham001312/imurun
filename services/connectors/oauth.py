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
    # Slack joins scopes with commas; most providers use spaces.
    scope_separator: str = " "
    # Provider-specific extras, e.g. Google's access_type=offline.
    authorize_params: tuple[tuple[str, str], ...] = ()
    token_params: tuple[tuple[str, str], ...] = ()
    # PKCE (S256) is sent unless the provider is known to reject it.
    pkce: bool = True
    # How the client authenticates at the token endpoint: "body" sends
    # client_secret as a form field, "basic" uses HTTP Basic (Canva).
    client_auth: str = "body"


def client_credentials(provider: OAuthProvider, data: dict) -> dict | None:
    """Put the client secret where the provider wants it; returns extra headers."""
    if provider.client_auth == "basic" and provider.client_secret:
        pair = f"{provider.client_id}:{provider.client_secret}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}
    if provider.client_secret:
        data["client_secret"] = provider.client_secret
    return None


def post_token(transport, url: str, data: dict, headers: dict | None):
    if headers:
        return transport.post_form(url, data, headers=headers)
    return transport.post_form(url, data)


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

    @property
    def provider(self) -> OAuthProvider:
        return self._provider

    @property
    def redirect_uri(self) -> str:
        return self._redirect

    def begin(self) -> dict:
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        with self._lock:
            self._pending[state] = _Pending(
                verifier=verifier,
                expires_at=self._clock() + self._ttl,
            )
        params = {
            "response_type": "code",
            "client_id": self._provider.client_id,
            "redirect_uri": self._redirect,
            "scope": self._provider.scope_separator.join(self._provider.scopes),
            "state": state,
        }
        if self._provider.pkce:
            params["code_challenge"] = _challenge(verifier)
            params["code_challenge_method"] = "S256"
        if not params["scope"]:
            params.pop("scope")     # scopes fixed in the app registration (Supabase, Render)
        params.update(dict(self._provider.authorize_params))
        query = urlencode(params)
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
        }
        if self._provider.pkce:
            data["code_verifier"] = pending.verifier
        headers = client_credentials(self._provider, data)
        data.update(dict(self._provider.token_params))
        try:
            response = post_token(self._transport, self._provider.token_url, data, headers)
        except Exception as exc:  # noqa: BLE001 - provider failures become OAuthError
            self._deny(f"exchange failed: {type(exc).__name__}")
            raise OAuthError(f"the token exchange failed: {type(exc).__name__}") from exc

        if isinstance(response, dict) and response.get("ok") is False:
            reason = str(response.get("error") or "provider refused the exchange")
            self._deny(reason)
            raise OAuthError(reason)
        token = response.get("access_token") if isinstance(response, dict) else None
        if not token:
            reason = ""
            if isinstance(response, dict):
                reason = str(response.get("error_description") or response.get("error") or "")
            self._deny("no access token")
            raise OAuthError(reason or "the provider returned no access token")
        self._tokens.set(self._provider.id, str(token))
        # Long-lived providers (Google, Slack) rotate access tokens; keep the
        # refresh token so the connector can renew without a new sign-in.
        if isinstance(response, dict) and response.get("refresh_token"):
            try:
                self._tokens.set(f"{self._provider.id}:refresh", str(response["refresh_token"]))
            except Exception:  # noqa: BLE001 - a refresh token is a bonus, not a requirement
                pass
        if isinstance(response, dict) and response.get("expires_in") is not None:
            try:
                self._tokens.set(
                    f"{self._provider.id}:expires_at",
                    str(self._clock() + float(response["expires_in"])),
                )
            except Exception:  # noqa: BLE001
                pass
        # The scopes the user actually granted (Google lets the user untick
        # some). Connectors use this to report tools honestly as unavailable.
        _store_scope(self._tokens, self._provider.id, response)
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


def _store_scope(tokens, provider_id: str, response) -> None:
    if not isinstance(response, dict) or not response.get("scope"):
        return
    try:
        tokens.set(f"{provider_id}:scope", " ".join(str(response["scope"]).replace(",", " ").split()))
    except Exception:  # noqa: BLE001 - the scope list is informational
        pass


class RefreshRejected(OAuthError):
    """The provider refused the refresh token (revoked, expired or wrong client)."""


def refresh_access_token(
    provider: OAuthProvider,
    transport: OAuthTransport,
    tokens,
    *,
    clock=time.time,
    mark=None,
) -> str:
    """Trade the stored refresh token for a new access token.

    Stores the new access token, its expiry and - when the provider rotates
    it - the new refresh token. Raises ``OAuthError`` when there is no
    refresh token or the exchange fails, and ``RefreshRejected`` when the
    provider says the grant is no longer valid (the caller should then treat
    the account as disconnected). Never puts a token in an error message.
    """
    refresh = tokens.get(f"{provider.id}:refresh")
    if not refresh:
        raise OAuthError(f"{provider.id} has no refresh token; connect it again")
    if not provider.client_id:
        raise OAuthError(f"{provider.id} has no OAuth client id configured")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": provider.client_id,
    }
    headers = client_credentials(provider, data)

    def _note(why: str) -> None:
        if mark is None:
            return
        try:
            mark("G3-oauth", provider.id, why)
        except Exception:  # noqa: BLE001
            pass

    try:
        response = post_token(transport, provider.token_url, data, headers)
    except Exception as exc:  # noqa: BLE001 - the text never holds the token
        text = str(exc)
        _note(f"refresh failed: {type(exc).__name__}")
        if "invalid_grant" in text or "answered 400" in text or "answered 401" in text:
            raise RefreshRejected(
                f"{provider.id} refused the refresh token (revoked or expired); connect it again"
            ) from None
        raise OAuthError(f"refreshing {provider.id} failed: {text[:200]}") from None
    if not isinstance(response, dict):
        raise OAuthError(f"refreshing {provider.id} returned an unreadable answer")
    if response.get("error"):
        reason = str(response.get("error"))
        _note(f"refresh refused: {reason[:60]}")
        if reason in ("invalid_grant", "unauthorized_client", "invalid_client"):
            raise RefreshRejected(
                f"{provider.id} refused the refresh token ({reason}); connect it again"
            )
        raise OAuthError(f"refreshing {provider.id} failed: {reason[:200]}")
    token = response.get("access_token")
    if not token:
        raise OAuthError(f"refreshing {provider.id} returned no access token")
    tokens.set(provider.id, str(token))
    if response.get("refresh_token"):
        tokens.set(f"{provider.id}:refresh", str(response["refresh_token"]))
    if response.get("expires_in") is not None:
        try:
            tokens.set(f"{provider.id}:expires_at", str(clock() + float(response["expires_in"])))
        except (TypeError, ValueError):
            pass
    _store_scope(tokens, provider.id, response)
    _note("refreshed")
    return str(token)
