"""OAuth 2.0 device authorization grant (RFC 8628) - GitHub's headless fallback.

For a run without a browser on this machine (a server, SSH, the TUI):

1. ``POST /api/connectors/github/device`` -> ``start()`` asks GitHub for a
   device code and returns ``{user_code, verification_uri, expires_in}``;
2. the user opens the URL on any device and types the code;
3. a background thread polls the token endpoint at GitHub's ``interval``
   (``slow_down`` adds 5 s, as the RFC says) until the user approves,
   denies, or the code expires; the token goes to the token store;
4. ``GET /api/connectors/github/device`` reports ``pending`` /
   ``connected`` / ``denied`` / ``expired`` / ``error``; a ``connector``
   event is published when it ends.

The device code itself never leaves this process (only the short user code
is shown). Device flow must be enabled in the GitHub OAuth app settings.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .oauth import _store_scope

GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceFlow:
    def __init__(self, setup: Any, *, publish: Callable[[str, dict], Any] | None = None,
                 sleep=time.sleep, clock=time.time, background: bool = True) -> None:
        self.setup = setup
        self._publish = publish
        self._sleep = sleep
        self._clock = clock
        self._background = background
        self._lock = threading.RLock()   # _finish runs inside poll_once's lock
        self._state: dict = {"state": "idle"}
        self._device_code = ""
        self._generation = 0

    def status(self) -> dict:
        with self._lock:
            view = dict(self._state)
        if view.get("expires_at"):
            view["expires_in"] = max(0, round(view.pop("expires_at") - self._clock()))
        return view

    def start(self) -> dict:
        spec = self.setup.spec
        client_id = self.setup.client_id()
        if not spec.device_code_url:
            raise ValueError(f"{spec.label} has no device flow")
        if not client_id:
            raise ValueError(f"add the {spec.label} OAuth client id in Settings first")
        scopes = self.setup.scopes()
        answer = self.setup.form_transport.post_form(
            spec.device_code_url, {"client_id": client_id, "scope": " ".join(scopes)})
        if not isinstance(answer, dict) or not answer.get("device_code") or not answer.get("user_code"):
            reason = (answer or {}).get("error_description") or (answer or {}).get("error") or "no device code"
            raise ValueError(f"{spec.label} refused the device flow: {str(reason)[:160]}")
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._device_code = str(answer["device_code"])
            self._state = {
                "state": "pending",
                "user_code": str(answer["user_code"]),
                "verification_uri": str(answer.get("verification_uri") or "https://github.com/login/device"),
                "interval": max(1, int(answer.get("interval") or 5)),
                "expires_at": self._clock() + float(answer.get("expires_in") or 900),
            }
        if self._background:
            threading.Thread(target=self._poll_loop, args=(generation,), name="oauth-device",
                             daemon=True).start()
        return self.status()

    def _poll_loop(self, generation: int) -> None:
        while True:
            with self._lock:
                if generation != self._generation or self._state.get("state") != "pending":
                    return
                interval = self._state["interval"]
            self._sleep(interval)
            if self.poll_once(generation) != "pending":
                return

    def poll_once(self, generation: int | None = None) -> str:
        spec = self.setup.spec
        with self._lock:
            if generation is not None and generation != self._generation:
                return "stale"
            if self._state.get("state") != "pending":
                return self._state.get("state", "idle")
            if self._clock() >= self._state["expires_at"]:
                return self._finish("expired", "the code expired; start again")
            code = self._device_code
        try:
            answer = self.setup.form_transport.post_form(spec.device_token_url, {
                "client_id": self.setup.client_id(), "device_code": code, "grant_type": GRANT})
        except Exception as exc:  # noqa: BLE001 - network hiccups keep polling
            with self._lock:
                self._state["last_error"] = type(exc).__name__
            return "pending"
        answer = answer if isinstance(answer, dict) else {}
        error = str(answer.get("error") or "")
        if answer.get("access_token"):
            tokens = self.setup.tokens
            tokens.set(spec.id, str(answer["access_token"]))
            if answer.get("refresh_token"):
                tokens.set(f"{spec.id}:refresh", str(answer["refresh_token"]))
            if answer.get("expires_in"):
                tokens.set(f"{spec.id}:expires_at", str(self._clock() + float(answer["expires_in"])))
            _store_scope(tokens, spec.id, answer)
            return self._finish("connected", "")
        if error == "authorization_pending":
            return "pending"
        if error == "slow_down":
            with self._lock:
                self._state["interval"] = self._state["interval"] + 5
            return "pending"
        if error == "access_denied":
            return self._finish("denied", "the request was declined")
        if error == "expired_token":
            return self._finish("expired", "the code expired; start again")
        return self._finish("error", str(answer.get("error_description") or error or "unknown answer")[:200])

    def _finish(self, state: str, message: str) -> str:
        with self._lock:
            self._state = {"state": state, "message": message}
            self._device_code = ""
        if self._publish is not None:
            try:
                self._publish("connector", {"connector": self.setup.spec.id, "connected": state == "connected",
                                            "message": message or "Connected", "via": "device"})
            except Exception:  # noqa: BLE001
                pass
        return state
