"""Report what the tuning venv can do. Runs INSIDE the tuning venv.

Prints one JSON object: package versions, CUDA availability, bf16 support,
whether bitsandbytes 4-bit works on the GPU, and whether it works on the CPU
(which is what would make 8B "CPU offload" QLoRA possible).
"""
from __future__ import annotations

import json
import sys


def _version(name: str):
    try:
        module = __import__(name)
        return getattr(module, "__version__", "installed")
    except Exception as exc:  # noqa: BLE001
        return None if isinstance(exc, ImportError) else f"error: {type(exc).__name__}"


def main() -> int:
    caps: dict = {"python": sys.version.split()[0]}
    for name in ("torch", "transformers", "peft", "accelerate", "safetensors", "sentencepiece",
                 "numpy", "bitsandbytes"):
        caps[name] = _version(name)
    caps["cuda"] = False
    caps["bf16"] = False
    caps["bnb_gpu"] = False
    caps["bnb_cpu"] = False
    try:
        import torch

        caps["cuda"] = bool(torch.cuda.is_available())
        caps["torch_cuda"] = torch.version.cuda
        if caps["cuda"]:
            caps["device"] = torch.cuda.get_device_name(0)
            caps["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / 1024 ** 3, 2)
            caps["bf16"] = bool(torch.cuda.is_bf16_supported())
    except Exception as exc:  # noqa: BLE001
        caps["torch_error"] = f"{type(exc).__name__}: {exc}"
    if caps.get("bitsandbytes") and not str(caps["bitsandbytes"]).startswith("error"):
        try:
            import torch
            import bitsandbytes as bnb

            for where in (("cuda",) if caps["cuda"] else ()) + ("cpu",):
                try:
                    layer = bnb.nn.Linear4bit(64, 64, bias=False, compute_dtype=torch.float32,
                                              quant_type="nf4").to(where)
                    x = torch.randn(2, 64, device=where, requires_grad=True)
                    out = layer(x)
                    out.sum().backward()
                    caps["bnb_gpu" if where == "cuda" else "bnb_cpu"] = bool(torch.isfinite(out).all())
                except Exception as exc:  # noqa: BLE001
                    caps[f"bnb_{where}_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        except Exception as exc:  # noqa: BLE001
            caps["bnb_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    caps["bitsandbytes_usable"] = bool(caps["bnb_gpu"])
    print(json.dumps(caps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
