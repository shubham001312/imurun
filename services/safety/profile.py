"""G-1 probe(): read the machine's real capacity, time-boxed, never throw.

Every part is probed independently; a part that fails is recorded in
`errors` and the profile is returned with what is known (V9: marked, not
silently skipped).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class GpuInfo:
    present: bool
    vendor: str | None = None


@dataclass(frozen=True)
class MachineProfile:
    ram_total: int
    ram_available: int
    cpu_cores: int
    disk_free: int
    gpu: GpuInfo
    errors: tuple[str, ...] = ()


def _windows_memory() -> tuple[int, int]:
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

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        raise OSError("GlobalMemoryStatusEx failed")
    return int(stat.ullTotalPhys), int(stat.ullAvailPhys)


def _linux_memory() -> tuple[int, int]:
    values: dict[str, int] = {}
    with open("/proc/meminfo", "r", encoding="ascii") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            if rest:
                values[key.strip()] = int(rest.strip().split()[0]) * 1024
    return values["MemTotal"], values["MemAvailable"]


def _macos_memory() -> tuple[int, int]:
    total = int(
        subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout.strip()
    )
    # vm_stat reports pages; free + inactive is the closest estimate of what
    # a new process can claim without swapping.
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2, check=True).stdout
    page = 4096
    free = inactive = 0
    for line in out.splitlines():
        if "page size of" in line:
            digits = "".join(ch for ch in line if ch.isdigit())
            if digits:
                page = int(digits)
        elif line.startswith("Pages free:"):
            free = int(line.split(":")[1].strip().rstrip("."))
        elif line.startswith("Pages inactive:"):
            inactive = int(line.split(":")[1].strip().rstrip("."))
    return total, (free + inactive) * page


def _memory() -> tuple[int, int]:
    if sys.platform == "win32":
        return _windows_memory()
    if sys.platform == "darwin":
        return _macos_memory()
    return _linux_memory()


def _gpu() -> GpuInfo:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0 and out.stdout.strip():
            return GpuInfo(present=True, vendor="nvidia")
    except (OSError, subprocess.SubprocessError):
        pass
    return GpuInfo(present=False)


def probe(
    *,
    memory: Callable[[], tuple[int, int]] | None = None,
    disk: Callable[[str], object] | None = None,
    gpu: Callable[[], GpuInfo] | None = None,
    path: str | None = None,
) -> MachineProfile:
    """Probe the machine. Injectable parts exist so tests can prove the
    failure path without breaking the real one."""
    errors: list[str] = []
    total = available = 0
    try:
        total, available = (memory or _memory)()
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        errors.append(f"memory: {exc}")

    cores = 1
    try:
        cores = max(1, os.cpu_count() or 1)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"cpu: {exc}")

    free = 0
    try:
        usage = (disk or shutil.disk_usage)(path or os.path.expanduser("~"))
        free = int(usage.free)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"disk: {exc}")

    try:
        card = (gpu or _gpu)()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"gpu: {exc}")
        card = GpuInfo(present=False)

    return MachineProfile(
        ram_total=total,
        ram_available=available,
        cpu_cores=cores,
        disk_free=free,
        gpu=card,
        errors=tuple(errors),
    )
