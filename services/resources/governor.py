"""ResourceGovernor: the one place that turns the budget into decisions.

* engine: thread count, RAM budget and VRAM headroom for every model load
  (``load_env``) and the thread target for every generation
  (``before_generate`` / ``after_generate``);
* priority: the server process runs below normal priority while a reply, a
  voice request or a tuning step runs (``resources.lowerPriority``,
  ``priority.PriorityGuard``) and is restored afterwards;
* voice / tuning: their thread grants come out of the same ledger;
* research: fetch concurrency;
* disk: downloads, exports and tuning runs are refused when they would eat
  into the disk headroom (``check_disk``);
* GPU: detected from nvidia-smi, rocm-smi / sysfs, or the OS (registry /
  CIM / system_profiler); after a load the engine's own placement report
  (gpu name, layers on the GPU, bytes, mix) is merged into the view;
* dynamic: a sampler thread watches the host; when the user's *other* load
  grows past the CPU headroom for 2 samples, the thread target for the next
  generation is lowered, and raised back after 3 calm samples. Every change
  is published as the ``resources`` event at once; a heartbeat follows every
  10 s only while an ``/api/events`` client listens.
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import counters
from .budget import (GB, MB, PRESET_NAMES, Machine, ResourceBudget, balanced_percentages, compute_budget,
                     normalize_custom, normalize_preset)
from .priority import PriorityGuard
from .share import CHAT, TUNING, VOICE, ThreadLedger

LOG = logging.getLogger(__name__)

SECTION = "resources"
ENGINE_KNOBS = ("FISH_THREADS", "FISH_RAM_BUDGET_MB", "FISH_VRAM_HEADROOM_MB")
# The engine's placement treats 0 as "unset" (env_mb), so the smallest RAM
# budget we hand over is this; the rest of the model streams from disk.
MIN_RAM_BUDGET_MB = 256
LOWER_AFTER = 2       # consecutive samples over the headroom before lowering
RAISE_AFTER = 3       # consecutive calm samples before raising back
GPU_EVERY = 3         # GPU counters on every Nth sample, and only while someone listens
DEFAULTS = {"headroom": "balanced", "custom": {}, "dynamic": True, "lowerPriority": True}
_VENDOR_PREFIXES = ("NVIDIA GeForce ", "NVIDIA ", "AMD Radeon ", "AMD ", "Intel(R) ", "Intel ", "Apple ")


def short_gpu_name(name: str) -> str:
    out = (name or "").strip()
    for prefix in _VENDOR_PREFIXES:
        if out.startswith(prefix):
            return out[len(prefix):].strip() or out
    return out


@dataclass(frozen=True)
class DiskCheck:
    ok: bool
    message: str
    path: str
    free: int
    total: int
    need: int
    min_free: int

    def view(self) -> dict:
        return {"ok": self.ok, "message": self.message, "path": self.path, "free": self.free,
                "total": self.total, "need": self.need, "minFree": self.min_free,
                "freeGb": round(self.free / GB, 2), "minFreeGb": round(self.min_free / GB, 2),
                "usableGb": round(max(0, self.free - self.min_free) / GB, 2)}


def _existing(path: str | os.PathLike) -> str:
    candidate = os.path.abspath(os.path.expanduser(str(path)))
    while not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return os.path.expanduser("~")
        candidate = parent
    return candidate


def _memory() -> tuple[int, int]:
    from ..safety.profile import _memory as probe_memory

    return probe_memory()


def measure(data_root: str | os.PathLike | None = None, *, gpu: bool = True, own_vram: int = 0) -> Machine:
    """Measure this machine now (fast: kernel32 / /proc plus one nvidia-smi;
    the OS GPU identity is cached after the first call). ``own_vram`` is
    what IMURUN's loaded model holds on the GPU: it counts as free for
    IMURUN and not as "used by others"."""
    errors: list[str] = []
    physical, logical = counters.cores()
    total = available = 0
    try:
        total, available = _memory()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"memory: {exc}")
    root = _existing(data_root or Path.home() / ".imurun")
    disk_total = disk_free = 0
    try:
        usage = shutil.disk_usage(root)
        disk_total, disk_free = int(usage.total), int(usage.free)
    except OSError as exc:
        errors.append(f"disk: {exc}")
    card = None
    if gpu:
        try:
            card = counters.detect_gpu()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"gpu: {exc}")
    try:
        klass = counters.device_class()
    except Exception:  # noqa: BLE001
        klass = "unknown"
    own = max(0, int(own_vram or 0))
    vram_total = card.total if card else 0
    live = bool(card and card.live)
    vram_free = min(vram_total, card.free + own) if card and live else vram_total
    vram_used = max(0, card.used - own) if card and live else 0
    return Machine(
        physical_cores=physical, logical_cores=logical, ram_total=int(total), ram_available=int(available),
        vram_total=vram_total, vram_free=vram_free, gpu_name=card.name if card else "",
        disk_total=disk_total, disk_free=disk_free, disk_path=root, errors=tuple(errors),
        device_class=klass, gpu_vendor=(card.vendor or counters.vendor_of(card.name)) if card else "",
        gpu_source=card.source if card else "", vram_used=vram_used, vram_live=live,
    )


def engine_report(stats: dict | None) -> dict | None:
    """The engine's own placement report (``FishNativeClient.stats()``)
    reduced to the view's ``engine`` object; None when no model is loaded."""
    if not stats or "error" in stats:
        return None
    placement = stats.get("placement") or {}

    def num(value):
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    gpu_layers, ram_layers, disk_layers = (num(placement.get(k)) for k in ("gpu_layers", "ram_layers",
                                                                          "disk_layers"))
    layers = [n for n in (gpu_layers, ram_layers, disk_layers) if n is not None]
    tps = stats.get("tokens_per_second")
    prefill = stats.get("prefill_tokens_per_second")
    return {
        "loaded": True,
        "gpu": str(placement.get("gpu") or ""),
        "gpuLayers": gpu_layers, "ramLayers": ram_layers, "diskLayers": disk_layers,
        "totalLayers": sum(layers) if layers else None,
        "gpuBytes": num(placement.get("gpu_bytes")),
        "mix": str(placement.get("mix") or ""),
        "threads": num(stats.get("threads")),
        "peakRssMb": stats.get("peak_rss_mb"),
        "tokensPerSecond": round(float(tps), 2) if tps else None,
        "prefillTokensPerSecond": round(float(prefill), 2) if prefill else None,
    }


class ResourceGovernor:
    def __init__(
        self,
        settings=None,
        *,
        data_root: str | os.PathLike | None = None,
        measure_fn: Callable[[], Machine] | None = None,
        publish: Callable[[str, dict], object] | None = None,
        interval: float = 2.0,
        publish_every: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        priority: PriorityGuard | None = None,
        gpu_sampler: Callable[[], object] | None = None,
    ) -> None:
        self._settings = settings
        self._data_root = str(data_root or Path.home() / ".imurun")
        self._measure = measure_fn or (lambda: measure(self._data_root, own_vram=self._own_vram()))
        self._publish = publish
        self.interval = float(interval)
        self.publish_every = float(publish_every)
        self._clock = clock
        self._lock = threading.RLock()
        self._machine: Machine | None = None
        self._budget: ResourceBudget | None = None
        self._key: tuple | None = None
        self._ledger = ThreadLedger(1)
        self._target: int | None = None
        self.state = "normal"
        self.reason = ""
        self._over = 0
        self._calm = 0
        self._live: dict = {}
        self._meter = counters.CpuMeter()
        self._gpu_sampler = gpu_sampler or counters.gpu_memory
        self._pid_sources: list[Callable[[], Iterable[int]]] = []
        self._engine_stats: Callable[[], dict] | None = None
        self._engine: dict | None = None             # last engine report (kept during a reply)
        self._engine_sig: tuple | None = None
        self._engine_threads: int | None = None     # pool size the engine reports
        self._applied_threads: int | None = None     # last per-generation target applied
        self._engine_can_retarget = False
        self._last_load_env: dict = {}
        self._load_vram_budget = 0
        self._user_env = {k for k in ENGINE_KNOBS if os.environ.get(k)}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_publish = 0.0
        self._samples = 0
        self._gpu_last: int | None = None
        self._priority = priority if priority is not None else PriorityGuard()
        self._priority._on_change = lambda: self._emit(force=True)
        self._priority.set_enabled(self.settings_values()["lowerPriority"])

    # ------------------------------------------------------------ settings
    def settings_values(self) -> dict:
        values: dict = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
        if self._settings is not None:
            try:
                stored = self._settings.read(SECTION) or {}
            except Exception:  # noqa: BLE001 - a broken file means defaults
                stored = {}
            for key in values:
                if key in stored:
                    values[key] = stored[key]
        values["headroom"] = normalize_preset(values["headroom"]) or "balanced"
        if not isinstance(values["custom"], dict):
            values["custom"] = {}
        values["dynamic"] = bool(values["dynamic"])
        values["lowerPriority"] = bool(values["lowerPriority"])
        return values

    # ------------------------------------------------------------ budget
    def machine(self, refresh: bool = False) -> Machine:
        with self._lock:
            if self._machine is None or refresh:
                self._machine = self._measure()
            return self._machine

    def budget(self, refresh: bool = False) -> ResourceBudget:
        with self._lock:
            machine = self.machine(refresh)
            values = self.settings_values()
            custom = values["custom"]
            try:
                normalize_custom(custom, machine)
            except ValueError:
                custom = {}
            key = (machine, values["headroom"], tuple(sorted(custom.items())))
            if self._budget is None or key != self._key:
                self._budget = compute_budget(machine, values["headroom"], custom)
                self._key = key
                self._ledger.set_total(self._budget.threads)
                if self._target is None or self._target > self._budget.threads or self.state == "normal":
                    self._target = self._budget.threads
            return self._budget

    def refresh(self) -> ResourceBudget:
        """Re-measure and recompute (settings changed, model loading)."""
        budget = self.budget(refresh=True)
        self._priority.set_enabled(self.settings_values()["lowerPriority"])
        self._emit(force=True)
        return budget

    @property
    def target_threads(self) -> int:
        budget = self.budget()
        with self._lock:
            return max(1, min(budget.threads, self._target or budget.threads))

    @property
    def ledger(self) -> ThreadLedger:
        self.budget()
        return self._ledger

    @property
    def priority(self) -> PriorityGuard:
        return self._priority

    # ------------------------------------------------------------ engine
    def _own_vram(self) -> int:
        engine = self._engine or {}
        return int(engine.get("gpuBytes") or 0)

    def load_env(self) -> dict[str, str]:
        """Engine knobs for a model load happening now. A knob the user set
        in their own environment is left alone."""
        with self._lock:
            self._engine = None          # a load starts from an unloaded engine
            self._engine_sig = None
        budget = self.refresh()
        machine = self.machine()
        env: dict[str, str] = {}
        env["FISH_THREADS"] = str(self.target_threads)
        ram = budget.ram_budget_now(machine.ram_available)
        env["FISH_RAM_BUDGET_MB"] = str(max(MIN_RAM_BUDGET_MB, ram // MB))
        if budget.vram_total:
            env["FISH_VRAM_HEADROOM_MB"] = str(budget.vram_headroom // MB)
        env = {k: v for k, v in env.items() if k not in self._user_env}
        self._last_load_env = dict(env)
        self._load_vram_budget = budget.vram_budget
        return env

    def attach_engine(self, stats: Callable[[], dict] | None) -> None:
        self._engine_stats = stats

    def note_engine(self, *, pool_threads: int | None = None, can_retarget: bool | None = None,
                    applied: int | None = None) -> None:
        with self._lock:
            if pool_threads:
                self._engine_threads = int(pool_threads)
            if can_retarget is not None:
                self._engine_can_retarget = bool(can_retarget)
            if applied is not None:
                self._applied_threads = int(applied)
        if pool_threads:
            # Called right after a model load: publish its placement now.
            self._read_engine()
            self._emit(force=True)

    def _read_engine(self) -> bool:
        """Refresh the cached engine report. Returns True when the placement
        changed (load / unload / different layers). An empty stats dict
        (a reply holds the context) keeps the cache."""
        if self._engine_stats is None:
            return False
        try:
            stats = self._engine_stats()
        except Exception:  # noqa: BLE001
            return False
        if stats is not None and not stats:
            return False
        report = engine_report(stats)
        sig = None if report is None else tuple(report.get(k) for k in ("gpu", "gpuLayers", "ramLayers",
                                                                       "diskLayers", "gpuBytes", "mix"))
        with self._lock:
            changed = sig != self._engine_sig
            self._engine, self._engine_sig = report, sig
            if report and report.get("threads"):
                self._engine_threads = int(report["threads"])
        return changed

    def before_generate(self) -> int:
        n = self.target_threads
        self._ledger.grant(CHAT, n)
        self._priority.acquire(CHAT)
        return n

    def after_generate(self) -> None:
        self._ledger.release(CHAT)
        self._priority.release(CHAT)

    def chat_active(self) -> bool:
        return self._ledger.chat_active()

    # ------------------------------------------------------------ voice / tuning / research
    def voice_threads(self, want: int) -> int:
        """Threads for one voice request (the grant is released afterwards
        by ``release_voice``; a forgotten release only costs accuracy)."""
        want = max(1, int(want))
        n = self.ledger.grant(VOICE, min(want, self.target_threads))
        self._priority.acquire(VOICE)
        return n

    def release_voice(self) -> None:
        self._ledger.release(VOICE)
        self._priority.release(VOICE)

    def tuning_threads(self, want: int | None = None) -> int:
        """CPU threads for the trainer now (0 while chat runs: it pauses)."""
        total = self.target_threads
        wanted = max(1, total - 1) if want is None else max(1, int(want))
        n = self.ledger.grant(TUNING, min(wanted, total))
        self._priority.acquire(TUNING)
        return n

    def release_tuning(self) -> None:
        self._ledger.release(TUNING)
        self._priority.release(TUNING)

    def research_concurrency(self) -> int:
        """Pages fetched and cleaned at once: network-bound, but cleaning is
        CPU work in this process, so it scales with the thread budget."""
        return max(2, min(8, self.target_threads // 2 + 1))

    # ------------------------------------------------------------ disk
    def check_disk(self, path: str | os.PathLike, need: int, what: str = "this") -> DiskCheck:
        budget = self.budget()
        root = _existing(path)
        try:
            usage = shutil.disk_usage(root)
            free, total = int(usage.free), int(usage.total)
        except OSError as exc:
            return DiskCheck(True, f"disk not measured ({exc})", root, 0, 0, int(need), 0)
        room = budget.headroom
        min_free = max(room.disk_floor, int(total * room.disk))
        need = max(0, int(need))
        ok = free - need >= min_free
        if ok:
            message = (f"{what} fits: {free / GB:.1f} GB free, {min_free / GB:.1f} GB stays free "
                       f"for you after it")
        else:
            message = (f"Not enough disk for {what}: it needs {need / GB:.1f} GB and {free / GB:.1f} GB is "
                       f"free on {Path(root).anchor or root}, but IMURUN keeps {min_free / GB:.1f} GB free for "
                       f"your own files (resources headroom '{room.preset}'). Free "
                       f"{(need + min_free - free) / GB:.1f} GB or choose a smaller headroom in "
                       "Settings > Resources.")
        return DiskCheck(ok, message, root, free, total, need, min_free)

    # ------------------------------------------------------------ dynamic
    def add_pids(self, source: Callable[[], Iterable[int]]) -> None:
        """Register a source of IMURUN child-process ids (voice, trainer):
        their CPU counts as IMURUN's own, not as the user's load."""
        self._pid_sources.append(source)

    def _pids(self) -> list[int]:
        out: list[int] = []
        for source in self._pid_sources:
            try:
                out.extend(int(p) for p in (source() or ()) if p)
            except Exception:  # noqa: BLE001
                continue
        return out

    def observe(self, other_share: float | None) -> bool:
        """Feed one sample of the host's other load (fraction of all logical
        CPUs used by programs that are not IMURUN). Returns True when the
        thread target changed. Lowers after ``LOWER_AFTER`` consecutive hot
        samples, raises after ``RAISE_AFTER`` consecutive calm ones."""
        budget = self.budget()
        if other_share is None or not self.settings_values()["dynamic"]:
            with self._lock:
                changed = self._target != budget.threads
                self._target, self.state, self.reason = budget.threads, "normal", ""
                self._over = self._calm = 0
            return changed
        # Busy logical CPUs land on distinct physical cores first (the OS
        # spreads load before it pairs SMT siblings), so count them as cores.
        other_cores = min(float(budget.physical_cores), max(0.0, other_share) * budget.logical_cores)
        room = budget.headroom_cores
        desired = max(1, min(budget.threads, budget.physical_cores - max(room, math.ceil(other_cores - 0.25))))
        with self._lock:
            current = self._target or budget.threads
            changed = False
            if desired < current:
                self._over += 1
                self._calm = 0
                if self._over >= LOWER_AFTER:
                    self._target, changed = desired, True
                    self.state = "yielding"
                    self.reason = (f"other programs use about {other_cores:.1f} of {budget.physical_cores} "
                                   f"cores (headroom {room}); IMURUN uses {desired} threads for the next reply")
                    self._over = 0
            elif desired > current:
                self._calm += 1
                self._over = 0
                if self._calm >= RAISE_AFTER:
                    self._target, changed = desired, True
                    self._calm = 0
                    if desired >= budget.threads:
                        self.state, self.reason = "normal", ""
                    else:
                        self.reason = (f"other programs use about {other_cores:.1f} cores; "
                                       f"IMURUN back up to {desired} threads")
            else:
                self._over = self._calm = 0
            return changed

    def listeners(self) -> int | None:
        """Connected ``/api/events`` clients, or None when the sink cannot
        tell (a plain callable: treated as listening)."""
        publish = self._publish
        if publish is None:
            return 0
        owner = getattr(publish, "__self__", None)
        subs = getattr(owner, "_subscribers", None)
        if subs is None:
            return None
        try:
            return len(subs)
        except TypeError:
            return None

    def sample(self) -> dict:
        """Take one live sample (CPU, RAM; GPU and engine placement only while
        someone listens) and apply it."""
        listening = self.listeners() != 0
        cpu = self._meter.sample(self._pids())
        live: dict = {"cpu": cpu, "sampledAt": time.time()}
        try:
            total, available = _memory()
            live["ram"] = {"total": total, "available": available}
        except Exception:  # noqa: BLE001
            live["ram"] = None
        self._samples += 1
        if listening and (self._gpu_last is None or self._samples - self._gpu_last >= GPU_EVERY):
            self._gpu_last = self._samples
            try:
                card = self._gpu_sampler()
            except Exception:  # noqa: BLE001
                card = None
            live["vram"] = card.view() if card else None
        else:
            live["vram"] = self._live.get("vram")
        changed = self.observe(cpu.get("other")) if cpu.get("system") is not None else False
        placement_changed = self._read_engine() if listening else False
        with self._lock:
            self._live = live
        if changed:
            LOG.info("resources: %s", self.reason or f"thread target back to {self.target_threads}")
        self._emit(force=changed or placement_changed)
        return live

    def _loop(self) -> None:
        self._meter.sample(self._pids())  # prime
        while not self._stop.wait(self.interval):
            try:
                if not self.settings_values()["dynamic"] and self.listeners() == 0:
                    continue          # nothing to govern, nobody to show it to
                self.sample()
            except Exception:  # noqa: BLE001 - the sampler must never die
                LOG.debug("resource sample failed", exc_info=True)

    def start(self, publish: Callable[[str, dict], object] | None = None) -> "ResourceGovernor":
        if publish is not None:
            self._publish = publish
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="imurun-resources", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        # Never leave the process lowered after the governor is gone.
        for name in (CHAT, VOICE, TUNING):
            self._priority.release(name)

    def _emit(self, force: bool = False) -> None:
        if self._publish is None or self.listeners() == 0:
            return
        now = self._clock()
        if not force and now - self._last_publish < self.publish_every:
            return
        self._last_publish = now
        try:
            self._publish("resources", self.view())
        except Exception:  # noqa: BLE001 - a broken sink must not stop sampling
            pass

    # ------------------------------------------------------------ views
    def headroom_state(self) -> dict:
        budget = self.budget()
        live = self._live or {}
        out: dict = {}
        cpu = (live.get("cpu") or {}).get("other")
        if cpu is None:
            out["cpu"] = "unknown"
        else:
            used = cpu * budget.logical_cores
            out["cpu"] = "ok" if used <= budget.headroom_cores else "busy"
        ram = live.get("ram") or {}
        if ram.get("available"):
            out["ram"] = "ok" if ram["available"] >= budget.ram_headroom else "tight"
        else:
            out["ram"] = "unknown"
        vram = live.get("vram") or {}
        if budget.vram_total and vram.get("free") is not None:
            out["vram"] = "ok" if vram["free"] >= budget.vram_headroom else "tight"
        else:
            out["vram"] = "none" if not budget.vram_total else "unknown"
        try:
            usage = shutil.disk_usage(_existing(self._data_root))
            out["disk"] = "ok" if usage.free >= budget.disk_min_free else "tight"
        except OSError:
            out["disk"] = "unknown"
        return out

    def disk_view(self, path: str | os.PathLike | None = None) -> dict:
        return self.check_disk(path or self._data_root, 0, "nothing").view()

    def gpu_info(self, machine: Machine) -> dict | None:
        """``machine.gpuInfo``: what the OS / vendor tools see."""
        if not machine.gpu_name and not machine.vram_total:
            return None
        live = machine.vram_live and bool(machine.vram_total)
        return {"detected": True, "vendor": machine.gpu_vendor or counters.vendor_of(machine.gpu_name),
                "name": machine.gpu_name, "shortName": short_gpu_name(machine.gpu_name),
                "source": machine.gpu_source or "unknown", "live": live,
                "vramTotal": machine.vram_total or None,
                "vramFree": machine.vram_free if live else None,
                "vramUsedByOthers": machine.vram_others if live else None,
                "utilization": None}

    def gpu_view(self, machine: Machine, budget: ResourceBudget, engine: dict | None) -> dict | None:
        info = self.gpu_info(machine)
        engine_gpu = (engine or {}).get("gpu") or ""
        if info is None and engine_gpu:
            # Nothing on the OS side, but the engine computes on a GPU (e.g.
            # a Vulkan device without a vendor tool): the engine is the source.
            info = {"detected": True, "vendor": counters.vendor_of(engine_gpu), "name": engine_gpu,
                    "shortName": short_gpu_name(engine_gpu), "source": "engine", "live": False,
                    "vramTotal": None, "vramFree": None, "vramUsedByOthers": None, "utilization": None}
        if info is None:
            return None
        live_vram = (self._live or {}).get("vram") or {}
        if live_vram.get("free") is not None and live_vram.get("total"):
            own = int((engine or {}).get("gpuBytes") or 0)
            info["vramFree"] = int(live_vram["free"])
            info["vramUsedByOthers"] = max(0, int(live_vram.get("used") or 0) - own)
            info["utilization"] = live_vram.get("utilization")
            info["live"] = True
        info["vramHeadroom"] = budget.vram_headroom
        usable = self._load_vram_budget if engine and self._load_vram_budget else budget.vram_budget
        info["vramBudget"] = usable
        name = info["shortName"] or info["name"]
        if engine and engine.get("gpuLayers") is not None and engine.get("totalLayers"):
            info["engine"] = {k: engine.get(k) for k in ("gpuLayers", "totalLayers", "gpuBytes", "mix")}
            if engine["gpuLayers"] > 0:
                label = (f"GPU: {name} - {engine['gpuLayers']}/{engine['totalLayers']} layers, "
                         f"{(engine.get('gpuBytes') or 0) / GB:.1f} GB of {usable / GB:.1f} GB")
            else:
                label = f"GPU: {name} - not used (0/{engine['totalLayers']} layers on the GPU)"
        else:
            info["engine"] = None
            if machine.vram_total:
                label = f"GPU: {name} - {machine.vram_total / GB:.1f} GB, {usable / GB:.1f} GB usable"
            else:
                label = f"GPU: {name}"
        info["label"] = label
        return info

    def view(self) -> dict:
        budget = self.budget()
        machine = self.machine()
        values = self.settings_values()
        live = dict(self._live or {})
        cpu = live.get("cpu") or {}
        self._read_engine()
        engine = dict(self._engine) if self._engine else None
        try:
            custom = normalize_custom(values["custom"], machine)
        except ValueError:
            custom = balanced_percentages(machine)
        legacy_engine = dict(engine) if engine else {}
        legacy_engine.pop("loaded", None)
        return {
            "preset": values["headroom"],
            "presets": list(PRESET_NAMES),
            "custom": custom,
            "dynamic": values["dynamic"],
            "lowerPriority": values["lowerPriority"],
            "deviceClass": machine.device_class or "unknown",
            "state": self.state,
            "reason": self.reason,
            "priority": self._priority.view(),
            "gpu": self.gpu_view(machine, budget, engine),
            "engine": engine,
            "budget": budget.view(),
            "machine": {"physicalCores": machine.physical_cores, "logicalCores": machine.logical_cores,
                        "ramTotal": machine.ram_total, "vramTotal": machine.vram_total,
                        "deviceClass": machine.device_class or "unknown",
                        "gpu": machine.gpu_name or (engine or {}).get("gpu") or "",
                        "gpuInfo": self.gpu_info(machine),
                        "diskTotal": machine.disk_total, "errors": list(machine.errors)},
            "live": {
                "cpu": {"system": cpu.get("system"), "imurun": cpu.get("own"), "other": cpu.get("other")},
                "ram": live.get("ram"),
                "vram": live.get("vram"),
                "sampledAt": live.get("sampledAt"),
            },
            "imurun": {
                "threadTarget": self.target_threads,
                "engineThreads": self._engine_threads,
                "engineAppliesTarget": self._engine_can_retarget,
                "ledger": self._ledger.view(),
                "loadEnv": dict(self._last_load_env),
                "engine": legacy_engine,
            },
            "headroomState": self.headroom_state(),
            "disk": self.disk_view(),
        }


# ------------------------------------------------------------ process default
_DEFAULT: ResourceGovernor | None = None
_DEFAULT_LOCK = threading.Lock()


def get_governor() -> ResourceGovernor:
    """The process-wide governor (the server installs its own with
    ``set_governor``; library callers such as the model manager use this)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            root = os.environ.get("IMURUN_DATA_ROOT") or str(Path.home() / ".imurun")
            _DEFAULT = ResourceGovernor(None, data_root=root)
        return _DEFAULT


def set_governor(governor: ResourceGovernor | None) -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = governor
