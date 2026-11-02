"""Live local resource sampling for the Resource manager panel.

Uses psutil when it is installed and falls back to platform calls so the panel
still reports real numbers on a bare machine. Sizes are measured from the data
root, never invented.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path


def _directory_size(path: Path, limit_files: int = 20000) -> int:
    total = 0
    seen = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            seen += 1
            if seen > limit_files:
                return total
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _memory() -> dict | None:
    try:
        import psutil  # type: ignore

        info = psutil.virtual_memory()
        return {"used": int(info.used), "total": int(info.total)}
    except Exception:
        pass

    if os.name == "nt":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total = int(status.ullTotalPhys)
                return {"used": total - int(status.ullAvailPhys), "total": total}
        except Exception:
            return None

    try:
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            values = {}
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                key, _, rest = line.partition(":")
                values[key.strip()] = int(rest.strip().split()[0]) * 1024
            total = values.get("MemTotal", 0)
            available = values.get("MemAvailable", values.get("MemFree", 0))
            return {"used": max(0, total - available), "total": total}
    except (OSError, ValueError, IndexError):
        pass
    return None


def _cpu_percent() -> float | None:
    try:
        import psutil  # type: ignore

        return float(psutil.cpu_percent(interval=0.1))
    except Exception:
        pass
    try:
        load = os.getloadavg()[0]
        count = os.cpu_count() or 1
        return round(min(100.0, (load / count) * 100.0), 1)
    except (AttributeError, OSError):
        return None


class ResourceSampler:
    def __init__(self, data_root: Path | str, models_root: Path | str | None = None) -> None:
        self.data_root = Path(data_root)
        self.models_root = Path(models_root) if models_root else self.data_root / "models"
        self._lock = threading.Lock()

    def view(self) -> dict:
        with self._lock:
            memory = _memory()
            cpu = _cpu_percent()
            try:
                disk = shutil.disk_usage(self.data_root if self.data_root.exists() else Path.home())
                disk_used, disk_total = int(disk.used), int(disk.total)
            except OSError:
                disk_used, disk_total = 0, 0

            cache = _directory_size(self.models_root)
            scratch = sum(_directory_size(d) for d in self._scratch_dirs())
            transcripts = _directory_size(self.data_root / "sessions")
            logs = _directory_size(self.data_root / "logs")

        return {
            "memory": (
                {
                    "used": memory["used"],
                    "total": memory["total"],
                    "percent": round((memory["used"] / memory["total"]) * 100, 1)
                    if memory["total"] else 0,
                }
                if memory else None
            ),
            "disk": {
                "used": disk_used,
                "total": disk_total,
                "percent": round((disk_used / disk_total) * 100, 1) if disk_total else 0,
            },
            "cpu": {"percent": cpu},
            "cache": {"bytes": cache},
            "scratch": {"bytes": scratch},
            "transcripts": {"bytes": transcripts},
            "logs": {"bytes": logs},
            "sampledAt": time.time(),
        }

    def _scratch_dirs(self) -> list[Path]:
        """Directories that hold only regenerable scratch data."""
        return [self.data_root / name for name in ("cache", "tmp", ".cache")]

    def clear_cache(self) -> dict:
        """Delete only regenerable scratch data.

        Downloaded models (``*.gguf``), the user's notes/facts and the chat
        transcripts are user data and are deliberately left untouched.
        """
        removed = 0
        cleared: list[str] = []
        for target in self._scratch_dirs():
            if not target.is_dir():
                continue
            for root, _dirs, files in os.walk(target):
                for name in files:
                    path = Path(root) / name
                    try:
                        removed += path.stat().st_size
                        path.unlink()
                    except OSError:
                        continue
            cleared.append(target.name)
        return {"removed_bytes": removed, "cleared": cleared}
