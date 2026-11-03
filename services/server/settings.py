"""Durable JSON settings sections for the shell.

Every settings panel reads and writes one section. The store validates the
section name so a request can never escape the settings directory, and it
writes atomically so a crash mid-save cannot corrupt the file.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path

from ..voice.service import DEFAULTS as VOICE_DEFAULTS

SECTION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

DEFAULTS: dict[str, dict] = {
    "general": {
        "profileName": "",
        "emailRole": "",
        "personaDescription": "",
    },
    "profile": {
        "name": "",
        "email": "",
        "role": "Local",
        "timezone": "",
    },
    "server": {
        "port": 8732,
        "logLevel": "Info",
        "contextSize": 8192,
        "gpuLayers": -1,
        "threads": 0,
        "loadMode": "mmap",
        "lazyMode": "auto",
        "mmap": True,
        "flashAttention": True,
        "kvCacheType": "f16",
        "maxTokens": 2048,
        "temperature": 0.7,
        "topP": 0.9,
        "repeatPenalty": 1.1,
        "seed": -1,
        "thinkingLevel": "balanced",
        "startWithShell": True,
    },
    "permissions": {
        "readFiles": True,
        "writeFiles": True,
        "runShell": True,
        "network": True,
        "sendEmail": True,
    },
    "customisation": {
        "theme": "liquid-paper",
        "font": "manrope",
        "backgroundMotion": "full",
        "ambientMode": "mixed",
        "glassIntensity": "frosted",
        "density": "comfortable",
        "accentColor": "#b45309",
        "notificationDuration": 4500,
        "reducedMotion": False,
    },
    "resources": {
        "cacheLimitGb": 40,
        "transcriptDays": 30,
        "logDays": 7,
        # Resource governor (services/resources, docs/resources/README.md):
        # balanced | performance (half the headroom) | gentle (double) | custom.
        "headroom": "balanced",
        # custom only: per-resource headroom percentages {cpu, ram, vram, disk}.
        "custom": {},
        # Lower IMURUN's threads while other programs are busy.
        "dynamic": True,
    },
    # --- tuning channel (services/tuning, docs/tuning/README.md) -------------
    # Off by default; validated by services/tuning/scheduler.validate_settings.
    "tuning": {
        "enabled": False,
        "auto": "off",               # off | nightly | when-idle | every-N-approved-samples
        "min_samples": 200,
        "max_hours": 4.0,
        "allow_8b_slow": False,
        "quant": "Q4_K_M",           # Q4_K_M | Q5_K_M | Q6_K | Q8_0 | F16
        "model": "selected",         # "selected" or a catalogue id
        "epochs": 2,
        "idle_minutes": 15,
        "nightly_hour": 2,
        "include_pii": False,
        "auto_download": False,
        "max_seq_len": 512,
    },
    # Hugging Face token for gated original weights (tuning) - write-only like
    # the search keys: GET returns a mask, never the value.
    "huggingface": {"hf_token": ""},
    # --- end tuning ----------------------------------------------------------
    # Server-side voice (services/voice, docs/voice/README.md); the first four
    # keys are the shell's browser-voice settings (frontend API-REQUESTS #4).
    "voice": copy.deepcopy(VOICE_DEFAULTS),
    # Web search providers (see services/tools/builtin/web_search.py). Keys are
    # write-only through the API: GET returns a mask, never the value, and the
    # server exports them to the environment the search tool reads.
    "search": {
        "provider": "",            # "" = auto: first key present, else keyless
        "IMURUN_BRAVE_API_KEY": "",
        "IMURUN_TAVILY_API_KEY": "",
        "IMURUN_SERPER_API_KEY": "",
        "IMURUN_SEARXNG_URL": "",
    },
    # Connector settings (see docs/connectors/GMAIL.md). The Google OAuth
    # client secret is NOT kept here: POST /api/connectors/gmail/config puts it
    # in the connector token store (keychain / protected file).
    "connectors": {
        "gmail": {
            "clientId": "",
            "tools": [
                "gmail_search", "gmail_read", "gmail_labels", "gmail_draft",
                "gmail_send", "gmail_modify", "gmail_trash",
            ],
            "maxResults": 10,
            "bodyChars": 4000,
        },
        # One entry per connector: the user's own OAuth app client id. A
        # missing "tools" list means every tool of that connector is on.
        "github": {"clientId": ""},
        "slack": {"clientId": ""},
        "vercel": {"clientId": ""},
        "render": {"clientId": ""},
        "cloudflare": {"clientId": ""},
        "supabase": {"clientId": "", "keyRole": "anon"},
        "canva": {"clientId": ""},
    },
    # MCP servers (see docs/connectors/MCP.md). The built-in Gmail MCP server
    # ships as an example entry that is off until the user enables it.
    "mcp": {
        "servers": [
            {
                "id": "gmail",
                "name": "Gmail (built-in MCP server)",
                "transport": "stdio",
                "command": "${python}",
                "args": ["${repo}/services/mcp/servers/gmail_mcp.py", "--home", "${home}"],
                "env": {},
                "url": "",
                "headers": {},
                "enabled": False,
                "auto_approve": [],
            },
            {
                "id": "connectors",
                "name": "IMURUN connectors (built-in MCP server)",
                "transport": "stdio",
                "command": "${python}",
                "args": ["${repo}/services/mcp/servers/connectors_mcp.py", "--home", "${home}",
                         "--services", "github,slack,vercel,render,cloudflare,supabase,canva"],
                "env": {},
                "url": "",
                "headers": {},
                "enabled": False,
                "auto_approve": [],
            },
        ],
    },
}


SEARCH_KEYS = ("IMURUN_BRAVE_API_KEY", "IMURUN_TAVILY_API_KEY", "IMURUN_SERPER_API_KEY", "IMURUN_SEARXNG_URL")
SECRET_FIELDS: dict[str, tuple[str, ...]] = {"search": SEARCH_KEYS, "huggingface": ("hf_token",)}


def mask_secrets(section: str, values: dict) -> dict:
    """What the API may return: secrets become "set"/"" markers."""
    fields = SECRET_FIELDS.get(section)
    if not fields:
        return values
    out = dict(values)
    for key in fields:
        if key in out:
            out[key] = "••••••••" if str(out.get(key) or "").strip() else ""
    return out


def apply_search_env(values: dict) -> None:
    """Export the stored search keys to the environment the tools read.

    An empty stored value never clears a key the user set in the shell; the
    masked placeholder is never written (it comes back from careless clients).
    """
    for key in SEARCH_KEYS:
        value = str(values.get(key) or "").strip()
        if value and value != "••••••••":
            os.environ[key] = value
    provider = str(values.get("provider") or "").strip().lower()
    if provider:
        os.environ["IMURUN_SEARCH_PROVIDER"] = provider
    elif "IMURUN_SEARCH_PROVIDER" in os.environ and values.get("provider") == "":
        os.environ.pop("IMURUN_SEARCH_PROVIDER", None)


class SettingsStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Keys saved in an earlier session take effect at startup.
        try:
            apply_search_env(self.read("search"))
        except Exception:  # noqa: BLE001 - a corrupt file must not stop the server
            pass

    def _path(self, section: str) -> Path:
        if not SECTION_RE.match(section or ""):
            raise ValueError("invalid settings section")
        return self.root / f"{section}.json"

    def read(self, section: str) -> dict:
        # Deep copy: the connectors / mcp defaults hold nested dicts and lists
        # that a caller must never be able to mutate in place.
        default = copy.deepcopy(DEFAULTS.get(section, {}))
        path = self._path(section)
        if not path.is_file():
            return default
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        if not isinstance(data, dict):
            return default
        default.update(data)
        return default

    def write(self, section: str, values: dict) -> dict:
        if not isinstance(values, dict):
            raise ValueError("settings must be an object")
        # A client echoing the mask back must never overwrite a stored secret.
        secret = SECRET_FIELDS.get(section, ())
        values = {k: v for k, v in values.items() if not (k in secret and v == "••••••••")}
        with self._lock:
            merged = self.read(section)
            merged.update(values)
            path = self._path(section)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(merged, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return merged

    def sections(self) -> list[str]:
        known = set(DEFAULTS)
        for path in self.root.glob("*.json"):
            if SECTION_RE.match(path.stem):
                known.add(path.stem)
        return sorted(known)
