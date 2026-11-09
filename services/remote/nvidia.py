"""NVIDIA-hosted models: kept for older imports.

The NVIDIA-only code grew into every provider; it now lives in
``services/remote/hosted.py`` (routes, settings, selection) and
``services/remote/providers.py`` (the registry). Everything that worked
before still does: the ``/api/remote/nvidia...`` routes are the general
``/api/remote/<provider>...`` routes with ``nvidia``, the key stays in the
secret store under ``model.nvidia``, ids ``nvidia:<model>`` still select,
and the version-1 ``remote_models.json`` is read as NVIDIA's preferences.
"""
from __future__ import annotations

from .hosted import (RemoteSettings, attach, handle_delete, handle_get, handle_post, is_post_route,
                     local_selected, model_entries, select, settings_for)
from .providers import PROVIDERS

SECRET_NAME = "model.nvidia"
ID_PREFIX = "nvidia:"
PROVIDER = PROVIDERS["nvidia"]

__all__ = ["ID_PREFIX", "PROVIDER", "RemoteSettings", "SECRET_NAME", "attach", "handle_delete",
           "handle_get", "handle_post", "is_post_route", "local_selected", "model_entries", "select",
           "settings_for"]
