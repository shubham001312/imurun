"""G-2 probe(): read the machine's real capacity, time-boxed, never throw.

Every part is probed independently inside a deadline; a part that fails or
times out is recorded in `errors` and the profile is returned with what is
known (V9: marked, not silently skipped).
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

DEFAULT_DEADLINE = 3.0

_PHYSICAL_CORES: int | None = None


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


def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


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
    total = int(_run(["sysctl", "-n", "hw.memsize"], 2.0).stdout.strip())
    out = _run(["vm_stat"], 2.0).stdout
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


def _physical_cores() -> int:
    global _PHYSICAL_CORES
    if _PHYSICAL_CORES is not None:
        return _PHYSICAL_CORES
    value = 0
    try:
        if sys.platform == "win32":
            out = _run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum",
                ],
                3.0,
            )
            value = int(out.stdout.strip())
        elif sys.platform == "darwin":
            value = int(_run(["sysctl", "-n", "hw.physicalcpu"], 2.0).stdout.strip())
        else:
            pairs: set[tuple[str, str]] = set()
            physical = core = None
            with open("/proc/cpuinfo", "r", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("physical id"):
                        physical = line.split(":")[1].strip()
                    elif line.startswith("core id"):
                        core = line.split(":")[1].strip()
                    elif not line.strip() and physical is not None and core is not None:
                        pairs.add((physical, core))
                        physical = core = None
            value = len(pairs)
    except Exception:  # noqa: BLE001 - fall back to logical cores
        value = 0
    _PHYSICAL_CORES = value if value > 0 else (os.cpu_count() or 1)
    return _PHYSICAL_CORES


def _gpu() -> GpuInfo:
    if sys.platform == "win32":
        out = _run(
            ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
            3.0,
        ).stdout.casefold()
        if not out.strip():
            return GpuInfo(present=False)
        for vendor, needle in (
            ("nvidia", "nvidia"),
            ("amd", "amd"),
            ("amd", "radeon"),
            ("intel", "intel"),
            ("apple", "apple"),
        ):
            if needle in out:
                return GpuInfo(present=True, vendor=vendor)
        return GpuInfo(present=True, vendor="other")

    if sys.platform == "darwin":
        out = _run(["system_profiler", "SPDisplaysDataType"], 3.0).stdout.casefold()
        if "chipset model" not in out:
            return GpuInfo(present=False)
        if "apple" in out:
            return GpuInfo(present=True, vendor="apple")
        if "amd" in out or "radeon" in out:
            return GpuInfo(present=True, vendor="amd")
        if "intel" in out:
            return GpuInfo(present=True, vendor="intel")
        return GpuInfo(present=True, vendor="other")

    vendors = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}
    try:
        for path in glob.glob("/sys/class/drm/card*/device/vendor"):
            with open(path, "r", encoding="ascii") as fh:
                vendor_id = fh.read().strip()
            if vendor_id in vendors:
                return GpuInfo(present=True, vendor=vendors[vendor_id])
    except OSError:
        pass
    try:
        out = _run(["lspci"], 2.0).stdout.casefold()
        if "vga" in out or "3d controller" in out:
            for vendor in ("nvidia", "amd", "intel"):
                if vendor in out:
                    return GpuInfo(present=True, vendor=vendor)
            return GpuInfo(present=True, vendor="other")
    except (OSError, subprocess.SubprocessError):
        pass
    return GpuInfo(present=False)


def _within(seconds: float, part: Callable[[], object]) -> object:
    """Run one probe part with a hard deadline; raise on timeout."""
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = part()
        except Exception as exc:  # noqa: BLE001 - carried to the caller
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(max(0.0, seconds))
    if thread.is_alive():
        raise TimeoutError(f"timed out after {seconds:.1f}s")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]


def _existing(path: str) -> str:
    candidate = os.path.abspath(os.path.expanduser(path))
    while not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return os.path.expanduser("~")
        candidate = parent
    return candidate


def probe(
    *,
    memory: Callable[[], tuple[int, int]] | None = None,
    disk: Callable[[str], object] | None = None,
    gpu: Callable[[], GpuInfo] | None = None,
    path: str | None = None,
    model_dir: str | None = None,
    deadline: float = DEFAULT_DEADLINE,
) -> MachineProfile:
    """Probe the machine. Injectable parts exist so tests can prove the
    failure and timeout paths without breaking the real one."""
    errors: list[str] = []
    stop = time.monotonic() + max(0.0, deadline)

    def remaining() -> float:
        return max(0.0, stop - time.monotonic())

    total = available = 0
    try:
        total, available = _within(remaining(), memory or _memory)  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        errors.append(f"memory: {exc}")

    cores = 1
    try:
        cores = max(1, int(_within(remaining(), _physical_cores)))  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        errors.append(f"cpu: {exc}")

    target = model_dir or path or os.path.expanduser("~/.imurun")
    free = 0
    try:
        usage = _within(remaining(), lambda: (disk or shutil.disk_usage)(_existing(target)))  # type: ignore[misc]
        free = int(usage.free)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        errors.append(f"disk: {exc}")

    try:
        card = _within(remaining(), gpu or _gpu)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"gpu: {exc}")
        card = GpuInfo(present=False)

    return MachineProfile(
        ram_total=total,  # type: ignore[arg-type]
        ram_available=available,  # type: ignore[arg-type]
        cpu_cores=cores,
        disk_free=free,
        gpu=card,  # type: ignore[arg-type]
        errors=tuple(errors),
    )
