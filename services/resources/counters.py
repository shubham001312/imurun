"""Low-level counters: CPU time (system and per process), cores, GPU memory.

No new dependency: psutil is used when it is installed (it is an optional
extra), otherwise the platform's own counters are read directly - Windows
through kernel32 (GetSystemTimes / GetProcessTimes / processor topology),
Linux through /proc; GPUs through nvidia-smi, then rocm-smi / sysfs (Linux),
then the OS identity (Windows registry / CIM, macOS system_profiler). Every function
returns ``None`` (or an empty result) instead of raising: a missing counter
makes the governor less precise, never broken.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

_CORES: tuple[int, int] | None = None


# ------------------------------------------------------------------ cores

def _windows_physical_cores() -> int:
    """Count RelationProcessorCore entries (one per physical core; SMT
    siblings share an entry) via GetLogicalProcessorInformation. This is a
    sub-millisecond call; the PowerShell/CIM query it replaces took longer
    than the whole 3 s probe deadline on this laptop."""
    import ctypes

    class _Info(ctypes.Structure):
        _fields_ = [("mask", ctypes.c_size_t), ("relationship", ctypes.c_int),
                    ("union", ctypes.c_ulonglong * 2)]

    kernel32 = ctypes.windll.kernel32
    length = ctypes.c_ulong(0)
    kernel32.GetLogicalProcessorInformation(None, ctypes.byref(length))
    if length.value == 0:
        return 0
    count = length.value // ctypes.sizeof(_Info)
    buffer = (_Info * count)()
    if not kernel32.GetLogicalProcessorInformation(buffer, ctypes.byref(length)):
        return 0
    return sum(1 for item in buffer if item.relationship == 0)


def _linux_physical_cores() -> int:
    pairs: set[tuple[str, str]] = set()
    physical = core = None
    with open("/proc/cpuinfo", "r", encoding="ascii", errors="replace") as fh:
        for line in fh:
            if line.startswith("physical id"):
                physical = line.split(":")[1].strip()
            elif line.startswith("core id"):
                core = line.split(":")[1].strip()
            elif not line.strip():
                if physical is not None and core is not None:
                    pairs.add((physical, core))
                physical = core = None
    if physical is not None and core is not None:
        pairs.add((physical, core))
    return len(pairs)


def cores() -> tuple[int, int]:
    """(physical, logical) cores of this machine, measured once."""
    global _CORES
    if _CORES is not None:
        return _CORES
    logical = os.cpu_count() or 1
    physical = 0
    try:
        import psutil  # type: ignore

        physical = int(psutil.cpu_count(logical=False) or 0)
    except Exception:  # noqa: BLE001 - optional dependency
        physical = 0
    if physical <= 0:
        try:
            if sys.platform == "win32":
                physical = _windows_physical_cores()
            elif sys.platform == "darwin":
                out = subprocess.run(["sysctl", "-n", "hw.physicalcpu"], capture_output=True,
                                     text=True, timeout=2.0).stdout
                physical = int(out.strip() or 0)
            else:
                physical = _linux_physical_cores()
        except Exception:  # noqa: BLE001 - fall back below
            physical = 0
    if physical <= 0:
        # Unknown topology: assume 2-way SMT when there is more than one CPU.
        physical = max(1, logical // 2) if logical > 1 else 1
    _CORES = (min(physical, logical), logical)
    return _CORES


# ------------------------------------------------------------------ CPU time

def _filetime(ft) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def system_times() -> tuple[float, float] | None:
    """(busy_seconds, total_seconds) summed over every logical CPU since boot."""
    try:
        import psutil  # type: ignore

        t = psutil.cpu_times()
        total = float(sum(t))
        idle = float(getattr(t, "idle", 0.0)) + float(getattr(t, "iowait", 0.0))
        return total - idle, total
    except Exception:  # noqa: BLE001
        pass
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                         ctypes.byref(user)):
                return None
            # Kernel time includes idle time.
            i, k, u = _filetime(idle), _filetime(kernel), _filetime(user)
            return (k + u - i) / 1e7, (k + u) / 1e7
        if os.path.exists("/proc/stat"):
            with open("/proc/stat", "r", encoding="ascii") as fh:
                parts = fh.readline().split()[1:]
            values = [float(v) for v in parts]
            tick = float(os.sysconf("SC_CLK_TCK"))
            idle = values[3] + (values[4] if len(values) > 4 else 0.0)
            total = sum(values[:8])
            return (total - idle) / tick, total / tick
    except Exception:  # noqa: BLE001
        return None
    return None


def process_seconds(pid: int | None = None) -> float | None:
    """CPU seconds (user + kernel) used by one process; ``None`` = this one."""
    if pid is None or pid == os.getpid():
        t = os.times()
        return float(t.user + t.system)
    try:
        import psutil  # type: ignore

        t = psutil.Process(int(pid)).cpu_times()
        return float(t.user + t.system)
    except Exception:  # noqa: BLE001
        pass
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                if not kernel32.GetProcessTimes(wintypes.HANDLE(handle), ctypes.byref(c), ctypes.byref(e),
                                                ctypes.byref(k), ctypes.byref(u)):
                    return None
                return (_filetime(k) + _filetime(u)) / 1e7
            finally:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
        path = f"/proc/{int(pid)}/stat"
        if os.path.exists(path):
            with open(path, "r", encoding="ascii", errors="replace") as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
            tick = float(os.sysconf("SC_CLK_TCK"))
            return (float(fields[11]) + float(fields[12])) / tick
    except Exception:  # noqa: BLE001
        return None
    return None


class CpuMeter:
    """Turns cumulative CPU time into shares between two calls.

    ``sample(pids)`` returns the machine's busy share and IMURUN's own share
    (this process plus the listed child processes: voice worker, trainer),
    both as fractions of ALL logical CPUs - the scale Task Manager uses.
    The first call only primes the counters (values are ``None``).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_system: tuple[float, float] | None = None
        self._last_proc: dict[int, float] = {}
        self._last_wall = 0.0

    def sample(self, pids=()) -> dict:
        with self._lock:
            now = time.monotonic()
            system = system_times()
            own_pids = [os.getpid()] + [int(p) for p in pids if p and int(p) != os.getpid()]
            proc_now: dict[int, float] = {}
            for pid in own_pids:
                value = process_seconds(pid)
                if value is not None:
                    proc_now[pid] = value
            _physical, logical = cores()
            out: dict = {"system": None, "own": None, "other": None}
            if system is not None and self._last_system is not None:
                busy = system[0] - self._last_system[0]
                total = system[1] - self._last_system[1]
                if total > 0:
                    out["system"] = max(0.0, min(1.0, busy / total))
            wall = now - self._last_wall if self._last_wall else 0.0
            if wall > 0 and self._last_proc:
                used = sum(max(0.0, proc_now[p] - self._last_proc[p]) for p in proc_now if p in self._last_proc)
                out["own"] = max(0.0, min(1.0, used / (wall * logical)))
            if out["system"] is not None and out["own"] is not None:
                out["other"] = max(0.0, out["system"] - out["own"])
            self._last_system, self._last_proc, self._last_wall = system, proc_now, now
            out["interval"] = round(wall, 3)
            return out


# ------------------------------------------------------------------ device class

def _windows_has_battery() -> bool | None:
    import ctypes

    class _Power(ctypes.Structure):
        _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("percent", ctypes.c_ubyte),
                    ("saver", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]

    status = _Power()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    if status.flag == 255:               # "unknown status"
        return None
    return not bool(status.flag & 128)   # 128 = no system battery


def _linux_has_battery(root: str = "/sys/class/power_supply") -> bool | None:
    if not os.path.isdir(root):
        return None
    for name in os.listdir(root):
        try:
            with open(os.path.join(root, name, "type"), "r", encoding="ascii", errors="replace") as fh:
                if fh.read().strip().lower() == "battery":
                    return True
        except OSError:
            continue
    return False


_DEVICE_CLASS: str | None = None


def device_class() -> str:
    """``laptop`` (a system battery exists), ``desktop`` or ``unknown``.
    A laptop shares one cooler between CPU and GPU and throttles sooner, so
    it keeps more CPU headroom (budget.cpu_share)."""
    global _DEVICE_CLASS
    if _DEVICE_CLASS is not None:
        return _DEVICE_CLASS
    battery: bool | None = None
    try:
        if sys.platform == "win32":
            battery = _windows_has_battery()
        elif sys.platform == "darwin":
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=2.0).stdout
            battery = "InternalBattery" in (out or "")
        else:
            battery = _linux_has_battery()
    except Exception:  # noqa: BLE001
        battery = None
    _DEVICE_CLASS = "unknown" if battery is None else ("laptop" if battery else "desktop")
    return _DEVICE_CLASS


# ------------------------------------------------------------------ GPU

MIB = 1024 * 1024
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


@dataclass(frozen=True)
class GpuMemory:
    """One GPU. ``used`` / ``free`` are measured only when ``live`` (vendor
    tool or sysfs); identity-only sources (registry, CIM, system_profiler)
    report ``used=0, free=total, live=False``."""

    name: str
    total: int
    used: int
    free: int
    utilization: float | None  # 0..1, None when unknown
    source: str = "nvidia-smi"
    vendor: str = ""
    live: bool = True

    def view(self) -> dict:
        return {"name": self.name, "vendor": self.vendor or vendor_of(self.name), "total": self.total,
                "used": self.used if self.live else None, "free": self.free if self.live else None,
                "utilization": self.utilization, "source": self.source, "live": self.live}


_PCI_VENDORS = {"10de": "nvidia", "1002": "amd", "1022": "amd", "8086": "intel", "106b": "apple"}
# Adapters nobody computes on (remote desktops, the fallback driver, virtual displays).
_SKIP_ADAPTERS = ("microsoft basic", "remote display", "virtual", "parsec", "displaylink",
                  "citrix", "vmware svga", "hyper-v")


def vendor_of(name: str, hint: str = "") -> str:
    text = f"{hint} {name}".lower()
    for key, vendor in (("nvidia", "nvidia"), ("geforce", "nvidia"), ("quadro", "nvidia"), ("rtx", "nvidia"),
                        ("gtx", "nvidia"), ("advanced micro", "amd"), ("amd", "amd"), ("radeon", "amd"),
                        ("intel", "intel"), ("apple", "apple")):
        if key in text:
            return vendor
    return "other" if text.strip() else ""


def _rank(card: "GpuMemory") -> tuple:
    # Discrete before integrated (Intel iGPUs report a token 128 MB), then size.
    discrete = card.vendor in ("nvidia", "amd") or (card.vendor == "intel" and "arc" in card.name.lower())
    return (discrete, card.total)


def _best(cards) -> "GpuMemory | None":
    cards = [c for c in cards if c is not None and c.name
             and not any(skip in c.name.lower() for skip in _SKIP_ADAPTERS)]
    return max(cards, key=_rank) if cards else None


def _run(cmd: list[str], timeout: float) -> str | None:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _NO_WINDOW
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if sys.platform == "win32":
        candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def parse_nvidia_smi(out: str) -> "GpuMemory | None":
    """``--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu
    --format=csv,noheader,nounits`` (MiB). The largest card wins."""
    cards = []
    for line in (out or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            total, used, free = (int(float(v)) * MIB for v in parts[1:4])
        except ValueError:
            continue
        try:
            util: float | None = float(parts[4]) / 100.0 if len(parts) > 4 else None
        except ValueError:
            util = None
        cards.append(GpuMemory(name=parts[0], total=total, used=used, free=free, utilization=util,
                               source="nvidia-smi", vendor="nvidia"))
    return _best(cards)


def parse_rocm_smi(out: str) -> "GpuMemory | None":
    """``rocm-smi --showproductname --showmeminfo vram --json``: one object
    per card; key names vary by ROCm version, matched case-insensitively."""
    try:
        data = json.loads(out or "{}")
    except ValueError:
        return None
    cards = []
    for key, info in (data.items() if isinstance(data, dict) else ()):
        if not isinstance(info, dict) or not str(key).lower().startswith("card"):
            continue
        lower = {str(k).lower(): v for k, v in info.items()}

        def num(name: str) -> int:
            try:
                return int(float(str(lower.get(name, "0")).strip()))
            except ValueError:
                return 0

        total = num("vram total memory (b)")
        used = num("vram total used memory (b)")
        name = next((str(lower[n]).strip() for n in ("card series", "card model", "card sku", "device name")
                     if str(lower.get(n, "")).strip()), "AMD GPU")
        if total:
            cards.append(GpuMemory(name=name, total=total, used=used, free=max(0, total - used),
                                   utilization=None, source="rocm-smi", vendor="amd"))
    return _best(cards)


def parse_drm(root: str = "/sys/class/drm") -> "GpuMemory | None":
    """Linux sysfs ``card*/device/{vendor,device,mem_info_vram_total,
    mem_info_vram_used}`` - amdgpu exposes VRAM (live); other drivers give
    identity only."""
    if not os.path.isdir(root):
        return None
    cards = []
    for name in sorted(os.listdir(root)):
        if not name.startswith("card") or "-" in name:
            continue
        dev = os.path.join(root, name, "device")

        def read(fname: str) -> str:
            try:
                with open(os.path.join(dev, fname), "r", encoding="ascii", errors="replace") as fh:
                    return fh.read().strip()
            except OSError:
                return ""

        vendor_id = read("vendor").lower().replace("0x", "")
        if not vendor_id:
            continue
        vendor = _PCI_VENDORS.get(vendor_id, "other")
        label = read("product_name") or f"{vendor.upper() if vendor != 'other' else 'GPU'} {read('device')}".strip()
        try:
            total = int(read("mem_info_vram_total") or 0)
            used = int(read("mem_info_vram_used") or 0)
        except ValueError:
            total = used = 0
        cards.append(GpuMemory(name=label, total=total, used=used if total else 0,
                               free=max(0, total - used) if total else 0, utilization=None,
                               source="sysfs", vendor=vendor, live=bool(total)))
    return _best(cards)


def parse_cim_json(out: str) -> "GpuMemory | None":
    """``Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,
    AdapterCompatibility | ConvertTo-Json``. AdapterRAM is a uint32, so a
    card of 4 GB or more reads as ~4 GB: the name is right, the size a floor."""
    try:
        data = json.loads(out or "null")
    except ValueError:
        return None
    items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    cards = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        try:
            total = max(0, int(item.get("AdapterRAM") or 0))
        except (TypeError, ValueError):
            total = 0
        cards.append(GpuMemory(name=name, total=total, used=0, free=total, utilization=None, source="cim",
                               vendor=vendor_of(name, str(item.get("AdapterCompatibility") or "")), live=False))
    return _best(cards)


_DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def registry_adapters() -> list[dict]:
    """Windows display adapters from the registry: ``DriverDesc``,
    ``ProviderName`` and ``HardwareInformation.qwMemorySize`` (64-bit,
    unlike CIM's AdapterRAM). Sub-millisecond, no subprocess."""
    import winreg  # type: ignore

    out: list[dict] = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _DISPLAY_CLASS) as root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not sub.isdigit():
                continue
            entry: dict = {}
            try:
                with winreg.OpenKey(root, sub) as key:
                    for value in ("DriverDesc", "ProviderName", "HardwareInformation.qwMemorySize",
                                  "HardwareInformation.MemorySize"):
                        try:
                            entry[value] = winreg.QueryValueEx(key, value)[0]
                        except OSError:
                            continue
            except OSError:
                continue
            if entry.get("DriverDesc"):
                out.append(entry)
    return out


def parse_registry(entries) -> "GpuMemory | None":
    cards = []
    for entry in entries or ():
        name = str(entry.get("DriverDesc") or "").strip()
        size = entry.get("HardwareInformation.qwMemorySize") or entry.get("HardwareInformation.MemorySize") or 0
        if isinstance(size, (bytes, bytearray)):
            size = int.from_bytes(bytes(size[:8]), "little")
        try:
            total = max(0, int(size))
        except (TypeError, ValueError):
            total = 0
        cards.append(GpuMemory(name=name, total=total, used=0, free=total, utilization=None, source="registry",
                               vendor=vendor_of(name, str(entry.get("ProviderName") or "")), live=False))
    return _best(cards)


def parse_system_profiler(out: str) -> "GpuMemory | None":
    """macOS ``system_profiler SPDisplaysDataType -json``. Apple silicon has
    unified memory (no VRAM figure): total stays 0 and the engine's own
    report is the authority."""
    try:
        items = (json.loads(out or "{}") or {}).get("SPDisplaysDataType") or []
    except (ValueError, AttributeError):
        return None
    cards = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("sppci_model") or item.get("_name") or "").strip()
        vram = str(item.get("spdisplays_vram") or item.get("spdisplays_vram_shared") or "")
        match = re.match(r"\s*([\d.]+)\s*(GB|MB)", vram, re.I)
        total = int(float(match.group(1)) * (1024**3 if match.group(2).upper() == "GB" else MIB)) if match else 0
        cards.append(GpuMemory(name=name, total=total, used=0, free=total, utilization=None,
                               source="system_profiler",
                               vendor=vendor_of(name, str(item.get("spdisplays_vendor") or "")), live=False))
    return _best(cards)


def gpu_memory(timeout: float = 3.0) -> "GpuMemory | None":
    """Live memory of the main GPU: nvidia-smi, else (Linux) amdgpu sysfs or
    rocm-smi. None when no live counter exists - ``detect_gpu`` adds the
    identity-only fallbacks."""
    exe = _nvidia_smi()
    if exe:
        card = parse_nvidia_smi(_run([exe, "--query-gpu=name,memory.total,memory.used,memory.free,"
                                      "utilization.gpu", "--format=csv,noheader,nounits"], timeout) or "")
        if card is not None:
            return card
    if sys.platform.startswith("linux"):
        card = parse_drm()
        if card is not None and card.live:
            return card
        exe = shutil.which("rocm-smi")
        if exe:
            card = parse_rocm_smi(_run([exe, "--showproductname", "--showmeminfo", "vram", "--json"],
                                       timeout) or "")
            if card is not None:
                return card
    return None


_IDENTITY: "GpuMemory | None" = None
_IDENTITY_DONE = False
_IDENTITY_LOCK = threading.Lock()


def gpu_identity(timeout: float = 8.0) -> "GpuMemory | None":
    """The OS's own view of the display adapters (cached; it does not change
    while running): Windows registry, then ``Get-CimInstance
    Win32_VideoController``; macOS system_profiler; Linux sysfs."""
    global _IDENTITY, _IDENTITY_DONE
    with _IDENTITY_LOCK:
        if _IDENTITY_DONE:
            return _IDENTITY
        card: GpuMemory | None = None
        try:
            if sys.platform == "win32":
                try:
                    card = parse_registry(registry_adapters())
                except Exception:  # noqa: BLE001 - fall through to CIM
                    card = None
                if card is None:
                    ps = shutil.which("powershell") or shutil.which("pwsh")
                    if ps:
                        card = parse_cim_json(_run(
                            [ps, "-NoProfile", "-NonInteractive", "-Command",
                             "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM,"
                             "AdapterCompatibility | ConvertTo-Json -Compress"], timeout) or "")
            elif sys.platform == "darwin":
                card = parse_system_profiler(_run(["system_profiler", "SPDisplaysDataType", "-json"],
                                                  timeout) or "")
            else:
                card = parse_drm()
        except Exception:  # noqa: BLE001
            card = None
        _IDENTITY, _IDENTITY_DONE = card, True
        return card


def detect_gpu(timeout: float = 3.0) -> "GpuMemory | None":
    """The machine's main GPU from the best source available (live vendor
    counter first, then OS identity). Never raises; None only when no source
    sees a GPU at all."""
    try:
        live = gpu_memory(timeout)
    except Exception:  # noqa: BLE001
        live = None
    return live if live is not None else gpu_identity()


def reset_cache() -> None:
    """Forget the cached GPU identity and device class (tests)."""
    global _IDENTITY, _IDENTITY_DONE, _DEVICE_CLASS
    with _IDENTITY_LOCK:
        _IDENTITY, _IDENTITY_DONE = None, False
    _DEVICE_CLASS = None
