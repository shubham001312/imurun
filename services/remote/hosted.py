"""Hosted models from every provider: keys, enabled models, selection, routes.

The user connects a provider in Settings → Models (or from the model
picker): the key is verified (an authenticated GET /models, or for NVIDIA,
whose list is public, a one-token call) and kept in the same secret store as
connector tokens (OS keychain, else a DPAPI / AES-GCM file) under
``model.<provider>``. NVIDIA keeps its original ``model.nvidia`` name. A key
is never returned by the API, written to a log or put in an error message:
views show ``****abcd`` only.

Plain preferences live in ``<home>/remote_models.json`` (version 2)::

    {"version": 2, "selected": "openrouter:deepseek/deepseek-chat",
     "providers": {"nvidia": {"enabled": [...], "contexts": {...}},
                   "custom": {"base_url": "https://...", "name": "...", "keyless": false,
                              "enabled": [...]}}}

The version-1 file the NVIDIA-only code wrote (``{"enabled": [...],
"selected": "<model>"}``) is read as NVIDIA's preferences.

Model ids are ``<provider>:<model>`` (``nvidia:meta/llama-3.3-70b-instruct``);
only the first colon separates, since OpenRouter ids contain colons.
Selecting one goes through ``POST /api/models/select`` and routes generation
to that provider's client through ``EngineModelClient.select_remote``;
selecting a local model routes back to FISH. The choice survives a restart.

Routes (all behind the loopback bearer token, like every /api route):

  GET    /api/remote                     every provider with its state
  GET    /api/remote/<p>                 one provider's state
  GET    /api/remote/<p>/models          the key's chat catalogue (?refresh=1 re-reads)
  POST   /api/remote/<p>/key             {"key"} (custom also {"base_url", "name"}): verify + save
  POST   /api/remote/<p>/models          {"enabled": ["<model id>", ...]}
  DELETE /api/remote/<p>/key             forget the key (custom: the server too)
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..safety.keys import InvalidKey, redact, validate_key
from . import providers as registry
from .providers import CUSTOM, BadBaseUrl, Provider

try:  # run as ``imurun.services...``
    from ...model.remote_client import OpenAICompatClient, RemoteError, display_name, is_chat_model
except ImportError:  # run from the imurun directory
    from model.remote_client import OpenAICompatClient, RemoteError, display_name, is_chat_model

PREFS_FILE = "remote_models.json"
MAX_ENABLED = 40
CATALOGUE_TTL = 3600.0
Reply = tuple[int, dict]
_MODEL_ID = re.compile(r"^[\w.\-/:@+]{1,200}$")


def _clean_model_id(value: object) -> str:
    text = str(value or "").strip()
    return text if _MODEL_ID.match(text) else ""


class RemoteSettings:
    """Keys (secret store) and model preferences (plain JSON), per provider."""

    def __init__(self, home: Path | str, secrets=None) -> None:
        self._home = Path(home)
        self._path = self._home / PREFS_FILE
        self._secrets = secrets
        self._lock = threading.RLock()
        self._catalogues: dict[str, tuple[float, list[dict]]] = {}

    # ------------------------------------------------------------ secret
    def _store(self):
        if self._secrets is None:
            from ..connectors.tokens import build_secret_store

            self._secrets = build_secret_store(self._home)
        return self._secrets

    def key(self, provider: Provider) -> str | None:
        if provider.env_key:
            env = os.getenv(provider.env_key, "").strip()
            if env:
                return env
        try:
            return self._store().get(provider.secret_name) or None
        except Exception:  # noqa: BLE001 - an unreadable store means "no key"
            return None

    def from_env(self, provider: Provider) -> bool:
        return bool(provider.env_key and os.getenv(provider.env_key, "").strip())

    def set_key(self, provider: Provider, key: str) -> None:
        self._store().set(provider.secret_name, key)
        self._catalogues.pop(provider.id, None)

    def clear_key(self, provider: Provider) -> None:
        try:
            self._store().delete(provider.secret_name)
        except Exception:  # noqa: BLE001
            pass
        self._catalogues.pop(provider.id, None)

    def store_view(self) -> dict:
        try:
            from ..connectors.tokens import store_view

            return store_view(self._store())
        except Exception:  # noqa: BLE001
            return {"backend": "unknown", "encrypted": False, "warning": ""}

    # ------------------------------------------------------------- prefs
    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if data.get("version") != 2:
            # Version 1 (NVIDIA only): {"enabled": [...], "selected": "<model>"}.
            old_selected = str(data.get("selected") or "")
            nvidia: dict = {}
            if isinstance(data.get("enabled"), list):
                nvidia["enabled"] = [str(m) for m in data["enabled"]]
            data = {"version": 2, "providers": {"nvidia": nvidia} if nvidia else {},
                    "selected": ("nvidia:" + old_selected) if old_selected else None}
        if not isinstance(data.get("providers"), dict):
            data["providers"] = {}
        return data

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)

    def provider_prefs(self, provider: Provider) -> dict:
        with self._lock:
            entry = self._read()["providers"].get(provider.id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        enabled = entry.get("enabled")
        entry["enabled"] = ([str(m) for m in enabled] if isinstance(enabled, list)
                            else list(provider.recommended[:provider.default_count]))
        entry["explicit"] = isinstance(enabled, list)
        contexts = entry.get("contexts")
        entry["contexts"] = {str(k): int(v) for k, v in contexts.items()
                             if isinstance(v, (int, float)) and v > 0} if isinstance(contexts, dict) else {}
        return entry

    def update_provider(self, provider: Provider, **changes: Any) -> dict:
        with self._lock:
            data = self._read()
            entry = data["providers"].get(provider.id)
            entry = dict(entry) if isinstance(entry, dict) else {}
            for name, value in changes.items():
                if value is None:
                    entry.pop(name, None)
                else:
                    entry[name] = value
            data["providers"][provider.id] = entry
            self._write(data)
        return self.provider_prefs(provider)

    def reset_provider(self, provider: Provider) -> None:
        with self._lock:
            data = self._read()
            data["providers"].pop(provider.id, None)
            if str(data.get("selected") or "").startswith(provider.id + ":"):
                data["selected"] = None
            self._write(data)

    def selected(self) -> str | None:
        with self._lock:
            return str(self._read().get("selected") or "") or None

    def set_selected(self, model_id: str | None) -> None:
        with self._lock:
            data = self._read()
            data["selected"] = model_id or None
            self._write(data)

    # ---------------------------------------------------------- per provider
    def base_url(self, provider: Provider) -> str:
        if provider.custom:
            return str(self.provider_prefs(provider).get("base_url") or "")
        return provider.env_base_url() or provider.base_url

    def hosts(self, provider: Provider) -> tuple[str, ...]:
        if provider.custom:
            url = self.base_url(provider)
            return (registry.custom_host(url),) if url else ()
        return provider.hosts

    def label(self, provider: Provider) -> str:
        if provider.custom:
            name = str(self.provider_prefs(provider).get("name") or "").strip()
            return name or "Custom server"
        return provider.label

    def keyless(self, provider: Provider) -> bool:
        return bool(provider.custom and self.provider_prefs(provider).get("keyless"))

    def configured(self, provider: Provider) -> bool:
        if provider.custom:
            return bool(self.base_url(provider)) and (self.keyless(provider) or bool(self.key(provider)))
        return bool(self.key(provider))

    # --------------------------------------------------------- catalogue
    def catalogue(self, provider: Provider, client: OpenAICompatClient, refresh: bool = False) -> list[dict]:
        cached = self._catalogues.get(provider.id)
        if not refresh and cached is not None and time.time() - cached[0] < CATALOGUE_TTL:
            return cached[1]
        models = client.list_models()
        self._catalogues[provider.id] = (time.time(), models)
        return models

    def cached_catalogue(self, provider: Provider) -> list[dict]:
        cached = self._catalogues.get(provider.id)
        return cached[1] if cached else []

    def view(self, provider: Provider, engine: Any = None) -> dict:
        key = self.key(provider)
        prefs = self.provider_prefs(provider)
        client = getattr(engine, "remote_client", None)
        active = bool(engine is not None and getattr(engine, "remote", False) and client is not None
                      and getattr(client, "provider", "") == provider.id)
        configured = self.configured(provider)
        view = dict(provider.view())
        view.update({
            "provider": provider.id,
            "label": self.label(provider),
            "configured": configured,
            "key_hint": redact(key) if key else "",
            "from_env": self.from_env(provider),
            "store": self.store_view(),
            "keyless": self.keyless(provider),
            "base_url": self.base_url(provider) if (configured or not provider.custom) else "",
            "name": str(prefs.get("name") or "") if provider.custom else "",
            "enabled": prefs["enabled"] if configured else [],
            "contexts": prefs["contexts"],
            "active": active,
            "selected": (client.model if active else None),
            "signup_url": provider.key_url,
        })
        return view


# ----------------------------------------------------------------- wiring
def settings_for(server: Any) -> RemoteSettings:
    settings = getattr(server, "_remote_settings", None)
    if settings is None:
        settings = RemoteSettings(getattr(server, "_home", None) or Path.home() / ".imurun")
        server._remote_settings = settings
    return settings


def _engine(server: Any):
    return getattr(server, "_model", None)


def build_client(settings: RemoteSettings, provider: Provider, *, key: str | None = None,
                 base_url: str | None = None) -> OpenAICompatClient:
    """A client for ``provider``. ``key`` / ``base_url`` override the stored
    ones (used to verify a key before it is saved). Raises RemoteError (kind
    ``host``) when the URL is not one the key may be sent to."""
    url = base_url if base_url is not None else settings.base_url(provider)
    if provider.custom:
        hosts: tuple[str, ...] = (registry.custom_host(url),) if url else ()
    else:
        hosts = provider.hosts
    if key is not None:
        key_source: Any = key
    elif provider.custom:
        bound = registry.custom_origin(url)

        def key_source() -> str | None:
            # G5: a custom key belongs to the origin (scheme, host, port) it was
            # saved for, recorded next to it as ``key_origin``. A client built
            # for another address, or a key left over from one, is never sent.
            prefs = settings.provider_prefs(provider)
            if not bound or prefs.get("key_origin") != bound:
                return None
            if registry.custom_origin(settings.base_url(provider)) != bound:
                return None
            return settings.key(provider)
    else:
        key_source = lambda: settings.key(provider)  # noqa: E731 - read on every call
    if not url:
        raise RemoteError(f"{settings.label(provider)} has no server address yet", kind="host")
    return OpenAICompatClient(
        key_source, base_url=url, hosts=hosts, provider=provider.id, label=settings.label(provider),
        key_url=provider.key_url, key_required=not (provider.custom and (key == "" or settings.keyless(provider)
                                                                          and key is None)),
        headers=dict(provider.headers), token_param=provider.token_param, stream_usage=provider.stream_usage,
        no_sampling_prefixes=provider.no_sampling_prefixes, recommended=provider.recommended,
        probe_model=provider.probe_model or (provider.recommended[0] if provider.recommended else ""))


def client_for(server: Any, provider: Provider) -> OpenAICompatClient | None:
    """The cached client for ``provider`` (rebuilt when its URL or name changed)."""
    settings = settings_for(server)
    cache = getattr(server, "_remote_clients", None)
    if cache is None:
        cache = server._remote_clients = {}
    url = settings.base_url(provider)
    signature = (url, settings.label(provider), settings.keyless(provider))
    held = cache.get(provider.id)
    if held is not None and held[0] == signature:
        return held[1]
    try:
        client = build_client(settings, provider)
    except RemoteError:
        cache.pop(provider.id, None)
        return None
    # Each backoff wait before a retry is announced to the shell
    # ("NVIDIA rate limit; retrying in 4 s, 1 of 5"); see model/remote_client.py.
    client._on_retry = lambda info: _publish(server, "model.retry", info)
    cache[provider.id] = (signature, client)
    return client


def _forget_client(server: Any, provider: Provider) -> None:
    cache = getattr(server, "_remote_clients", None)
    if cache:
        cache.pop(provider.id, None)


def _activate(server: Any, provider: Provider, model_id: str) -> bool:
    engine = _engine(server)
    if engine is None or not hasattr(engine, "select_remote") or not hasattr(engine, "attach_remote"):
        return False
    client = client_for(server, provider)
    if client is None:
        return False
    settings = settings_for(server)
    context = settings.provider_prefs(provider)["contexts"].get(model_id)

    def remember(mid: str, value: int) -> None:
        # A window the provider reported (model details or a context-length
        # error) is kept, so Stats and the prompt budget use the real size.
        contexts = dict(settings.provider_prefs(provider)["contexts"])
        if contexts.get(mid) != value:
            contexts[mid] = int(value)
            settings.update_provider(provider, contexts=contexts)

    if hasattr(client, "_on_context"):
        client._on_context = remember
    engine.attach_remote(client)
    try:
        chosen = bool(engine.select_remote(model_id, context=context))
    except TypeError:  # an engine facade without the context argument
        chosen = bool(engine.select_remote(model_id))
    if chosen and not context and callable(getattr(client, "fetch_context", None)):
        # Off the request path: one GET /models/{id} to the provider.
        threading.Thread(target=client.fetch_context, name="imurun-context-lookup", daemon=True).start()
    return chosen


def attach(server: Any, engine: Any, home: Path | str) -> None:
    """At startup: restore the user's last hosted choice, if any."""
    if engine is None or not hasattr(engine, "attach_remote"):
        return
    settings = RemoteSettings(home)
    server._remote_settings = settings
    server._remote_clients = {}
    selected = settings.selected()
    parsed = registry.split_id(selected) if selected else None
    if parsed is None:
        return
    provider, model_id = parsed

    def restore() -> None:
        # Off the start-up path: reading the key can wait on the system keyring.
        try:
            if settings.configured(provider):
                _activate(server, provider, model_id)
        except Exception:  # noqa: BLE001 - the local engine stays selected
            pass

    if os.getenv("IMURUN_SYNC_REMOTE_RESTORE"):  # tests: deterministic
        restore()
    else:
        threading.Thread(target=restore, name="imurun-remote-restore", daemon=True).start()


def _active(server: Any) -> tuple[str, str] | None:
    engine = _engine(server)
    client = getattr(engine, "remote_client", None)
    if client is None or not getattr(engine, "remote", False):
        return None
    return getattr(client, "provider", ""), client.model


def model_entries(server: Any) -> list[dict]:
    """Rows for GET /api/models ``hosted``: every configured provider's enabled models."""
    settings = settings_for(server)
    active = _active(server)
    rows = []
    for provider in registry.PROVIDERS.values():
        if not settings.configured(provider):
            continue
        prefs = settings.provider_prefs(provider)
        label = settings.label(provider)
        for model_id in prefs["enabled"]:
            rows.append({
                "id": f"{provider.id}:{model_id}", "model": model_id, "name": display_name(model_id),
                "provider": provider.id, "provider_label": label, "remote": True,
                "downloaded": True, "compatible": True,
                "context": prefs["contexts"].get(model_id),
                "selected": active == (provider.id, model_id),
                "description": f"Hosted by {label}. Needs an internet connection."
                               if not provider.custom else f"Served by {label}.",
                "tags": [provider.id, "online"],
            })
    return rows


def is_hosted_id(model_id: str) -> bool:
    return registry.split_id(model_id) is not None


def select(server: Any, model_id: str) -> Reply:
    """POST /api/models/select with ``<provider>:<model id>``."""
    parsed = registry.split_id(model_id)
    if parsed is None:
        return 400, {"error": "Hosted model ids look like <provider>:<model>."}
    provider, model = parsed
    model = _clean_model_id(model)
    settings = settings_for(server)
    label = settings.label(provider)
    if not model:
        return 400, {"error": "That model id has characters a model id cannot have."}
    if not settings.configured(provider):
        return 400, {"error": f"Connect {label} in Settings, Models first."}
    if not provider.custom and not is_chat_model(model):
        return 400, {"error": f"That {label} model is not a chat model."}
    engine = _engine(server)
    if engine is None or not hasattr(engine, "select_remote"):
        return 501, {"error": "this server cannot use hosted models"}
    if not _activate(server, provider, model):
        return 500, {"error": f"could not switch to the {label} model"}
    prefs = settings.provider_prefs(provider)
    if model not in prefs["enabled"]:
        settings.update_provider(provider, enabled=(prefs["enabled"] + [model])[-MAX_ENABLED:])
    settings.set_selected(f"{provider.id}:{model}")
    name = engine.selected_name()
    _publish(server, "model.switch.done", {"model_name": name, "remote": True, "provider": provider.id})
    _record(server, f"selected {provider.id}:{model}")
    return 200, {"selected": name, "id": f"{provider.id}:{model}", "queued": False, "remote": True,
                 "provider": provider.id}


def local_selected(server: Any) -> None:
    """A local model was selected: forget the hosted choice for next start."""
    settings = getattr(server, "_remote_settings", None)
    if settings is not None:
        try:
            settings.set_selected(None)
        except OSError:
            pass


# ----------------------------------------------------------------- routes
def _parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def _provider_or_404(provider_id: str) -> Provider | Reply:
    provider = registry.get(provider_id)
    if provider is None:
        return 404, {"error": f"unknown provider {provider_id[:40]!r}"}
    return provider


def _error_reply(exc: RemoteError) -> Reply:
    if exc.kind == "key":
        return 400, {"error": str(exc), "kind": exc.kind}
    if exc.kind == "host":
        return 400, {"error": str(exc), "kind": exc.kind}
    if exc.kind == "rate_limit":
        return 429, {"error": str(exc), "kind": exc.kind}
    return 502, {"error": str(exc), "kind": exc.kind}


def list_view(server: Any) -> dict:
    settings = settings_for(server)
    engine = _engine(server)
    active = _active(server)
    return {"providers": [settings.view(p, engine) for p in registry.PROVIDERS.values()],
            "selected": f"{active[0]}:{active[1]}" if active else None,
            "store": settings.store_view()}


def handle_get(server: Any, raw_path: str) -> Reply | None:
    path = urlsplit(raw_path).path.rstrip("/")
    parts = _parts(path)
    if parts[:2] != ["api", "remote"]:
        return None
    if len(parts) == 2:
        return 200, list_view(server)
    found = _provider_or_404(parts[2])
    if not isinstance(found, Provider):
        return found
    provider = found
    settings = settings_for(server)
    if len(parts) == 3:
        return 200, settings.view(provider, _engine(server))
    if len(parts) == 4 and parts[3] == "models":
        label = settings.label(provider)
        if not settings.configured(provider):
            return 400, {"error": f"Connect {label} first."}
        client = client_for(server, provider)
        if client is None:
            return 400, {"error": f"{label} has no usable server address."}
        refresh = (parse_qs(urlsplit(raw_path).query).get("refresh") or ["0"])[0] in ("1", "true")
        prefs = settings.provider_prefs(provider)
        enabled = list(prefs["enabled"])
        has_models = True
        try:
            models = settings.catalogue(provider, client, refresh=refresh)
        except RemoteError as exc:
            if exc.kind != "no_models":
                return _error_reply(exc)
            models, has_models = [], False
        rows = [dict(m, enabled=m["id"] in enabled) for m in models]
        known = {m["id"] for m in models}
        # Enabled models the catalogue does not list (typed by hand, or gone).
        extra = [{"id": mid, "name": display_name(mid), "owner": "", "context": prefs["contexts"].get(mid),
                  "recommended": False, "enabled": True, "listed": False} for mid in enabled if mid not in known]
        return 200, {"provider": provider.id, "label": label, "models": extra + rows,
                     "enabled": enabled, "has_models": has_models}
    return 404, {"error": "not found"}


def is_post_route(path: str) -> bool:
    parts = _parts(path)
    return len(parts) == 4 and parts[:2] == ["api", "remote"] and parts[3] in ("key", "models")


def handle_post(server: Any, path: str, payload: dict) -> Reply:
    parts = _parts(path)
    found = _provider_or_404(parts[2])
    if not isinstance(found, Provider):
        return found
    provider = found
    if parts[3] == "key":
        return _save_key(server, provider, payload)
    return _save_models(server, provider, payload)


def _save_key(server: Any, provider: Provider, payload: dict) -> Reply:
    settings = settings_for(server)
    active = _active(server)  # before the save: the old client stops being usable
    raw = payload.get("key")
    base_url = None
    name = ""
    if provider.custom:
        try:
            base_url = registry.clean_custom_url(payload.get("base_url"))
        except BadBaseUrl as exc:
            return 400, {"error": str(exc), "field": "base_url"}
        name = str(payload.get("name") or "").strip()[:60]
        if any(ord(ch) < 32 for ch in name):
            return 400, {"error": "The name cannot contain control characters.", "field": "name"}
    keyless = False
    keep = False
    key = ""
    has_text = isinstance(raw, str) and bool(raw.strip())
    if provider.custom and not has_text:
        stored = settings.key(provider)
        old_url = settings.base_url(provider)
        if payload.get("no_key") or not stored:
            keyless = True
        elif old_url and registry.custom_origin(old_url) == registry.custom_origin(base_url or ""):
            keep, key = True, stored  # same scheme, host and port: the saved key may stay
        else:
            old = registry.custom_origin(old_url).split("://")[-1] or "the old server"
            return 400, {"error": "Paste the key again: the server address changed, and the saved key "
                                  f"is only ever sent to {old}.", "field": "key"}
    if not keyless and not keep:
        try:
            key = validate_key(raw)
        except InvalidKey as exc:
            return 400, {"error": f"That key cannot be used: {exc}.", "field": "key"}
        problem = registry.key_problem(key)
        if problem:
            return 400, {"error": f"That key cannot be used: {problem}.", "field": "key"}
        mismatch = registry.key_mismatch(provider, key)
        if mismatch:
            return 400, {"error": mismatch, "field": "key"}
    label = name or settings.label(provider) if provider.custom else provider.label
    try:
        probe = build_client(settings, provider, key=key, base_url=base_url)
    except RemoteError as exc:
        return 400, {"error": str(exc), "field": "base_url" if provider.custom else "key"}
    verified, warning = True, ""
    try:
        probe.verify(key or None, provider.verify)
    except RemoteError as exc:
        if exc.kind in ("key", "host", "redirect"):
            return 400, {"error": str(exc), "kind": exc.kind, "field": "key" if exc.kind == "key" else "base_url"}
        if exc.kind == "no_models":
            warning = (f"Saved. {label} has no model list, so type the model ids it serves "
                       "in Choose models.")
        else:
            # Offline or the provider is busy: keep the key, say it is unverified.
            verified, warning = False, f"Saved, but not verified yet: {exc}"
    try:
        if provider.custom:
            # Order matters (G5): drop the old key and its origin before the
            # address changes, store the new key, and only then record the
            # origin it belongs to. A failure part-way leaves no key usable.
            origin = registry.custom_origin(base_url or "")
            if not keep:
                settings.update_provider(provider, key_origin=None)
                settings.clear_key(provider)
            settings.update_provider(provider, base_url=base_url, name=name or None, keyless=keyless or None)
            if not keyless and not keep:
                settings.set_key(provider, key)
            if not keyless:
                settings.update_provider(provider, key_origin=origin)
        elif not keyless and not keep:
            settings.set_key(provider, key)
    except Exception as exc:  # noqa: BLE001 - the store's message never holds the key
        return 500, {"error": f"could not store the key ({type(exc).__name__})"}
    _forget_client(server, provider)
    if active and active[0] == provider.id:
        # The engine still holds the client built for the old address or name.
        if not _activate(server, provider, active[1]):
            engine = _engine(server)
            if engine is not None and hasattr(engine, "use_local"):
                engine.use_local()
    if not settings.provider_prefs(provider)["explicit"]:
        _default_models(server, provider)
    _record(server, f"{provider.id} key saved {redact(key) if key else '(no key)'}"
                    f"{'' if verified else ' (unverified)'}")
    return 200, dict(settings.view(provider, _engine(server)), verified=verified, warning=warning)


def _default_models(server: Any, provider: Provider) -> None:
    """First connect: switch on the recommended models this key's catalogue offers."""
    settings = settings_for(server)
    wanted = list(provider.recommended)
    client = client_for(server, provider)
    enabled = wanted[:provider.default_count]
    contexts: dict[str, int] = {}
    if client is not None:
        try:
            catalogue = settings.catalogue(provider, client, refresh=True)
            offered = {m["id"]: m for m in catalogue}
            if offered:
                enabled = [mid for mid in wanted if mid in offered][:provider.default_count]
                if not enabled:  # none of the suggestions: the first few the server lists
                    enabled = [m["id"] for m in catalogue[:provider.default_count]]
                contexts = {mid: offered[mid]["context"] for mid in enabled if offered[mid].get("context")}
        except RemoteError:
            pass
    settings.update_provider(provider, enabled=enabled, contexts=contexts or None)


def _save_models(server: Any, provider: Provider, payload: dict) -> Reply:
    settings = settings_for(server)
    enabled = payload.get("enabled")
    if not isinstance(enabled, list) or not all(isinstance(m, str) for m in enabled):
        return 400, {"error": "enabled must be a list of model ids"}
    cleaned: list[str] = []
    for model_id in enabled:
        model_id = model_id.strip()
        if model_id.startswith(provider.id + ":"):
            model_id = model_id[len(provider.id) + 1:]
        model_id = _clean_model_id(model_id)
        if not model_id or model_id in cleaned:
            continue
        if not provider.custom and not is_chat_model(model_id):
            continue
        cleaned.append(model_id)
    cleaned = cleaned[:MAX_ENABLED]
    known = {m["id"]: m for m in settings.cached_catalogue(provider)}
    contexts = dict(settings.provider_prefs(provider)["contexts"])
    for model_id in cleaned:
        context = (known.get(model_id) or {}).get("context")
        if context:
            contexts[model_id] = int(context)
    contexts = {k: v for k, v in contexts.items() if k in cleaned}
    settings.update_provider(provider, enabled=cleaned, contexts=contexts)
    return 200, settings.view(provider, _engine(server))


def handle_delete(server: Any, path: str) -> Reply | None:
    parts = _parts(path)
    if len(parts) != 4 or parts[:2] != ["api", "remote"] or parts[3] != "key":
        return None
    found = _provider_or_404(parts[2])
    if not isinstance(found, Provider):
        return found
    provider = found
    settings = settings_for(server)
    was_active = (_active(server) or ("", ""))[0] == provider.id
    settings.clear_key(provider)
    if provider.custom:
        settings.reset_provider(provider)
    elif str(settings.selected() or "").startswith(provider.id + ":"):
        settings.set_selected(None)
    _forget_client(server, provider)
    engine = _engine(server)
    if was_active and engine is not None and hasattr(engine, "use_local"):
        engine.use_local()
        _publish(server, "model.switch.done", {"model_name": _safe_name(engine), "remote": False})
    _record(server, f"{provider.id} key removed")
    return 200, settings.view(provider, engine)


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
