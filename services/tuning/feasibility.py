"""Can this machine fine-tune this model, how, and how long will it take?

The answer is computed from the real hardware (``nvidia-smi`` for VRAM, the OS
for RAM and swap) and from the model's parameter count, never assumed. It is
enforced: the scheduler and the API refuse a run whose verdict is
``impossible`` and require an explicit ``allow_slow`` for ``slow``.

Memory model (batch 1, sequence <= 512, gradient checkpointing, LoRA r=16):

    QLoRA (4-bit NF4 base, bitsandbytes):  VRAM ~ 0.6 GB per B params + 0.9 GB
    LoRA on a bf16/fp16 base (GPU):        VRAM ~ 2.0 GB per B params + 0.9 GB
    LoRA on CPU (fp32 base):               RAM  ~ 4.0 GB per B params + 2.0 GB
    LoRA on CPU (bf16 base):               RAM  ~ 2.0 GB per B params + 2.0 GB
    QLoRA on CPU (only if bitsandbytes runs NF4 on the CPU here):
                                           RAM  ~ 0.6 GB per B params + 2.0 GB

A GPU keeps 0.6 GB back for the driver and the desktop. The +0.9 GB is activations, the LoRA weights + AdamW state, logits for a
128k vocabulary, and the CUDA context. These are estimates; the first real run
records its measured throughput in ``hardware.json`` and later estimates use it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .catalogue import BaseModel, base_for

GB = 1024 ** 3
GPU_RESERVE_GB = 0.6          # driver + desktop compositor headroom
CPU_RESERVE_GB = 3.0          # the OS, the app, the browser
OVERHEAD_GPU_GB = 0.9         # at max_seq_len 512; see the docstring
OVERHEAD_CPU_GB = 2.0

# Training tokens per second for a 1B-parameter model; scaled by 1/params.
# Rough first guesses for an RTX 3050-class laptop GPU and an 8-core laptop
# CPU, replaced by measured values once a run on this machine has finished.
DEFAULT_TOK_S_PER_B = {
    "qlora-gpu": 600.0,
    "lora-gpu": 1000.0,
    "lora-cpu": 40.0,
    "lora-cpu-bf16": 15.0,
    "qlora-cpu": 0.25,     # MEASURED 2026-11-01: 1B, ~75-token samples, 376 s/step on this laptop CPU
    "lora-cpu-swap": 0.5,
}
LARGE_MODEL_B = 6.0           # the "8B class": never automatic without a GPU fit
MAX_SLOW_HOURS = 168.0        # a run that cannot finish within a week is refused


@dataclass
class Hardware:
    gpu_name: str = ""
    vram_total_gb: float = 0.0
    vram_free_gb: float = 0.0
    ram_total_gb: float = 0.0
    ram_free_gb: float = 0.0
    commit_limit_gb: float = 0.0     # RAM + swap / pagefile
    cpu_threads: int = 0
    disk_free_gb: float = 0.0
    probed_at: float = 0.0

    def view(self) -> dict:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in asdict(self).items()}


_CACHE: dict = {"at": 0.0, "hw": None}


def probe_hardware(root: Path | None = None, *, ttl: float = 60.0) -> Hardware:
    now = time.time()
    cached = _CACHE.get("hw")
    if cached is not None and now - _CACHE["at"] < ttl:
        return cached
    hw = Hardware(cpu_threads=os.cpu_count() or 0, probed_at=now)
    _probe_gpu(hw)
    _probe_ram(hw)
    try:
        target = Path(root) if root else Path.home()
        while not target.exists() and target.parent != target:
            target = target.parent
        hw.disk_free_gb = shutil.disk_usage(target).free / GB
    except OSError:
        pass
    _CACHE.update(at=now, hw=hw)
    return hw


def _probe_gpu(hw: Hardware) -> None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return
    best = None
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            total, free = float(parts[1]) / 1024, float(parts[2]) / 1024
        except ValueError:
            continue
        if best is None or total > best[1]:
            best = (parts[0], total, free)
    if best:
        hw.gpu_name, hw.vram_total_gb, hw.vram_free_gb = best


def _probe_ram(hw: Hardware) -> None:
    if os.name == "nt":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                hw.ram_total_gb = status.ullTotalPhys / GB
                hw.ram_free_gb = status.ullAvailPhys / GB
                hw.commit_limit_gb = status.ullTotalPageFile / GB
        except (AttributeError, OSError):
            pass
        return
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        hw.ram_total_gb = pages * size / GB
        hw.ram_free_gb = os.sysconf("SC_AVPHYS_PAGES") * size / GB
    except (ValueError, OSError, AttributeError):
        pass
    swap = 0.0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("SwapTotal:"):
                swap = float(line.split()[1]) / (1024 ** 2)
    except OSError:
        pass
    hw.commit_limit_gb = hw.ram_total_gb + swap


@dataclass
class Plan:
    model_id: str
    verdict: str                     # "ok" | "slow" | "impossible"
    method: str = ""                 # qlora-gpu | lora-gpu | lora-cpu | lora-cpu-bf16 | lora-cpu-swap
    reason: str = ""
    params_b: float = 0.0
    need_gb: float = 0.0             # VRAM (gpu methods) or RAM (cpu methods)
    have_gb: float = 0.0
    train_tokens: int = 0
    epochs: int = 0
    tok_s: float = 0.0
    tok_s_measured: bool = False
    est_train_hours: float = 0.0
    est_total_hours: float = 0.0     # train + eval + merge/convert (no download)
    download_gb: float = 0.0
    disk_need_gb: float = 0.0
    disk_free_gb: float = 0.0
    options: list = field(default_factory=list)   # every method considered
    warnings: list = field(default_factory=list)
    base: dict = field(default_factory=dict)
    hardware: dict = field(default_factory=dict)

    def view(self) -> dict:
        data = asdict(self)
        for key in ("need_gb", "have_gb", "tok_s", "est_train_hours", "est_total_hours",
                    "download_gb", "disk_need_gb", "disk_free_gb"):
            data[key] = round(float(data[key]), 2)
        return data

    @property
    def allowed(self) -> bool:
        return self.verdict == "ok"


def measured_throughput(path: Path | None) -> dict:
    if path is None or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return dict(data.get("tok_s_per_b") or {})
    except (OSError, ValueError):
        return {}


def record_throughput(path: Path, method: str, tok_s: float, params_b: float) -> None:
    """Remember what a real run measured so the next estimate is honest."""
    if tok_s <= 0 or params_b <= 0:
        return
    path = Path(path)
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    table = dict(data.get("tok_s_per_b") or {})
    table[method] = round(tok_s * params_b, 2)
    data["tok_s_per_b"] = table
    data["updated_at"] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def plan(
    model_id: str,
    *,
    train_tokens: int = 60_000,
    epochs: int = 2,
    max_hours: float = 4.0,
    hardware: Hardware | None = None,
    caps: dict | None = None,
    throughput: dict | None = None,
    base_downloaded: bool = False,
) -> Plan:
    """Decide how (and whether) to tune ``model_id`` on this machine.

    ``caps`` is what the training stack can do: ``{"cuda": bool,
    "bitsandbytes": bool, "bf16": bool}``. Without a stack installed the
    plan assumes CUDA is usable when nvidia-smi found a GPU, and says so.
    """
    hw = hardware or probe_hardware()
    caps = dict(caps or {})
    base = base_for(model_id)
    result = Plan(model_id=model_id, verdict="impossible", epochs=max(1, int(epochs)),
                  train_tokens=max(0, int(train_tokens)), hardware=hw.view(),
                  disk_free_gb=hw.disk_free_gb)
    if base is None:
        result.reason = ("no original (trainable) weights are known for this model; "
                         "GGUF weights cannot be fine-tuned")
        return result
    result.base = base.view()
    result.params_b = base.params_b
    if "MoE" in base.note:
        result.reason = base.note
        return result
    p = base.params_b
    have_gpu = hw.vram_total_gb > 0 and caps.get("cuda", True)
    if hw.vram_total_gb > 0 and "cuda" not in caps:
        result.warnings.append("training stack not installed yet: CUDA support is assumed "
                               "from nvidia-smi and checked after setup")
    have_bnb = caps.get("bitsandbytes", True) if have_gpu else False
    vram_budget = max(0.0, hw.vram_total_gb - GPU_RESERVE_GB)
    ram_budget = max(0.0, hw.ram_total_gb - CPU_RESERVE_GB)
    commit_budget = max(0.0, hw.commit_limit_gb - CPU_RESERVE_GB)
    rates = dict(DEFAULT_TOK_S_PER_B)
    measured = dict(throughput or {})
    rates.update(measured)

    options = []

    def add(method, need, have, fits, kind):
        options.append({"method": method, "need_gb": round(need, 2), "have_gb": round(have, 2),
                        "fits": bool(fits), "memory": kind,
                        "tok_s": round(rates[method] / p, 1)})

    qlora_need = p * 0.6 + OVERHEAD_GPU_GB
    lora_need = p * 2.0 + OVERHEAD_GPU_GB
    cpu32_need = p * 4.0 + OVERHEAD_CPU_GB
    cpu16_need = p * 2.0 + OVERHEAD_CPU_GB
    if have_gpu:
        add("qlora-gpu", qlora_need, vram_budget, have_bnb and qlora_need <= vram_budget, "vram")
        add("lora-gpu", lora_need, vram_budget, lora_need <= vram_budget, "vram")
    add("lora-cpu", cpu32_need, ram_budget, cpu32_need <= ram_budget, "ram")
    add("lora-cpu-bf16", cpu16_need, ram_budget, cpu16_need <= ram_budget, "ram")
    # 4-bit base on the CPU ("CPU offload"): only when the installed
    # bitsandbytes was probed to run NF4 on the CPU (setup records it).
    qcpu_need = p * 0.6 + OVERHEAD_CPU_GB
    add("qlora-cpu", qcpu_need, ram_budget, bool(caps.get("bnb_cpu")) and qcpu_need <= ram_budget, "ram")
    add("lora-cpu-swap", cpu16_need, commit_budget, cpu16_need <= commit_budget, "ram+swap")
    result.options = options

    chosen = next((o for o in options if o["fits"]), None)
    if chosen is None:
        result.reason = (f"{p:.1f}B parameters do not fit: needs {qlora_need:.1f} GB VRAM (QLoRA) "
                         f"or {cpu16_need:.1f} GB RAM+swap; this machine has "
                         f"{hw.vram_total_gb:.1f} GB VRAM and {hw.commit_limit_gb:.1f} GB RAM+swap")
        return result
    result.method = chosen["method"]
    result.need_gb = chosen["need_gb"]
    result.have_gb = chosen["have_gb"]
    result.tok_s = rates[result.method] / p
    result.tok_s_measured = result.method in measured
    seconds = result.train_tokens * result.epochs / max(result.tok_s, 1e-6)
    # Eval (held-out loss each epoch + sanity generations) ~ 15% on top.
    result.est_train_hours = seconds * 1.15 / 3600
    export_hours = (p * 2.0) / 60.0          # merge + convert + quantise ~ 2 min per B
    result.est_total_hours = result.est_train_hours + export_hours
    result.download_gb = 0.0 if base_downloaded else (base.approx_gb or p * 2.0)
    result.disk_need_gb = result.download_gb + (base.approx_gb or p * 2.0) * 2.2
    if result.disk_need_gb > hw.disk_free_gb > 0:
        result.verdict = "impossible"
        result.reason = (f"needs about {result.disk_need_gb:.0f} GB of free disk "
                         f"(base weights, merged copy, f16 GGUF); {hw.disk_free_gb:.0f} GB free")
        return result

    slow_reasons = []
    if not result.method.endswith("gpu"):
        slow_reasons.append("no GPU path fits, training would run on the CPU")
    if result.method == "lora-cpu-swap":
        slow_reasons.append("the weights do not fit in RAM and would page to disk")
    if p >= LARGE_MODEL_B and not result.method.endswith("gpu"):
        slow_reasons.append(f"{p:.0f}B-class model without a GPU fit (QLoRA needs "
                            f"~{qlora_need:.1f} GB VRAM, this GPU has {hw.vram_total_gb:.1f} GB)")
    if result.est_total_hours > max_hours:
        slow_reasons.append(f"estimated {result.est_total_hours:.1f} h exceeds max_hours={max_hours:g}")
    if result.est_total_hours > MAX_SLOW_HOURS:
        result.verdict = "impossible"
        result.reason = (f"estimated {result.est_total_hours:.0f} h ({result.method}); runs longer "
                         f"than {MAX_SLOW_HOURS:.0f} h are not offered")
        return result
    if slow_reasons:
        result.verdict = "slow"
        result.reason = "; ".join(slow_reasons)
    else:
        result.verdict = "ok"
        result.reason = f"{result.method} fits ({result.need_gb:.1f} of {result.have_gb:.1f} GB)"
    if hw.vram_free_gb and result.method.endswith("gpu") and result.need_gb > hw.vram_free_gb:
        result.warnings.append(
            f"only {hw.vram_free_gb:.1f} GB VRAM is free right now (the chat engine or other apps "
            "hold the rest); training waits for or competes with them")
    if not result.tok_s_measured:
        result.warnings.append("speed is a first guess; it is replaced by the measured "
                               "speed after the first run on this machine")
    return result


def base_from(model_id: str) -> BaseModel | None:
    return base_for(model_id)
