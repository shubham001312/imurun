"""Model / voice downloads with byte progress, resumable via ``.part`` files.

Stdlib only (urllib). Files land in the voice data root; a download is
written to ``<file>.part`` and renamed only when complete, so an interrupted
download is never mistaken for an installed model. Progress goes to a
callback (the service turns it into ``voice.model.download`` SSE events).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HF = "https://huggingface.co"
USER_AGENT = "imurun-voice/1.0"
PIPER_REPO = "rhasspy/piper-voices"

# faster-whisper's Hugging Face repos (faster_whisper.utils._MODELS), copied so
# the server can list and download sizes without importing the package.
WHISPER_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
# The files faster-whisper needs (it downloads the same allow-list).
WHISPER_FILES = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.txt",
                 "vocabulary.json")
WHISPER_REQUIRED = ("config.json", "model.bin", "tokenizer.json")
# Approximate download sizes (model.bin dominates), for the picker.
WHISPER_SIZES_MB = {"tiny": 75, "base": 145, "small": 484, "medium": 1530, "large-v3": 3090,
                    "large-v3-turbo": 1620}

Progress = Callable[[dict], None]


class DownloadError(RuntimeError):
    pass


def hf_url(repo: str, path: str) -> str:
    return f"{HF}/{repo}/resolve/main/{path}"


def fetch_json(url: str, timeout: float = 30.0):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https host
        return json.loads(response.read().decode("utf-8"))


def fetch_file(url: str, target: Path, *, on_bytes: Callable[[int, int], None] | None = None,
               cancel: threading.Event | None = None, timeout: float = 60.0) -> int:
    """Stream ``url`` to ``target`` (via ``target.part``, resuming it). Returns bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:  # already complete
            os.replace(part, target)
            return have
        if exc.code == 404:
            raise DownloadError(f"not found: {url}") from exc
        raise DownloadError(f"HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DownloadError(f"network error for {url}: {exc}") from exc
    with response:
        if have and response.status != 206:
            have = 0  # server ignored the range: start over
        total = int(response.headers.get("Content-Length") or 0) + have
        mode = "ab" if have else "wb"
        done = have
        with open(part, mode) as out:
            while True:
                if cancel is not None and cancel.is_set():
                    raise DownloadError("cancelled")
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if on_bytes is not None:
                    on_bytes(done, total)
    if total and done < total:
        raise DownloadError(f"incomplete download of {url} ({done}/{total} bytes)")
    os.replace(part, target)
    return done


@dataclass
class Job:
    """One install (a whisper model or a piper voice) and its progress."""
    id: str
    kind: str            # "whisper" | "piper"
    name: str            # model size or voice key
    state: str = "queued"  # queued | downloading | done | error
    done: int = 0
    total: int = 0
    file: str = ""
    error: str = ""
    started: float = field(default_factory=time.time)

    def view(self) -> dict:
        return {"id": self.id, "kind": self.kind, "name": self.name, "state": self.state,
                "done": self.done, "total": self.total, "file": self.file, "error": self.error,
                "percent": round(100.0 * self.done / self.total, 1) if self.total else None}


def download_files(job: Job, files: list[tuple[str, Path, int]], publish: Progress,
                   cancel: threading.Event | None = None) -> None:
    """Download (url, target, expected_size) entries, publishing throttled progress."""
    job.state = "downloading"
    job.total = sum(size for _, _, size in files) or 0
    base = 0
    last = [0.0]

    def tick(done: int, total: int, *, force: bool = False) -> None:
        job.done = base + done
        if not job.total:
            job.total = total
        now = time.monotonic()
        if force or now - last[0] >= 0.5:
            last[0] = now
            publish(job.view())

    publish(job.view())
    for url, target, size in files:
        job.file = target.name
        if target.is_file() and (not size or target.stat().st_size == size):
            base += target.stat().st_size
            tick(0, 0, force=True)
            continue
        written = fetch_file(url, target, on_bytes=lambda d, t: tick(d, t), cancel=cancel)
        base += written
        tick(0, 0, force=True)
    job.state = "done"
    job.done = job.total = max(job.total, base)
    publish(job.view())
