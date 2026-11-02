"""Gmail connector: search, read, labels, drafts, send, label changes, trash.

One action = one Gmail REST call through the injected transport. The gate
(``ConnectorGate``) and, for the model, the tool executor decide whether an
action may run; this class only translates and refreshes the access token.

Token handling (keys in the token store):

* ``gmail``            - the access token (about one hour);
* ``gmail:refresh``    - the refresh token (``access_type=offline``);
* ``gmail:expires_at`` - epoch seconds;
* ``gmail:scope``      - the scopes the user actually granted.

The access token is refreshed transparently 60 s before it expires, and once
more when Gmail answers 401. A refused refresh token (revoked in the Google
account, or the client was deleted) clears the stored tokens so the connector
honestly reports "not connected" instead of failing every call.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from ..base import (
    READ,
    WRITE,
    Capability,
    Connector,
    ConnectorError,
    HttpTransport,
    quote_query,
    require_token,
)

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
MAX_LIST = 50
REFRESH_MARGIN = 60.0
METADATA_HEADERS = ("From", "To", "Cc", "Reply-To", "Subject", "Date", "Message-ID", "References")
TOKEN_KEYS = ("gmail", "gmail:refresh", "gmail:expires_at", "gmail:scope", "gmail:account")

SCOPE_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_FULL = "https://mail.google.com/"

# Which granted scope satisfies which kind of use (Google's scopes nest:
# modify covers read/compose/send; compose covers send).
SATISFIES: dict[str, tuple[str, ...]] = {
    "read": (SCOPE_READ, SCOPE_MODIFY, SCOPE_FULL),
    "draft": (SCOPE_COMPOSE, SCOPE_MODIFY, SCOPE_FULL),
    "send": (SCOPE_SEND, SCOPE_COMPOSE, SCOPE_MODIFY, SCOPE_FULL),
    "modify": (SCOPE_MODIFY, SCOPE_FULL),
}
# The one scope each use asks for when the user signs in.
REQUEST: dict[str, str] = {
    "read": SCOPE_READ,
    "draft": SCOPE_COMPOSE,
    "send": SCOPE_SEND,
    "modify": SCOPE_MODIFY,
}


def _limit(value, default: int = 10) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, MAX_LIST))


def _ids(value, field: str) -> list[str]:
    if value in (None, ""):
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [require_token(item, field) for item in items if str(item).strip()]


class GmailConnector(Connector):
    id = "gmail"
    label = "Gmail"
    capabilities = (
        Capability("profile", READ),
        Capability("list_messages", READ),
        Capability("get_message", READ),
        Capability("list_threads", READ),
        Capability("get_thread", READ),
        Capability("list_labels", READ),
        Capability("create_draft", WRITE),
        Capability("send", WRITE),
        Capability("modify_labels", WRITE),
        Capability("trash", WRITE),
    )

    def __init__(self, transport: HttpTransport, tokens, *, clock: Callable[[], float] = time.time) -> None:
        self._transport = transport
        self._tokens = tokens
        self._clock = clock
        self._refresh_lock = threading.Lock()
        # Set by wiring: () -> new access token. Raises OAuthError /
        # RefreshRejected. None means "no OAuth client configured".
        self.refresher: Callable[[], str] | None = None

    # ------------------------------------------------------------ state
    def connected(self) -> bool:
        try:
            return self._tokens.get("gmail") is not None
        except Exception:  # noqa: BLE001 - a broken store means not connected
            return False

    def granted_scopes(self) -> tuple[str, ...]:
        try:
            raw = self._tokens.get("gmail:scope") or ""
        except Exception:  # noqa: BLE001
            raw = ""
        return tuple(raw.split())

    def allows(self, use: str) -> bool:
        """Whether the granted scopes cover ``read``/``draft``/``send``/``modify``.

        Unknown grants (a token stored before scopes were recorded, or a
        provider that did not echo them) count as allowed: Gmail itself then
        answers 403 and the tool reports it.
        """
        granted = self.granted_scopes()
        if not granted:
            return True
        return any(scope in granted for scope in SATISFIES.get(use, ()))

    def status(self) -> dict:
        view = super().status()
        expires = None
        try:
            raw = self._tokens.get("gmail:expires_at")
            expires = float(raw) if raw else None
            refreshable = bool(self._tokens.get("gmail:refresh"))
            view["account"] = self._tokens.get("gmail:account") or ""
        except Exception:  # noqa: BLE001
            refreshable = False
        view.update({
            "scopes": list(self.granted_scopes()),
            "refreshable": refreshable,
            "expires_in": round(expires - self._clock()) if expires else None,
        })
        return view

    def disconnect(self) -> bool:
        """Revoke at Google (best effort) and forget every stored token."""
        token = None
        try:
            token = self._tokens.get("gmail:refresh") or self._tokens.get("gmail")
        except Exception:  # noqa: BLE001
            token = None
        if token and hasattr(self._transport, "post_form"):
            try:
                # The token goes in the form body, never in the URL.
                self._transport.post_form(REVOKE_URL, {"token": token})
            except Exception:  # noqa: BLE001 - local forget still happens
                pass
        had = bool(token)
        self.forget()
        return had

    def forget(self) -> None:
        for key in TOKEN_KEYS:
            try:
                self._tokens.delete(key)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ tokens
    def _access_token(self) -> str:
        token = self._tokens.get("gmail")
        if not token:
            raise ConnectorError("gmail is not connected")
        expires_at = self._tokens.get("gmail:expires_at")
        try:
            due = expires_at is not None and float(expires_at) - REFRESH_MARGIN <= self._clock()
        except (TypeError, ValueError):
            due = False
        if due and self.refresher is not None and self._tokens.get("gmail:refresh"):
            token = self._refresh(token)
        return token

    def _refresh(self, stale: str) -> str:
        with self._refresh_lock:
            current = self._tokens.get("gmail")
            if current and current != stale:
                return current      # another thread already refreshed
            try:
                return self.refresher()  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001 - OAuthError / RefreshRejected
                if type(exc).__name__ == "RefreshRejected":
                    self.forget()
                raise ConnectorError(str(exc)) from None

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        token = self._access_token()
        url = f"{BASE}{path}"
        try:
            return self._send(method, url, token, body)
        except ConnectorError as exc:
            if "answered 401" not in str(exc) or self.refresher is None:
                raise
            if not self._tokens.get("gmail:refresh"):
                raise ConnectorError("Gmail rejected the access token and there is no refresh token; connect again") from None
        token = self._refresh(token)
        return self._send(method, url, token, body)

    def _send(self, method: str, url: str, token: str, body: dict | None) -> dict:
        headers = {"Authorization": f"Bearer {token}"}
        if body is None:
            return self._transport.request(method, url, headers)
        return self._transport.request(method, url, headers, body)

    # ------------------------------------------------------------ actions
    def execute(self, action: str, params: dict) -> dict:
        params = params or {}

        if action == "profile":
            return self._call("GET", "/profile")

        if action in ("list_messages", "list_threads"):
            query = [f"maxResults={_limit(params.get('max_results'))}"]
            text = str(params.get("q", "")).strip()
            if text:
                query.append(f"q={quote_query(text)}")
            token = str(params.get("page_token", "")).strip()
            if token:
                query.append(f"pageToken={require_token(token, 'page_token')}")
            for label in _ids(params.get("label_ids"), "label_ids"):
                query.append(f"labelIds={label}")
            kind = "messages" if action == "list_messages" else "threads"
            return self._call("GET", f"/{kind}?{'&'.join(query)}")

        if action in ("get_message", "get_thread"):
            item_id = require_token(params.get("id", ""), "id")
            fmt = str(params.get("format") or ("full" if action == "get_message" else "metadata"))
            if fmt not in ("full", "metadata", "minimal"):
                raise ConnectorError("format must be full, metadata or minimal")
            query = f"format={fmt}"
            if fmt == "metadata":
                query += "".join(f"&metadataHeaders={name}" for name in METADATA_HEADERS)
            kind = "messages" if action == "get_message" else "threads"
            return self._call("GET", f"/{kind}/{item_id}?{query}")

        if action == "list_labels":
            return self._call("GET", "/labels")

        if action == "create_draft":
            message = self._raw_message(params)
            return self._call("POST", "/drafts", {"message": message})

        if action == "send":
            return self._call("POST", "/messages/send", self._raw_message(params))

        if action == "modify_labels":
            message_id = require_token(params.get("id", ""), "id")
            add = _ids(params.get("add"), "add")
            remove = _ids(params.get("remove"), "remove")
            if not add and not remove:
                raise ConnectorError("add or remove is required")
            return self._call(
                "POST", f"/messages/{message_id}/modify",
                {"addLabelIds": add, "removeLabelIds": remove},
            )

        if action == "trash":
            message_id = require_token(params.get("id", ""), "id")
            # Google answers 411 to a POST without a body; send an empty object.
            return self._call("POST", f"/messages/{message_id}/trash", {})

        raise ConnectorError(f"gmail has no action {action!r}")

    @staticmethod
    def _raw_message(params: dict) -> dict:
        raw = str(params.get("raw", "")).strip()
        if not raw:
            raise ConnectorError("raw is required")
        message = {"raw": raw}
        thread = str(params.get("thread_id", "")).strip()
        if thread:
            message["threadId"] = require_token(thread, "thread_id")
        return message
