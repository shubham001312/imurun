"""NVIDIA-hosted models: the key, the user's model list, and the API routes.

The user pastes a developer key from build.nvidia.com in Settings → Models.
It is verified with a one-token call, then kept in the same secret store as
connector tokens (OS keychain, else a DPAPI / AES-GCM file) under
``model.nvidia``. It is never returned by the API, written to a log or put
in an error message: views show ``****abcd`` only.

Which hosted models appear in the composer's model selector is a plain
preference (``<home>/remote_models.json``): the recommended ones that the
key's catalogue offers are switched on by default, and the user can add any
other chat model from the catalogue. Selecting one routes generation to it
through ``EngineModelClient.select_remote``; selecting a local model routes
back to the FISH engine. The choice survives a restart.

Routes (all behind the loopback bearer token, like every /api route):

  GET    /api/remote/nvidia           status: configured, key hint, enabled, selected
  GET    /api/remote/nvidia/models    the key's chat catalogue (?refresh=1 to re-read)
  POST   /api/remote/nvidia/key       {"key": "nvapi-..."}  verify + save
  POST   /api/remote/nvidia/models    {"enabled": ["meta/llama-3.3-70b-instruct", ...]}
  DELETE /api/remote/nvidia/key       forget the key (switches back to local if needed)

Selection itself goes through the existing ``POST /api/models/select`` with
an id of the form ``nvidia:<model id>``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..safety.keys import InvalidKey, redact, validate_key

try:  # run as ``imurun.services...``
    from ...model.nvidia_client import (DEFAULT_ENABLED, NVIDIA_BASE_URL, RECOMMENDED, NvidiaClient,
                                        RemoteError, display_name, is_chat_model)
except ImportError:  # run from the imurun directory
    from model.nvidia_client import (DEFAULT_ENABLED, NVIDIA_BASE_URL, RECOMMENDED, NvidiaClient,
                                     RemoteError, display_name, is_chat_model)

SECRET_NAME = "model.nvidia"
ID_PREFIX = "nvidia:"
Reply = tuple[int, dict]


class NvidiaSettings:
    """The key (secret store) and the model preferences (plain JSON)."""

    def __init__(self, home: Path | str, secrets=None) -> None:
        self._home = Path(home)
        self._path = self._home / "remote_models.json"
        self._secrets = secrets
        self._lock = threading.Lock()
        self._catalogue: list[dict] | None = None
        self._catalogue_at = 0.0

    # ------------------------------------------------------------ secret
    def _store(self):
        if self._secrets is None:
            from ..connectors.tokens import build_secret_store

            self._secrets = build_secret_store(self._home)
        return self._secrets

    def key(self) -> str | None:
        env = os.getenv("NVIDIA_API_KEY", "").strip()
        if env:
            return env
        try:
            return self._store().get(SECRET_NAME) or None
        except Exception:  # noqa: BLE001 - an unreadable store means "no key"
            return None

    def set_key(self, key: str) -> None:
        self._store().set(SECRET_NAME, key)
        self._catalogue = None

    def clear_key(self) -> None:
        try:
            self._store().delete(SECRET_NAME)
        except Exception:  # noqa: BLE001
            pass
        self._catalogue = None

    def store_view(self) -> dict:
        try:
            from ..connectors.tokens import store_view

            return store_view(self._store())
        except Exception:  # noqa: BLE001
            return {"backend": "unknown", "encrypted": False, "warning": ""}

    # ------------------------------------------------------------- prefs
    def prefs(self) -> dict:
        with self._lock:
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        enabled = data.get("enabled")
        return {
            "enabled": [str(m) for m in enabled] if isinstance(enabled, list) else list(DEFAULT_ENABLED),
            "selected": str(data.get("selected") or "") or None,
        }

    def save_prefs(self, **changes: Any) -> dict:
        prefs = self.prefs()
        prefs.update({k: v for k, v in changes.items() if k in ("enabled", "selected")})
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(prefs, indent=1), encoding="utf-8")
            os.replace(tmp, self._path)
        return prefs

    # --------------------------------------------------------- catalogue
    def catalogue(self, client: NvidiaClient, refresh: bool = False) -> list[dict]:
        if not refresh and self._catalogue is not None and time.time() - self._catalogue_at < 3600:
            return self._catalogue
        models = client.list_models()
        self._catalogue, self._catalogue_at = models, time.time()
        return models

    def view(self, engine=None) -> dict:
        key = self.key()
        prefs = self.prefs()
        active = bool(engine is not None and getattr(engine, "remote", False))
        client = getattr(engine, "remote_client", None)
        return {
            "provider": "nvidia",
            "configured": bool(key),
            "key_hint": redact(key) if key else "",
            "from_env": bool(os.getenv("NVIDIA_API_KEY", "").strip()),
            "store": self.store_view(),
            "enabled": prefs["enabled"],
            "selected": (client.model if active and client is not None else None),
            "recommended": list(RECOMMENDED),
            "signup_url": "https://build.nvidia.com/",
        }


# ----------------------------------------------------------------- wiring
def settings_for(server: Any) -> NvidiaSettings:
    settings = getattr(server, "_remote_settings", None)
    if settings is None:
        settings = NvidiaSettings(getattr(server, "_home", None) or Path.home() / ".imurun")
        server._remote_settings = settings
    return settings


def _engine(server: Any):
    return getattr(server, "_model", None)


def _client(server: Any) -> NvidiaClient | None:
    engine = _engine(server)
    client = getattr(engine, "remote_client", None)
    if client is None and engine is not None and hasattr(engine, "attach_remote"):
        settings = settings_for(server)
        client = NvidiaClient(settings.key, base_url=os.getenv("IMURUN_NVIDIA_BASE_URL", NVIDIA_BASE_URL))
        engine.attach_remote(client)
    return client


def attach(server: Any, engine: Any, home: Path | str) -> None:
    """At startup: give the engine its NVIDIA client and restore the choice."""
    if engine is None or not hasattr(engine, "attach_remote"):
        return
    settings = NvidiaSettings(home)
    server._remote_settings = settings
    client = NvidiaClient(settings.key, base_url=os.getenv("IMURUN_NVIDIA_BASE_URL", NVIDIA_BASE_URL))
    engine.attach_remote(client)
    selected = settings.prefs().get("selected")
    if selected and settings.key():
        engine.select_remote(selected)


def model_entries(server: Any) -> list[dict]:
    """Rows for GET /api/models: the enabled hosted models, when a key exists."""
    settings = settings_for(server)
    if not settings.key():
        return []
    engine = _engine(server)
    client = getattr(engine, "remote_client", None)
    active = client.model if (client is not None and getattr(engine, "remote", False)) else None
    rows = []
    for model_id in settings.prefs()["enabled"]:
        rows.append({
            "id": ID_PREFIX + model_id, "model": model_id, "name": display_name(model_id),
            "provider": "nvidia", "remote": True, "downloaded": True, "compatible": True,
            "selected": model_id == active, "description": "Hosted by NVIDIA. Needs an internet connection.",
            "tags": ["nvidia", "online"],
        })
    return rows


def select(server: Any, model_id: str) -> Reply:
    """POST /api/models/select with ``nvidia:<id>``."""
    model_id = model_id[len(ID_PREFIX):] if model_id.startswith(ID_PREFIX) else model_id
    settings = settings_for(server)
    if not settings.key():
        return 400, {"error": "Add an NVIDIA API key in Settings, Models first."}
    if not is_chat_model(model_id):
        return 400, {"error": "That NVIDIA model is not a chat model."}
    engine = _engine(server)
    if engine is None or not hasattr(engine, "select_remote") or _client(server) is None:
        return 501, {"error": "this server cannot use hosted models"}
    if not engine.select_remote(model_id):
        return 500, {"error": "could not switch to the NVIDIA model"}
    prefs = settings.prefs()
    enabled = prefs["enabled"] if model_id in prefs["enabled"] else prefs["enabled"] + [model_id]
    settings.save_prefs(selected=model_id, enabled=enabled)
    name = engine.selected_name()
    _publish(server, "model.switch.done", {"model_name": name, "remote": True})
    return 200, {"selected": name, "id": ID_PREFIX + model_id, "queued": False, "remote": True}


def local_selected(server: Any) -> None:
    """A local model was selected: forget the hosted choice for next start."""
    settings = getattr(server, "_remote_settings", None)
    if settings is not None:
        try:
            settings.save_prefs(selected=None)
        except OSError:
            pass


# ----------------------------------------------------------------- routes
def handle_get(server: Any, raw_path: str) -> Reply | None:
    path = urlsplit(raw_path).path
    if path == "/api/remote/nvidia":
        return 200, settings_for(server).view(_engine(server))
    if path == "/api/remote/nvidia/models":
        settings = settings_for(server)
        if not settings.key():
            return 400, {"error": "Add an NVIDIA API key first."}
        client = _client(server)
        if client is None:
            return 501, {"error": "this server cannot use hosted models"}
        refresh = (parse_qs(urlsplit(raw_path).query).get("refresh") or ["0"])[0] in ("1", "true")
        try:
            models = settings.catalogue(client, refresh=refresh)
        except RemoteError as exc:
            return (400 if exc.status in (401, 403) else 502), {"error": str(exc)}
        enabled = set(settings.prefs()["enabled"])
        return 200, {"models": [dict(m, enabled=m["id"] in enabled) for m in models]}
    return None


def is_post_route(path: str) -> bool:
    return path in ("/api/remote/nvidia/key", "/api/remote/nvidia/models")


def handle_post(server: Any, path: str, payload: dict) -> Reply:
    settings = settings_for(server)
    if path == "/api/remote/nvidia/key":
        raw = payload.get("key")
        try:
            key = validate_key(raw)
        except InvalidKey as exc:
            return 400, {"error": f"That key cannot be used: {exc}."}
        if not key.startswith("nvapi-"):
            return 400, {"error": "NVIDIA keys start with nvapi-. Copy it again from build.nvidia.com."}
        client = _client(server)
        if client is None:
            return 501, {"error": "this server cannot use hosted models"}
        verified, warning = True, ""
        try:
            client.verify(key)
        except RemoteError as exc:
            if exc.status in (401, 403):
                return 400, {"error": str(exc)}
            # Offline or NVIDIA busy: keep the key, say it is unverified.
            verified, warning = False, f"Saved, but not verified yet: {exc}"
        try:
            settings.set_key(key)
        except Exception as exc:  # noqa: BLE001 - the store's message never holds the key
            return 500, {"error": f"could not store the key ({type(exc).__name__})"}
        _record(server, f"NVIDIA key saved {redact(key)}{'' if verified else ' (unverified)'}")
        return 200, dict(settings.view(_engine(server)), verified=verified, warning=warning)
    if path == "/api/remote/nvidia/models":
        enabled = payload.get("enabled")
        if not isinstance(enabled, list) or not all(isinstance(m, str) for m in enabled):
            return 400, {"error": "enabled must be a list of model ids"}
        cleaned = []
        for model_id in enabled:
            model_id = model_id.strip()
            if model_id.startswith(ID_PREFIX):
                model_id = model_id[len(ID_PREFIX):]
            if is_chat_model(model_id) and model_id not in cleaned:
                cleaned.append(model_id)
        settings.save_prefs(enabled=cleaned[:40])
        return 200, settings.view(_engine(server))
    return 404, {"error": "not found"}


def handle_delete(server: Any, path: str) -> Reply | None:
    if path != "/api/remote/nvidia/key":
        return None
    settings = settings_for(server)
    settings.clear_key()
    settings.save_prefs(selected=None)
    engine = _engine(server)
    if engine is not None and hasattr(engine, "use_local"):
        engine.use_local()
    _record(server, "NVIDIA key removed")
    _publish(server, "model.switch.done", {"model_name": _safe_name(engine), "remote": False})
    return 200, settings.view(engine)


def _safe_name(engine: Any) -> str:
    try:
        return str(engine.selected_name() or "")
    except Exception:  # noqa: BLE001
        return ""


def _publish(server: Any, kind: str, data: dict) -> None:
    events = getattr(server, "_events", None)
    if events is not None:
        try:
            events.publish(kind, data)
        except Exception:  # noqa: BLE001
            pass


def _record(server: Any, why: str) -> None:
    recorder = getattr(server, "_record", None)
    if callable(recorder):
        try:
            recorder("remote-model", why)
        except Exception:  # noqa: BLE001
            pass
