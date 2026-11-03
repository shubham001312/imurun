"""GPU helpers for the voice worker (stdlib + ctypes only; safe to import anywhere).

* ``prepare_cuda_path`` - CTranslate2 4.x loads ``cublas64_12.dll`` /
  ``libcublas.so.12`` lazily with the plain OS search. The pip wheels
  (``nvidia-cublas-cu12``) put it under ``site-packages/nvidia/cublas/bin``
  (``lib`` on Linux), which is on no search path. ``os.add_dll_directory`` is
  NOT enough on Windows (measured: still "cublas64_12.dll is not found"), so
  the directories are prepended to ``PATH`` (and ``LD_LIBRARY_PATH`` for any
  child) before the first CUDA call. cuDNN 9 ships inside the ctranslate2
  wheel on Windows.
* ``memory`` - free/total VRAM through NVML (``nvml.dll`` comes with the
  NVIDIA driver), falling back to ``nvidia-smi``. Used to keep the FISH
  engine's VRAM: the voice model only goes to the GPU when it fits with
  headroom (docs/voice/README.md "GPU rules").
"""
from __future__ import annotations

import ctypes
import os
import shutil
import site
import subprocess
import sys
from pathlib import Path

# VRAM a loaded model needs on top of what was free before (CUDA context +
# weights + one beam-5 decode of a 30 s window), measured on an RTX 3050 4 GB
# with int8_float16 (figures from the 2026-11-02 run; no results file kept). Unknown sizes use the
# turbo figure.
# Peaks measured 2026-11-02: small 343 MB, large-v3-turbo 974 MB (float16:
# 1443 MB). Deleting the model returns all of it (CTranslate2 frees the
# context too), so an in-process release is enough - no process kill needed.
NEED_MB = {"tiny": 200, "base": 250, "small": 400, "medium": 1000,
           "large-v3": 1800, "large-v3-turbo": 1000}
HEADROOM_MB = 300   # left free for the FISH engine and the desktop compositor

_PATH_DONE = False


def _site_dirs() -> list[Path]:
    dirs = []
    for item in [*getattr(site, "getsitepackages", lambda: [])(), getattr(site, "getusersitepackages", lambda: "")()]:
        if item:
            dirs.append(Path(item))
    dirs.append(Path(sys.prefix) / "Lib" / "site-packages")
    seen, out = set(), []
    for item in dirs:
        key = str(item).lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def cuda_library_dirs() -> list[str]:
    found = []
    for base in _site_dirs():
        root = base / "nvidia"
        if not root.is_dir():
            continue
        for package in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            for leaf in ("bin", "lib"):
                path = root / package / leaf
                if path.is_dir() and str(path) not in found:
                    found.append(str(path))
    return found


def prepare_cuda_path() -> list[str]:
    global _PATH_DONE
    dirs = cuda_library_dirs()
    if _PATH_DONE or not dirs:
        return dirs
    current = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join([*dirs, current]) if current else os.pathsep.join(dirs)
    if os.name != "nt":
        ld = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join([*dirs, ld]) if ld else os.pathsep.join(dirs)
    _PATH_DONE = True
    return dirs


def cublas_loadable() -> tuple[bool, str]:
    prepare_cuda_path()
    names = ["cublas64_12.dll"] if os.name == "nt" else ["libcublas.so.12", "libcublas.so"]
    errors = []
    for name in names:
        candidates = [name] + [str(Path(d) / name) for d in cuda_library_dirs()]
        for candidate in candidates:
            try:
                ctypes.CDLL(candidate)
                return True, ""
            except OSError as exc:
                errors.append(str(exc))
    return False, ("cuBLAS for CUDA 12 not found (pip install nvidia-cublas-cu12; "
                   "see requirements-voice.txt)")


class _NvmlMemory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


def memory(index: int = 0) -> dict:
    """{"free_mb", "total_mb", "source"} or {} when no NVIDIA GPU is visible."""
    try:
        lib = ctypes.CDLL("nvml.dll" if os.name == "nt" else "libnvidia-ml.so.1")
        if lib.nvmlInit_v2() == 0:
            try:
                handle = ctypes.c_void_p()
                if lib.nvmlDeviceGetHandleByIndex_v2(ctypes.c_uint(index), ctypes.byref(handle)) == 0:
                    info = _NvmlMemory()
                    if lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(info)) == 0:
                        return {"free_mb": int(info.free // (1024 * 1024)),
                                "total_mb": int(info.total // (1024 * 1024)), "source": "nvml"}
            finally:
                lib.nvmlShutdown()
    except (OSError, AttributeError):
        pass
    smi = shutil.which("nvidia-smi")
    if not smi:
        return {}
    try:
        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        out = subprocess.run([smi, "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, check=False, **kwargs).stdout
        first = out.strip().splitlines()[index]
        free, total = (int(x.strip()) for x in first.split(","))
        return {"free_mb": free, "total_mb": total, "source": "nvidia-smi"}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return {}


def need_mb(model: str) -> int:
    return NEED_MB.get(model, NEED_MB["large-v3-turbo"])


def fits(model: str, free_mb: int | None, headroom_mb: int | None = None) -> bool:
    """``headroom_mb`` is the resource governor's VRAM headroom (budget.vram_headroom)
    when the server passes it; the larger of it and HEADROOM_MB applies."""
    if free_mb is None:
        return True   # unknown (no NVML): let CUDA try, the worker falls back on OOM
    return free_mb >= need_mb(model) + max(HEADROOM_MB, int(headroom_mb or 0))
