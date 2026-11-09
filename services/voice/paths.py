"""Where the voice layer keeps its files (never the working directory).

    <data root>/voice/                 (default ~/.imurun/voice, or IMURUN_VOICE_HOME)
      models/whisper/<size>/           faster-whisper CTranslate2 model files
      voices/piper/<voice key>/        <key>.onnx + <key>.onnx.json
      cache/piper-voices.json          the Piper catalogue, cached after first fetch
      tmp/                             scratch audio for system-voice engines (deleted after use)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def safe_name(value: str) -> str:
    cleaned = _SAFE.sub("_", str(value or "")).strip("._")
    return cleaned[:120] or "unnamed"


@dataclass(frozen=True)
class VoicePaths:
    root: Path

    @classmethod
    def at(cls, data_root: Path | str | None = None) -> "VoicePaths":
        override = os.getenv("IMURUN_VOICE_HOME", "").strip()
        if override:
            return cls(Path(override))
        base = Path(data_root) if data_root else Path(os.path.expanduser("~/.imurun"))
        return cls(base / "voice")

    @property
    def models(self) -> Path:
        return self.root / "models"

    def whisper_dir(self, size: str) -> Path:
        return self.models / "whisper" / safe_name(size)

    def piper_dir(self, key: str) -> Path:
        return self.root / "voices" / "piper" / safe_name(key)

    def piper_model(self, key: str) -> Path:
        return self.piper_dir(key) / f"{safe_name(key)}.onnx"

    def piper_config(self, key: str) -> Path:
        return self.piper_dir(key) / f"{safe_name(key)}.onnx.json"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def catalogue(self) -> Path:
        return self.cache / "piper-voices.json"

    @property
    def tmp(self) -> Path:
        return self.root / "tmp"

    def ensure(self) -> "VoicePaths":
        for directory in (self.root, self.models, self.cache, self.tmp):
            directory.mkdir(parents=True, exist_ok=True)
        return self
