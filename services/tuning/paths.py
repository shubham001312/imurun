"""Where the tuning channel keeps its files.

Everything lives under one root (default ``~/.imurun/tuning``) so a user can
see, back up or delete the whole channel in one place::

    tuning/
      venv/                 the separate training venv (torch, peft, ...)
      base/<model>/         original fp16/bf16 safetensors from Hugging Face
      datasets/             user-provided JSONL / CSV / Markdown files
      built/<build_id>.jsonl  datasets the channel assembled for a run
      runs/<run_id>/        config.json, metrics.jsonl, status.json, adapter/,
                            checkpoints/, merged/, export/
      tools/llama.cpp/      the pinned llama.cpp converter + llama-quantize
      manifest.json         sample hashes already trained, per base model
      registry.json         tuned models produced by the channel
      hardware.json         measured training throughput (tokens/s)

Feedback (thumbs / teach) is stored next to the transcripts, under
``<sessions>/feedback/<session>.jsonl``, because it belongs to the session.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_root() -> Path:
    override = os.getenv("IMURUN_TUNING_HOME", "").strip()
    if override:
        return Path(override)
    return Path(os.path.expanduser("~/.imurun/tuning"))


@dataclass(frozen=True)
class TuningPaths:
    root: Path

    @classmethod
    def at(cls, root: Path | str | None = None) -> "TuningPaths":
        return cls(Path(root) if root else default_root())

    @property
    def venv(self) -> Path:
        return self.root / "venv"

    @property
    def venv_python(self) -> Path:
        if os.name == "nt":
            return self.venv / "Scripts" / "python.exe"
        return self.venv / "bin" / "python"

    @property
    def base(self) -> Path:
        return self.root / "base"

    def base_dir(self, model_id: str) -> Path:
        return self.base / _safe(model_id)

    @property
    def datasets(self) -> Path:
        return self.root / "datasets"

    @property
    def built(self) -> Path:
        return self.root / "built"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    def run_dir(self, run_id: str) -> Path:
        return self.runs / _safe(run_id)

    @property
    def tools(self) -> Path:
        return self.root / "tools"

    @property
    def llama_cpp(self) -> Path:
        return self.tools / "llama.cpp"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def registry(self) -> Path:
        return self.root / "registry.json"

    @property
    def hardware(self) -> Path:
        return self.root / "hardware.json"

    @property
    def setup_log(self) -> Path:
        return self.root / "setup.json"

    def ensure(self) -> "TuningPaths":
        for directory in (self.root, self.datasets, self.runs, self.base, self.built):
            directory.mkdir(parents=True, exist_ok=True)
        return self


def _safe(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(name))
    cleaned = cleaned.strip("._") or "unnamed"
    return cleaned[:120]


def repo_root() -> Path:
    """The imurun checkout (services/tuning/paths.py -> imurun/)."""
    return Path(__file__).resolve().parents[2]


def models_dir() -> Path:
    """The app's models folder (``~/.imurun/models`` for an installed copy)."""
    from ..paths import models_dir as _shared

    return _shared()
