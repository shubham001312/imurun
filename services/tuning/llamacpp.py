"""Fetch and locate the llama.cpp conversion tools for export.

What is downloaded (once, into ``~/.imurun/tuning/tools/llama.cpp/``), all
from one llama.cpp GitHub release (``IMURUN_LLAMACPP_TAG`` pins a tag;
otherwise the latest release at setup time is recorded in ``VERSION.json``
and reused from then on):

1. The release's source archive
   ``https://github.com/ggml-org/llama.cpp/archive/refs/tags/<tag>.zip``.
   Only the top-level ``*.py`` scripts (``convert_hf_to_gguf.py``,
   ``convert_lora_to_gguf.py`` and the helpers they import) and the
   ``gguf-py/`` package are extracted; no C++ is built.
2. The prebuilt CPU binary archive for this OS
   (``llama-<tag>-bin-win-cpu-x64.zip`` on Windows, ``...-bin-ubuntu-x64.zip``
   on Linux, ``...-bin-macos-arm64.zip`` / ``-x64`` on macOS). Only
   ``llama-quantize`` and the shared libraries next to it are extracted.

Nothing else from llama.cpp is used; inference stays on the FISH engine.
"""
from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

API = "https://api.github.com/repos/ggml-org/llama.cpp/releases"
LAYOUT = 2      # bump when the set of extracted files changes (forces a re-fetch)
SOURCE = "https://github.com/ggml-org/llama.cpp/archive/refs/tags/{tag}.zip"


@dataclass
class Tools:
    root: Path
    tag: str = ""
    layout: int = 0
    convert_hf: Path | None = None
    convert_lora: Path | None = None
    quantize: Path | None = None

    @property
    def ready(self) -> bool:
        return bool(self.convert_hf and self.convert_hf.is_file()
                    and self.convert_lora and self.convert_lora.is_file())

    def view(self) -> dict:
        return {"root": str(self.root), "tag": self.tag, "converters": self.ready,
                "quantize": bool(self.quantize and Path(self.quantize).is_file())}


def locate(root: Path) -> Tools:
    root = Path(root)
    tools = Tools(root=root)
    info = root / "VERSION.json"
    if info.is_file():
        try:
            record = json.loads(info.read_text(encoding="utf-8"))
            tools.tag = record.get("tag", "")
            tools.layout = int(record.get("layout") or 0)
        except (OSError, ValueError):
            pass
    src = root / "src"
    if (src / "convert_hf_to_gguf.py").is_file():
        tools.convert_hf = src / "convert_hf_to_gguf.py"
    if (src / "convert_lora_to_gguf.py").is_file():
        tools.convert_lora = src / "convert_lora_to_gguf.py"
    exe = "llama-quantize.exe" if os.name == "nt" else "llama-quantize"
    found = sorted((root / "bin").rglob(exe)) if (root / "bin").is_dir() else []
    for candidate in [root / "bin" / exe, *found]:
        if candidate.is_file():
            tools.quantize = candidate
            break
    return tools


def _get(url: str, *, timeout: float = 60.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "imurun-tuning",
                                                   "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https hosts
        return response.read()


def _download(url: str, progress: Callable[[str, float], None], label: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "imurun-tuning"})
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        total = int(response.headers.get("Content-Length") or 0)
        buffer = io.BytesIO()
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            buffer.write(chunk)
            if total:
                progress(f"{label}: {buffer.tell() / 1e6:.0f} of {total / 1e6:.0f} MB",
                         100.0 * buffer.tell() / total)
        return buffer.getvalue()


def binary_asset_pattern() -> re.Pattern:
    system = platform.system().lower()
    machine = platform.machine().lower()
    archive = r"\.(zip|tar\.gz)$"
    if system == "windows":
        arch = "arm64" if "arm" in machine else "x64"
        return re.compile(rf"^llama-b?\d*-?bin-win-cpu-{arch}{archive}")
    if system == "darwin":
        arch = "arm64" if "arm" in machine else "x64"
        return re.compile(rf"^llama-b?\d*-?bin-macos-{arch}{archive}")
    return re.compile(rf"^llama-b?\d*-?bin-ubuntu-x64{archive}")


def pick_release(releases: list[dict], pattern: re.Pattern | None = None) -> dict | None:
    """Newest release that ships a CPU binary for this OS.

    llama.cpp publishes its ``bNNNNN`` builds as *pre-releases*, so
    ``/releases/latest`` returns an unrelated tag without binaries; list the
    releases instead and take the newest one with a matching asset.
    """
    pattern = pattern or binary_asset_pattern()
    for release in releases:
        if release.get("draft"):
            continue
        if any(pattern.search(a.get("name", "")) for a in release.get("assets", [])):
            return release
    return None


def _extract(blob: bytes, name: str, keep: re.Pattern):
    """(file name, bytes) for each wanted member of a .zip or .tar.gz."""
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for member in archive.infolist():
                base = Path(member.filename).name
                if not member.is_dir() and keep.match(base):
                    yield base, archive.read(member)
        return
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for member in archive.getmembers():
            base = Path(member.name).name
            if member.isfile() and keep.match(base):
                handle = archive.extractfile(member)
                if handle is not None:
                    yield base, handle.read()


def install(root: Path, progress: Callable[[str, float], None] = lambda m, p: None,
            tag: str | None = None) -> Tools:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    wanted = tag or os.getenv("IMURUN_LLAMACPP_TAG", "").strip()
    existing = locate(root)
    if existing.ready and existing.quantize and existing.layout == LAYOUT and (not wanted or wanted == existing.tag):
        return existing
    pinned = wanted or (existing.tag if existing.tag and existing.quantize else "")
    if pinned:
        release = json.loads(_get(f"{API}/tags/{pinned}"))
    else:
        release = pick_release(json.loads(_get(f"{API}?per_page=20")))
        if release is None:
            raise RuntimeError("no recent llama.cpp release ships a CPU binary for this OS")
    tag = release["tag_name"]
    record = {"tag": tag, "layout": LAYOUT, "fetched_at": time.time(), "files": []}

    progress(f"llama.cpp {tag}: converter scripts", 0.0)
    data = _download(SOURCE.format(tag=tag), progress, "llama.cpp source")
    record["files"].append({"url": SOURCE.format(tag=tag), "bytes": len(data)})
    src = root / "src"
    shutil.rmtree(src, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [m.filename.split("/", 1)[1] for m in archive.infolist() if "/" in m.filename]
        # Top-level Python packages the converters import (e.g. `conversion/`).
        packages = {n.split("/", 1)[0] for n in names if n.count("/") == 1 and n.endswith("/__init__.py")}
        for member in archive.infolist():
            parts = member.filename.split("/", 1)
            if len(parts) < 2 or not parts[1] or member.is_dir():
                continue
            rel = parts[1]
            top_py = "/" not in rel and rel.endswith(".py")
            in_package = rel.split("/", 1)[0] in packages and rel.endswith(".py")
            if not (top_py or in_package or rel.startswith("gguf-py/")):
                continue
            target = (src / rel).resolve()
            if not str(target).startswith(str(src.resolve())):
                continue           # zip-slip guard
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))

    pattern = binary_asset_pattern()
    asset = next((a for a in release.get("assets", []) if pattern.search(a.get("name", ""))), None)
    if asset is not None:
        progress(f"llama.cpp {tag}: {asset['name']}", 0.0)
        blob = _download(asset["browser_download_url"], progress, asset["name"])
        record["files"].append({"url": asset["browser_download_url"], "bytes": len(blob)})
        bin_dir = root / "bin"
        shutil.rmtree(bin_dir, ignore_errors=True)
        bin_dir.mkdir(parents=True)
        keep = re.compile(r"(llama-quantize(\.exe)?|.*\.(dll|so(\.\d+)*|dylib))$", re.I)
        for name, content in _extract(blob, asset["name"], keep):
            target = bin_dir / name
            target.write_bytes(content)
            if os.name != "nt":
                target.chmod(0o755)
    else:
        record["note"] = f"no prebuilt binary matching {pattern.pattern}; only Q8_0 / F16 export works"
    (root / "VERSION.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return locate(root)
