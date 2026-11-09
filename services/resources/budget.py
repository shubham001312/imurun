"""ResourceBudget: how much of this machine IMURUN may use.

Everything is derived from the measured machine (``Machine``, built from the
G-2 ``MachineProfile`` plus live GPU memory); nothing is tuned for one laptop.
The owner's rule: always leave 20-30 % headroom for the user's normal use of
CPU, memory, GPU and disk, scaled by how big the machine is.

Formulas (balanced preset; ``performance`` halves every headroom, ``gentle``
(alias ``eco``) doubles it, ``custom`` sets each percentage directly) - see
docs/resources/README.md for the reasoning and measured numbers:

* CPU: physical cores only (SMT siblings add contention, not bandwidth, to a
  memory-bound matmul). headroom by device class (``Machine.device_class``;
  a battery = laptop, unknown is treated as a laptop):

      physical cores   laptop   desktop
      <= 4              30 %     30 %
      5 - 8             30 %     25 %
      9 - 16            25 %     20 %
      > 16              20 %     20 %

  rounded, never under 20 % of the cores (x preset factor), at least one
  core always free (when there are two or more), never all of them. engine threads = physical - headroom (1..physical).
* RAM (weights + KV): total - OS reserve - headroom, headroom 25 % on <=8 GB,
  20 % below 32 GB, 15 % from 32 GB. OS reserve = max(1.5 GB, total / 8),
  capped at 4 GB (the engine planner's own reserve rule). At load time the
  budget is also capped by what is free right now minus the headroom, so a
  user who already filled the machine keeps their headroom.
* VRAM: budget = free VRAM at load - headroom. What other programs hold is
  already excluded by measuring *free* VRAM, so the headroom only has to
  cover their growth and the driver: max(256 MB, 10 % of the card), plus
  min(512 MB, 25 % of what others hold) when they hold more than 5 % of it;
  times the preset factor, floor 256 MB (the engine's measured minimum),
  cap 50 % of the card. A 4 GB card with ~0.3 GB used elsewhere keeps
  ~0.5 GB and gives the engine ~3.2 GB (an 8B needs ~3.1 GB resident); the
  earlier flat 25 % kept 1 GB and left ~2.1 GB.
* Disk: max(10 GB, 10 % of the drive) must stay free after a download,
  export or tuning run.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

GB = 1024**3
MB = 1024**2

PRESETS: dict[str, float] = {"gentle": 2.0, "balanced": 1.0, "performance": 0.5}
PRESET_NAMES = tuple(PRESETS) + ("custom",)
# Accepted on input, stored and reported under the canonical name.
PRESET_ALIASES = {"eco": "gentle"}
VRAM_FLOOR = 256 * 1024**2
DEVICE_CLASSES = ("laptop", "desktop", "unknown")


def normalize_preset(name) -> str | None:
    """Canonical preset name, or None when ``name`` is not one."""
    if not isinstance(name, str):
        return None
    name = PRESET_ALIASES.get(name.strip().lower(), name.strip().lower())
    return name if name in PRESET_NAMES else None
RESOURCES = ("cpu", "ram", "vram", "disk")
# Headroom percentages above this would leave IMURUN nothing to run with.
MAX_PERCENT = 90.0
DISK_FLOOR = 10 * GB
DISK_SHARE = 0.10
# Absolute floor for any preset: the G4 rule of services/safety/limits.py.
DISK_HARD_FLOOR = 2 * GB


@dataclass(frozen=True)
class Machine:
    """What was measured. Sizes in bytes; 0 means unknown / absent."""

    physical_cores: int
    logical_cores: int
    ram_total: int
    ram_available: int
    vram_total: int = 0
    vram_free: int = 0
    gpu_name: str = ""
    disk_total: int = 0
    disk_free: int = 0
    disk_path: str = ""
    errors: tuple[str, ...] = ()
    device_class: str = ""       # laptop | desktop | unknown ("" = unknown)
    gpu_vendor: str = ""
    gpu_source: str = ""         # nvidia-smi | rocm-smi | sysfs | registry | cim | system_profiler | engine
    vram_used: int = 0           # held by OTHER programs (IMURUN's own model excluded); 0 = total - free
    vram_live: bool = True       # vram_free / vram_used were measured (not identity-only)

    @property
    def has_gpu(self) -> bool:
        return self.vram_total > 0 or bool(self.gpu_name)

    @property
    def vram_others(self) -> int:
        """VRAM held by other programs (bytes; 0 when not measured)."""
        if not self.vram_total or not self.vram_live:
            return 0
        if self.vram_used:
            return max(0, int(self.vram_used))
        return max(0, int(self.vram_total) - int(self.vram_free))


def cpu_share(physical: int, device_class: str = "") -> float:
    laptop = device_class != "desktop"     # unknown counts as a laptop: the safer side
    if physical <= 4:
        return 0.30
    if physical <= 8:
        return 0.30 if laptop else 0.25
    if physical <= 16:
        return 0.25 if laptop else 0.20
    return 0.20


def ram_share(total: int) -> float:
    # Thresholds sit a little above the marketing sizes: a "16 GB" laptop
    # reports 15.8 GiB, an "8 GB" one 7.8 GiB.
    if total <= 8.5 * GB:
        return 0.25
    if total < 31 * GB:
        return 0.20
    return 0.15


def vram_headroom_bytes(total: int, others: int = 0, factor: float = 1.0) -> int:
    """Bytes of VRAM kept free at a load (see the module docstring)."""
    if total <= 0:
        return 0
    base = max(VRAM_FLOOR, 0.10 * total)
    growth = min(512 * MB, 0.25 * others) if others > 0.05 * total else 0.0
    return int(min(0.5 * total, max(VRAM_FLOOR, (base + growth) * factor)))


def vram_share(total: int, others: int = 0) -> float:
    """The balanced VRAM headroom as a fraction of the card."""
    return vram_headroom_bytes(total, others) / total if total > 0 else 0.10


def os_reserve(total: int) -> int:
    return int(min(4 * GB, max(1.5 * GB, total / 8)))


@dataclass(frozen=True)
class Headroom:
    """Headroom fractions (0..0.9) per resource, and where they came from."""

    preset: str
    cpu: float
    ram: float
    vram: float
    disk: float          # fraction of the drive (balanced: 0.10)
    disk_floor: int      # absolute bytes that stay free at least
    vram_bytes: int = 0  # VRAM headroom in bytes (the fraction above, resolved)

    def view(self) -> dict:
        return {"preset": self.preset, "cpu": round(self.cpu * 100, 1), "ram": round(self.ram * 100, 1),
                "vram": round(self.vram * 100, 1), "disk": round(self.disk * 100, 1),
                "diskFloorGb": round(self.disk_floor / GB, 1)}


def _clamp_share(value: float) -> float:
    return max(0.0, min(MAX_PERCENT / 100.0, float(value)))


def headroom_for(machine: Machine, preset: str = "balanced", custom: dict | None = None) -> Headroom:
    preset = normalize_preset(preset) or "balanced"
    if preset == "custom":
        values = normalize_custom(custom or {}, machine)
        total = int(machine.vram_total)
        return Headroom(
            preset="custom",
            cpu=values["cpu"] / 100.0,
            ram=values["ram"] / 100.0,
            vram=values["vram"] / 100.0,
            disk=values["disk"] / 100.0,
            disk_floor=DISK_HARD_FLOOR,
            vram_bytes=int(max(VRAM_FLOOR, total * values["vram"] / 100.0)) if total else 0,
        )
    m = PRESETS[preset]
    total = int(machine.vram_total)
    vram_bytes = vram_headroom_bytes(total, machine.vram_others, m)
    return Headroom(
        preset=preset,
        cpu=_clamp_share(cpu_share(machine.physical_cores, machine.device_class) * m),
        ram=_clamp_share(ram_share(machine.ram_total) * m),
        vram=_clamp_share(vram_bytes / total if total else 0.10 * m),
        disk=_clamp_share(DISK_SHARE * m),
        disk_floor=max(DISK_HARD_FLOOR, int(DISK_FLOOR * m)),
        vram_bytes=vram_bytes,
    )


def balanced_percentages(machine: Machine) -> dict:
    """The balanced numbers, used as the starting point of ``custom``."""
    return {"cpu": round(cpu_share(machine.physical_cores, machine.device_class) * 100, 1),
            "ram": round(ram_share(machine.ram_total) * 100, 1),
            "vram": round(vram_share(machine.vram_total, machine.vram_others) * 100, 1),
            "disk": round(DISK_SHARE * 100, 1)}


def normalize_custom(custom: dict, machine: Machine | None = None) -> dict:
    """Custom percentages with missing keys filled from balanced. Raises
    ``ValueError`` for a non-number or a value outside 0..90."""
    base = balanced_percentages(machine) if machine is not None else {"cpu": 25.0, "ram": 20.0,
                                                                        "vram": 10.0, "disk": 10.0}
    out = dict(base)
    if not isinstance(custom, dict):
        raise ValueError("custom must be an object {cpu, ram, vram, disk} of percentages")
    for key, value in custom.items():
        if key not in RESOURCES:
            raise ValueError(f"custom: unknown resource {key!r} (use {', '.join(RESOURCES)})")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(float(value)):
            raise ValueError(f"custom.{key} must be a number (percent)")
        if not 0 <= float(value) <= MAX_PERCENT:
            raise ValueError(f"custom.{key} must be between 0 and {MAX_PERCENT:.0f} percent")
        out[key] = float(value)
    return out


@dataclass(frozen=True)
class ResourceBudget:
    headroom: Headroom
    physical_cores: int
    logical_cores: int
    threads: int                 # engine compute threads (physical cores)
    headroom_cores: int
    ram_total: int
    ram_os_reserve: int
    ram_headroom: int
    ram_budget: int              # static: total - reserve - headroom
    vram_total: int
    vram_headroom: int
    vram_budget: int             # free-at-measurement - headroom
    disk_total: int
    disk_min_free: int
    notes: tuple[str, ...] = field(default_factory=tuple)
    vram_used_by_others: int = 0
    device_class: str = ""

    def ram_budget_now(self, available: int) -> int:
        """The RAM budget for a load happening now: never more than what is
        free minus the user's headroom."""
        if available <= 0:
            return self.ram_budget
        return max(0, min(self.ram_budget, available - self.ram_headroom))

    def vram_budget_now(self, free: int) -> int:
        return max(0, free - self.vram_headroom) if self.vram_total else 0

    def disk_allows(self, free: int, need: int) -> bool:
        return free - max(0, need) >= self.disk_min_free

    def view(self) -> dict:
        data = asdict(self)
        data["headroom"] = self.headroom.view()
        data["notes"] = list(self.notes)
        for key in ("ram_total", "ram_os_reserve", "ram_headroom", "ram_budget", "vram_total",
                    "vram_headroom", "vram_budget", "vram_used_by_others", "disk_total", "disk_min_free"):
            data[key + "_gb"] = round(data[key] / GB, 2)
        return data


def compute_budget(machine: Machine, preset: str = "balanced", custom: dict | None = None) -> ResourceBudget:
    room = headroom_for(machine, preset, custom)
    notes: list[str] = []
    physical = max(1, int(machine.physical_cores or 1))
    if physical >= 2:
        # At least one physical core always stays free for the user.
        # Rounded share, but never under 20 % of the cores (times the preset
        # factor): round(12 * 0.2) = 2 would keep only 17 % free.
        factor = PRESETS.get(room.preset)
        floor_share = 0.20 * factor if factor else 0.0
        spare = max(math.floor(physical * room.cpu + 0.5), math.ceil(physical * floor_share - 1e-9))
        spare = min(physical - 1, max(1, spare))
    else:
        spare = 0
        notes.append("single-core machine: no core can be kept free; the engine runs below normal priority")
    threads = max(1, physical - spare)

    total = max(0, int(machine.ram_total))
    reserve = os_reserve(total) if total else 0
    ram_room = int(total * room.ram)
    ram_budget = max(0, total - reserve - ram_room)

    vram_room = room.vram_bytes if machine.vram_total else 0
    vram_budget = max(0, machine.vram_free - vram_room) if machine.vram_total else 0
    others = machine.vram_others
    if machine.vram_total and others > machine.vram_total * 0.4:
        notes.append(f"other programs hold {others / GB:.1f} GB of the {machine.vram_total / GB:.1f} GB GPU; "
                     "fewer layers fit on it")
    if machine.vram_total and not machine.vram_live:
        notes.append(f"GPU found through {machine.gpu_source or 'the OS'} without a live memory counter: "
                     "the engine measures free VRAM itself at load")
    if not machine.vram_total:
        notes.append("no GPU memory measured: the engine runs on the CPU (or reports its own GPU)"
                     if not machine.gpu_name else
                     f"{machine.gpu_name}: VRAM size unknown; the engine measures it itself at load")

    disk_min = max(room.disk_floor, int(machine.disk_total * room.disk))
    if machine.ram_available and machine.ram_available < ram_room:
        notes.append(f"only {machine.ram_available / GB:.1f} GB RAM is free now - less than the "
                     f"{ram_room / GB:.1f} GB headroom; a model loaded now streams from disk")
    return ResourceBudget(
        headroom=room,
        physical_cores=physical,
        logical_cores=max(physical, int(machine.logical_cores or physical)),
        threads=threads,
        headroom_cores=spare,
        ram_total=total,
        ram_os_reserve=reserve,
        ram_headroom=ram_room,
        ram_budget=ram_budget,
        vram_total=int(machine.vram_total),
        vram_headroom=vram_room,
        vram_budget=vram_budget,
        disk_total=int(machine.disk_total),
        disk_min_free=disk_min,
        notes=tuple(notes),
        vram_used_by_others=others,
        device_class=machine.device_class or "unknown",
    )


def validate_settings(values: dict) -> dict:
    """Validate the ``resources`` settings keys this package owns. Returns
    the cleaned subset; raises ``ValueError`` with a user-facing message."""
    out: dict = {}
    for key in ("preset", "headroom"):
        if key in values:
            preset = normalize_preset(values[key])
            if preset is None:
                raise ValueError(f"{key} must be one of {', '.join(PRESET_NAMES)} (eco = gentle)")
            if "headroom" in out and out["headroom"] != preset:
                raise ValueError("preset and headroom disagree; send one of them")
            out["headroom"] = preset
    if "custom" in values:
        custom = values["custom"]
        if custom in (None, {}):
            out["custom"] = {}
        else:
            normalize_custom(custom)  # raises
            out["custom"] = {k: float(v) for k, v in custom.items()}
    for key in ("dynamic", "lowerPriority"):
        if key in values:
            if not isinstance(values[key], bool):
                raise ValueError(f"{key} must be true or false")
            out[key] = values[key]
    return out
