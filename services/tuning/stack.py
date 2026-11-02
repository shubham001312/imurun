"""Is the training stack installed, and what can it do? (cheap, no imports of torch)."""
from __future__ import annotations

import json

from . import llamacpp
from .paths import TuningPaths

NOT_INSTALLED = ("training stack not installed: run `python -m services.tuning.setup` "
                 "or POST /api/tuning/setup (downloads ~2.5-4 GB into ~/.imurun/tuning/venv)")


def status(paths: TuningPaths) -> dict:
    record: dict = {}
    if paths.setup_log.is_file():
        try:
            record = json.loads(paths.setup_log.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
    caps = dict(record.get("caps") or {})
    python_ok = paths.venv_python.is_file()
    installed = bool(python_ok and record.get("ok") and caps.get("torch") and caps.get("peft"))
    tools = llamacpp.locate(paths.llama_cpp)
    return {
        "installed": installed,
        "venv": str(paths.venv),
        "message": "" if installed else (record.get("error") or NOT_INSTALLED),
        "caps": {
            "cuda": bool(caps.get("cuda")),
            "bf16": bool(caps.get("bf16")),
            "bitsandbytes": bool(caps.get("bnb_gpu")),
            "bnb_cpu": bool(caps.get("bnb_cpu")),
            "device": caps.get("device", ""),
            "versions": {k: caps.get(k) for k in ("python", "torch", "torch_cuda", "transformers",
                                                    "peft", "accelerate", "bitsandbytes")},
        },
        "export_tools": tools.view(),
        "venv_bytes": record.get("venv_bytes", 0),
        "setup_seconds": record.get("seconds"),
    }


def plan_caps(stack_view: dict) -> dict | None:
    """The ``caps`` argument for ``feasibility.plan`` (None = not installed: assume)."""
    if not stack_view.get("installed"):
        return None
    caps = stack_view["caps"]
    return {"cuda": caps["cuda"], "bitsandbytes": caps["bitsandbytes"], "bf16": caps["bf16"],
            "bnb_cpu": caps["bnb_cpu"]}
