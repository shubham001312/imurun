"""Shared shape for bearer-token REST providers: token in, refresh, error out.

Token store keys for a provider ``<id>`` (written by ``OAuthFlow`` /
``refresh_access_token`` / the GitHub device flow):

* ``<id>``             - the access token;
* ``<id>:refresh``     - the refresh token, when the provider issues one;
* ``<id>:expires_at``  - epoch seconds, when the provider says;
* ``<id>:scope``       - the scopes the user actually granted.

The access token is refreshed 60 s before it expires and once more when the
provider answers 401. A refused refresh token forgets the account, so the
tools report "not connected" instead of failing every call. No method here
ever puts a token in an error message.
"""
from __future__ import annotations

import threading
import time
from typing import Callable
from urllib.parse import quote

from ..base import Connector, ConnectorError

REFRESH_MARGIN = 60.0


def query_string(pairs: dict) -> str:
    """``{k: v}`` -> ``?k=v&...`` (values quoted, empty values dropped)."""
    items = []
    for key, value in pairs.items():
        if value is None or value == "":
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        items += [f"{key}={quote(str(v), safe='')}" for v in values]
    return ("?" + "&".join(items)) if items else ""


def clamp(value, default: int, low: int = 1, high: int = 100) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


class BearerConnector(Connector):
    base: str = ""
    revoke_url: str = ""       # optional: POST form {token} on disconnect
    extra_headers: dict = {}

    def __init__(self, transport, tokens, *, clock: Callable[[], float] = time.time) -> None:
        self._transport = transport
        self._tokens = tokens
        self._clock = clock
        self._refresh_lock = threading.Lock()
        # Set by wiring: () -> new access token (OAuthError / RefreshRejected).
        self.refresher: Callable[[], str] | None = None

    # ------------------------------------------------------------ state
    def connected(self) -> bool:
        try:
            return self._tokens.get(self.id) is not None
        except Exception:  # noqa: BLE001
            return False

    def granted_scopes(self) -> tuple[str, ...]:
        try:
            raw = self._tokens.get(f"{self.id}:scope") or ""
        except Exception:  # noqa: BLE001
            raw = ""
        return tuple(raw.replace(",", " ").split())

    def status(self) -> dict:
        view = super().status()
        try:
            expires = self._tokens.get(f"{self.id}:expires_at")
            refreshable = bool(self._tokens.get(f"{self.id}:refresh"))
        except Exception:  # noqa: BLE001
            expires, refreshable = None, False
        try:
            expires_in = round(float(expires) - self._clock()) if expires else None
        except (TypeError, ValueError):
            expires_in = None
        try:
            account = self._tokens.get(f"{self.id}:account") or ""
        except Exception:  # noqa: BLE001
            account = ""
        view.update({"scopes": list(self.granted_scopes()), "refreshable": refreshable,
                     "expires_in": expires_in, "account": account})
        return view

    def verify_token(self, token: str) -> dict:
        """Token mode: check ``token`` with the provider before it is stored.

        Returns ``{account, permissions, expires?, expires_at?, token_id?}``;
        raises ConnectorError when the provider refuses it. Providers that
        only sign in with OAuth do not override this.
        """
        raise ConnectorError(f"{self.id} does not take pasted tokens")

    def forget(self) -> None:
        for key in (self.id, f"{self.id}:refresh", f"{self.id}:expires_at", f"{self.id}:scope",
                    f"{self.id}:account", f"{self.id}:permissions"):
            try:
                self._tokens.delete(key)
            except Exception:  # noqa: BLE001
                pass

    def disconnect(self) -> bool:
        try:
            token = self._tokens.get(f"{self.id}:refresh") or self._tokens.get(self.id)
        except Exception:  # noqa: BLE001
            token = None
        if token and self.revoke_url and hasattr(self._transport, "post_form"):
            try:
                self._transport.post_form(self.revoke_url, {"token": token})
            except Exception:  # noqa: BLE001 - local forget still happens
                pass
        self.forget()
        return bool(token)

    # ------------------------------------------------------------ tokens
    def _access_token(self) -> str:
        token = self._tokens.get(self.id)
        if not token:
            raise ConnectorError(f"{self.id} is not connected")
        expires_at = self._tokens.get(f"{self.id}:expires_at")
        try:
            due = expires_at is not None and float(expires_at) - REFRESH_MARGIN <= self._clock()
        except (TypeError, ValueError):
            due = False
        if due and self.refresher is not None and self._tokens.get(f"{self.id}:refresh"):
            token = self._refresh(token)
        return token

    def _refresh(self, stale: str) -> str:
        with self._refresh_lock:
            current = self._tokens.get(self.id)
            if current and current != stale:
                return current
            try:
                return self.refresher()  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ == "RefreshRejected":
                    self.forget()
                raise ConnectorError(str(exc)) from None

    def _headers(self, token: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {token or self._access_token()}"}
        headers.update(self.extra_headers)
        return headers

    def _request(self, method: str, path: str, body=None, *, url: str | None = None,
                 headers: dict | None = None):
        """One call; ``path`` is joined to ``base`` unless ``url`` is given."""
        target = url or f"{self.base}{path}"
        token = self._access_token()
        try:
            return self._send(method, target, token, body, headers)
        except ConnectorError as exc:
            if "answered 401" not in str(exc) or self.refresher is None \
                    or not self._tokens.get(f"{self.id}:refresh"):
                raise
        token = self._refresh(token)
        return self._send(method, target, token, body, headers)

    def _send(self, method: str, url: str, token: str, body, headers: dict | None):
        merged = self._headers(token)
        merged.update(headers or {})
        if body is None:
            return self._transport.request(method, url, merged)
        return self._transport.request(method, url, merged, body)
