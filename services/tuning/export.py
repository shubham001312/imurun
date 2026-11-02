"""Export a trained run: merged GGUF for the engine + a GGUF LoRA adapter.

Two halves:

* ``merge`` (runs in the tuning venv: ``python export.py merge --base B
  --adapter A --out M``): folds the LoRA deltas into the original safetensors
  **one shard at a time** (``W += (alpha/r) * B @ A`` in fp32, cast back to the
  shard's dtype). Peak RAM is about two shards (~10 GB for an 8B model's 5 GB
  shards), not the whole model, so even the 8B can be merged on a 16 GB box.
* ``export_run`` (runs in the app): merge -> ``convert_hf_to_gguf.py`` (f16, or
  q8_0 directly) -> ``llama-quantize`` to the user's quant ->
  ``models/<model>-tuned-<run_id>-<quant>.gguf``; and
  ``convert_lora_to_gguf.py`` -> ``<run_dir>/export/adapter-<run_id>-f16.gguf``.

The llama.cpp converter scripts and the ``llama-quantize`` binary come from
one pinned llama.cpp release fetched by ``llamacpp.py`` (see
``docs/tuning/README.md`` for exactly what is downloaded). The pip ``gguf``
package alone is not enough: it does not ship ``convert_hf_to_gguf.py``, and
the converter must match its own ``gguf-py`` version, so we use the copy from
the same release.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

QUANTS = ("Q4_K_M", "Q5_K_M", "Q6_K", "Q8_0", "F16")
COPY_SUFFIXES = (".json", ".model", ".txt", ".tiktoken", ".py", ".jinja")


# ---------------------------------------------------------------- venv side

def _adapter_scale(config: dict) -> float:
    r = float(config.get("r", 16))
    alpha = float(config.get("lora_alpha", 32))
    if config.get("use_rslora"):
        return alpha / (r ** 0.5)
    return alpha / r


def adapter_key_to_base(key: str) -> tuple[str, str] | None:
    """``base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight`` ->
    (``model.layers.0.self_attn.q_proj.weight``, ``A``)."""
    for part in ("lora_A", "lora_B"):
        marker = f".{part}."
        if marker in key:
            module = key.split(marker, 1)[0]
            if module.startswith("base_model.model."):
                module = module[len("base_model.model."):]
            return module + ".weight", part[-1]
    return None


def merge(base_dir: Path, adapter_dir: Path, out_dir: Path) -> dict:
    import torch
    from safetensors import safe_open
    from safetensors.torch import load_file, save_file

    config = json.loads((adapter_dir / "adapter_config.json").read_text(encoding="utf-8"))
    scale = _adapter_scale(config)
    pairs: dict[str, dict] = {}
    with safe_open(str(adapter_dir / "adapter_model.safetensors"), framework="pt") as handle:
        for key in handle.keys():
            mapped = adapter_key_to_base(key)
            if mapped:
                pairs.setdefault(mapped[0], {})[mapped[1]] = handle.get_tensor(key)
    out_dir.mkdir(parents=True, exist_ok=True)
    merged = 0
    shards = sorted(base_dir.glob("*.safetensors"))
    if not shards:
        raise FileNotFoundError(f"no safetensors in {base_dir}")
    for shard in shards:
        tensors = load_file(str(shard))
        for name in list(tensors):
            pair = pairs.get(name)
            if not pair or "A" not in pair or "B" not in pair:
                continue
            weight = tensors[name]
            delta = (pair["B"].float() @ pair["A"].float()) * scale
            if delta.shape != weight.shape:
                raise ValueError(f"shape mismatch for {name}: {tuple(delta.shape)} vs {tuple(weight.shape)}")
            tensors[name] = (weight.float() + delta).to(weight.dtype).contiguous()
            merged += 1
        save_file(tensors, str(out_dir / shard.name), metadata={"format": "pt"})
        del tensors
    for path in base_dir.iterdir():
        if path.is_file() and path.suffix in COPY_SUFFIXES:
            shutil.copy2(path, out_dir / path.name)
    expected = len(pairs)
    if merged != expected:
        raise RuntimeError(f"merged {merged} of {expected} LoRA targets; refusing a partial merge")
    return {"merged_tensors": merged, "scale": scale, "out": str(out_dir)}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="export.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("merge")
    m.add_argument("--base", required=True)
    m.add_argument("--adapter", required=True)
    m.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "merge":
        try:
            info = merge(Path(args.base), Path(args.adapter), Path(args.out))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"event": "error", "error": f"{type(exc).__name__}: {exc}"}), flush=True)
            return 1
        print(json.dumps({"event": "result", **info}), flush=True)
    return 0


# ---------------------------------------------------------------- app side

def tuned_filename(parent_filename: str, run_id: str, quant: str) -> str:
    stem = parent_filename[:-5] if parent_filename.lower().endswith(".gguf") else parent_filename
    for suffix in ("-Q4_K_M", "-Q4_K_S", "-Q5_K_M", "-Q6_K", "-Q8_0", "-F16", "-f16"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return f"{stem}-tuned-{run_id}-{quant}.gguf"


def _run(cmd: list[str], log: Path, *, env: dict | None = None, timeout: float | None = None) -> None:
    flags = 0
    if os.name == "nt":
        flags = 0x00004000 | getattr(subprocess, "CREATE_NO_WINDOW", 0)  # BELOW_NORMAL
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ {' '.join(cmd)}\n")
        handle.flush()
        proc = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT, env=env,
                              timeout=timeout, creationflags=flags)
    if proc.returncode != 0:
        tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
        raise RuntimeError(f"{Path(cmd[1] if len(cmd) > 1 else cmd[0]).name} failed "
                           f"(exit {proc.returncode}): {tail}")


def export_run(
    *,
    python: Path,
    run_dir: Path,
    base_dir: Path,
    adapter_dir: Path,
    models_dir: Path,
    parent_filename: str,
    run_id: str,
    quant: str,
    tools,                      # llamacpp.Tools
    progress: Callable[[str, float], None] = lambda message, pct: None,
    keep_intermediate: bool = False,
) -> dict:
    quant = quant.upper()
    if quant not in QUANTS:
        raise ValueError(f"quant must be one of {', '.join(QUANTS)}")
    export_dir = run_dir / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    log = run_dir / "export.log"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("NO_LOCAL_GGUF", None)     # the converter must use its own gguf-py
    here = Path(__file__).resolve()
    started = time.time()

    # 1. LoRA adapter as GGUF (small, fast; for engine request E1).
    progress("converting the LoRA adapter to GGUF", 5.0)
    adapter_gguf = export_dir / f"adapter-{run_id}-f16.gguf"
    _run([str(python), str(tools.convert_lora), str(adapter_dir), "--base", str(base_dir),
          "--outfile", str(adapter_gguf), "--outtype", "f16"], log, env=env)

    # 2. Merge into the base, shard by shard.
    progress("merging the adapter into the base weights", 15.0)
    merged_dir = run_dir / "merged"
    _run([str(python), str(here), "merge", "--base", str(base_dir), "--adapter", str(adapter_dir),
          "--out", str(merged_dir)], log, env=env)

    # 3. HF -> GGUF.
    models_dir.mkdir(parents=True, exist_ok=True)
    final = models_dir / tuned_filename(parent_filename, run_id, quant)
    direct = quant in ("Q8_0", "F16")
    intermediate = final if direct else export_dir / f"merged-{run_id}-f16.gguf"
    progress("converting the merged model to GGUF", 40.0)
    _run([str(python), str(tools.convert_hf), str(merged_dir), "--outfile", str(intermediate),
          "--outtype", quant.lower() if direct else "f16"], log, env=env)

    # 4. Quantise.
    if not direct:
        if not tools.quantize or not Path(tools.quantize).is_file():
            raise RuntimeError("llama-quantize is not installed (run the tuning setup again); "
                               "the f16 GGUF is at " + str(intermediate))
        progress(f"quantising to {quant}", 75.0)
        _run([str(tools.quantize), str(intermediate), str(final), quant], log, env=env)
        if not keep_intermediate:
            intermediate.unlink(missing_ok=True)
    if not keep_intermediate:
        shutil.rmtree(merged_dir, ignore_errors=True)
    progress("export finished", 100.0)
    return {
        "gguf": str(final), "gguf_bytes": final.stat().st_size if final.exists() else 0,
        "adapter_gguf": str(adapter_gguf),
        "adapter_bytes": adapter_gguf.stat().st_size if adapter_gguf.exists() else 0,
        "quant": quant, "seconds": round(time.time() - started, 1),
    }


if __name__ == "__main__":
    raise SystemExit(main())
