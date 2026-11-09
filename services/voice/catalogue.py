"""The Piper voice catalogue: which voices exist, per language, and their files.

Source of truth is ``voices.json`` in ``rhasspy/piper-voices`` on Hugging
Face (178 voices in 55 language families when checked on 2026-11-01). It is
fetched once and cached under ``<voice root>/cache/piper-voices.json``.

Offline and before the first fetch, ``BUILTIN`` is used: the voices this
layer recommends for the core languages, copied from that catalogue
(file paths and sizes verbatim). Verified present on 2026-11-01:
English (en_US / en_GB, many), German (de_DE, 10 voices), Hindi (hi_IN:
pratham, priyamvada, rohan), Bengali (bn_BD-google-medium, 16 speakers).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import download
from .languages import normalize
from .paths import VoicePaths

CATALOGUE_URL = download.hf_url(download.PIPER_REPO, "voices.json")


def _entry(key: str, quality: str, size: int, speakers: int = 1, native: str = "", english: str = "") -> dict:
    family_code = key.split("-")[0]           # "hi_IN"
    family = family_code.split("_")[0]        # "hi"
    voice_name = key.split("-")[1]
    base = f"{family}/{family_code}/{voice_name}/{quality}/{key}"
    return {
        "key": key, "name": voice_name, "quality": quality, "num_speakers": speakers,
        "language": {"code": family_code, "family": family, "name_native": native,
                     "name_english": english},
        "files": {f"{base}.onnx": {"size_bytes": size}, f"{base}.onnx.json": {"size_bytes": 0}},
    }


# Recommended default first per language. Sizes are from voices.json.
BUILTIN: dict[str, dict] = {item["key"]: item for item in (
    _entry("en_US-lessac-medium", "medium", 63201294, 1, "English", "English"),
    _entry("en_US-amy-medium", "medium", 63201294, 1, "English", "English"),
    _entry("en_GB-alba-medium", "medium", 63201294, 1, "English", "English"),
    _entry("de_DE-thorsten-medium", "medium", 63201294, 1, "Deutsch", "German"),
    _entry("de_DE-kerstin-low", "low", 63104526, 1, "Deutsch", "German"),
    _entry("hi_IN-priyamvada-medium", "medium", 63516050, 1, "हिन्दी", "Hindi"),
    _entry("hi_IN-pratham-medium", "medium", 63516050, 1, "हिन्दी", "Hindi"),
    _entry("hi_IN-rohan-medium", "medium", 62950044, 1, "हिन्दी", "Hindi"),
    _entry("bn_BD-google-medium", "medium", 76782515, 16, "বাংলা", "Bengali"),
)}
# The voice used for a language when the user has not chosen one.
DEFAULT_VOICE = {
    "en": "en_US-lessac-medium",
    "de": "de_DE-thorsten-medium",
    "hi": "hi_IN-priyamvada-medium",
    "bn": "bn_BD-google-medium",
}
_QUALITY_RANK = {"high": 0, "medium": 1, "low": 2, "x_low": 3}


class Catalogue:
    def __init__(self, paths: VoicePaths) -> None:
        self._paths = paths
        self._lock = threading.Lock()
        self._voices: dict[str, dict] | None = None
        self.source = "builtin"

    def voices(self, *, refresh: bool = False, allow_network: bool = True) -> dict[str, dict]:
        with self._lock:
            if self._voices is not None and not refresh:
                return self._voices
            data = None
            cached = self._paths.catalogue
            if cached.is_file() and not refresh:
                data = _load(cached)
                if data:
                    self.source = "cache"
            if data is None and allow_network:
                try:
                    data = download.fetch_json(CATALOGUE_URL, timeout=20.0)
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_text(json.dumps(data), encoding="utf-8")
                    self.source = "network"
                except Exception:  # noqa: BLE001 - offline: fall back to the built-in list
                    data = None
            if not isinstance(data, dict) or not data:
                data, self.source = dict(BUILTIN), "builtin"
            self._voices = data
            return data

    def get(self, key: str) -> dict | None:
        voices = self.voices(allow_network=False)
        return voices.get(key) or BUILTIN.get(key)

    def for_language(self, lang: str) -> list[dict]:
        code = normalize(lang)
        items = [v for v in self.voices(allow_network=False).values()
                 if (v.get("language") or {}).get("family") == code]
        default = DEFAULT_VOICE.get(code)
        items.sort(key=lambda v: (v.get("key") != default, _QUALITY_RANK.get(v.get("quality"), 9), v.get("key")))
        return items

    def languages(self) -> list[str]:
        return sorted({(v.get("language") or {}).get("family", "") for v in self.voices(allow_network=False).values()} - {""})

    def files(self, key: str) -> list[tuple[str, Path, int]]:
        entry = self.get(key)
        if entry is None:
            raise KeyError(f"unknown Piper voice {key!r}")
        out = []
        for rel, meta in (entry.get("files") or {}).items():
            if rel.endswith(".onnx"):
                out.append((download.hf_url(download.PIPER_REPO, rel), self._paths.piper_model(key),
                            int(meta.get("size_bytes") or 0)))
            elif rel.endswith(".onnx.json"):
                out.append((download.hf_url(download.PIPER_REPO, rel), self._paths.piper_config(key),
                            int(meta.get("size_bytes") or 0)))
        return out

    def installed(self, key: str) -> bool:
        return self._paths.piper_model(key).is_file() and self._paths.piper_config(key).is_file()


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
