"""Download the original (trainable) weights for a model.

Into ``~/.imurun/tuning/base/<catalogue_id>/``: the ``*.safetensors`` shards,
``config.json``, ``generation_config.json`` and the tokenizer files. Meta's
``original/`` folder (a duplicate ``.pth`` copy) and Mistral's
``consolidated.safetensors`` duplicate are skipped. Downloads go through
``huggingface_hub.hf_hub_download`` which resumes partial files.

Gated repos (Meta Llama, Gemma, Mistral) need the user's Hugging Face token
(Settings -> ``huggingface.hf_token``, or ``HF_TOKEN``) and an accepted
licence on huggingface.co. Without one, the ungated mirror listed in
``catalogue.py`` is tried; the result records which repo was used.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from .catalogue import BaseModel

WANTED_SUFFIXES = (".safetensors", ".json", ".model", ".tiktoken", ".txt", ".jinja")
SKIP_PREFIXES = ("original/", ".")
SKIP_NAMES = ("consolidated.safetensors",)


class BaseUnavailable(RuntimeError):
    pass


def complete(directory: Path) -> bool:
    marker = directory / ".imurun-base.json"
    return marker.is_file() and any(directory.glob("*.safetensors"))


def info(directory: Path) -> dict:
    try:
        return json.loads((directory / ".imurun-base.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def wanted_files(siblings) -> list[tuple[str, int]]:
    files = []
    for item in siblings or []:
        name = getattr(item, "rfilename", None) or (item.get("rfilename") if isinstance(item, dict) else "")
        size = getattr(item, "size", None) or (item.get("size") if isinstance(item, dict) else 0) or 0
        if not name or "/" in name or name.startswith(SKIP_PREFIXES) or name in SKIP_NAMES:
            continue
        if name.endswith(WANTED_SUFFIXES):
            files.append((name, int(size)))
    return files


def remote_size(repo: str, token: str | None) -> int:
    from huggingface_hub import HfApi

    meta = HfApi().model_info(repo, files_metadata=True, token=token or None)
    return sum(size for _name, size in wanted_files(meta.siblings))


def _refused(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(s in text for s in ("gated", "401", "403", "unauthorized", "forbidden", "accept", "access"))


def download(
    base: BaseModel,
    directory: Path,
    *,
    token: str | None = None,
    progress: Callable[[str, float, float], None] = lambda msg, pct, eta: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict:
    """Download (or resume) the base weights. Returns the recorded info."""
    if complete(directory):
        return info(directory)
    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError as exc:  # pragma: no cover - the app depends on it
        raise BaseUnavailable("huggingface_hub is not installed in the app venv") from exc
    token = token or os.getenv("HF_TOKEN") or None
    errors = []
    for repo in (base.repo, *base.mirrors):
        try:
            meta = HfApi().model_info(repo, files_metadata=True, token=token)
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            errors.append(f"{repo}: {'licence-gated (needs a Hugging Face token)' if _refused(exc) else type(exc).__name__}")
            continue
        files = wanted_files(meta.siblings)
        if not any(name.endswith(".safetensors") for name, _ in files):
            errors.append(f"{repo}: no safetensors weights")
            continue
        total = sum(size for _, size in files) or 1
        directory.mkdir(parents=True, exist_ok=True)
        done = 0
        started = time.time()
        stop = threading.Event()

        def watch(name: str, size: int, before: int) -> None:
            # hf_hub_download has no byte callback; poll the partial file (it is
            # named by hash, and files download one at a time, so take them all).
            cache = directory / ".cache" / "huggingface" / "download"
            while not stop.wait(2.0):
                try:
                    partial = sum(p.stat().st_size for p in cache.glob("*.incomplete")) if cache.is_dir() else 0
                except OSError:
                    partial = 0
                now = before + min(partial, size)
                rate = now / max(1e-6, time.time() - started)
                progress(f"downloading {repo}/{name}", 100.0 * now / total,
                         (total - now) / rate if rate > 0 else None)

        try:
            for name, size in files:
                if cancelled():
                    raise BaseUnavailable("cancelled")
                stop.clear()
                watcher = threading.Thread(target=watch, args=(name, size, done), daemon=True)
                watcher.start()
                try:
                    hf_hub_download(repo, name, local_dir=str(directory), token=token)
                finally:
                    stop.set()
                    watcher.join(timeout=3)
                done += size
                progress(f"downloaded {name}", 100.0 * done / total, None)
        except BaseUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            if _refused(exc):
                errors.append(f"{repo}: licence-gated (needs a Hugging Face token)")
                continue
            raise BaseUnavailable(f"download from {repo} failed: {type(exc).__name__}: {exc}") from exc
        record = {"repo": repo, "official": repo == base.repo, "bytes": total,
                  "files": [n for n, _ in files], "at": time.time(),
                  "seconds": round(time.time() - started, 1)}
        (directory / ".imurun-base.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record
    raise BaseUnavailable("could not download the original weights: " + "; ".join(errors))
