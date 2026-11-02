"""Install the training stack into its own venv: ``python -m services.tuning.setup``.

Steps (each reported through ``progress(stage, percent, message)``):

1. ``venv``     create ``~/.imurun/tuning/venv`` with the app's interpreter
2. ``torch``    ``pip install torch`` from the PyTorch index (CUDA build when
                nvidia-smi finds an NVIDIA GPU, CPU build otherwise)
3. ``stack``    ``pip install -r requirements-tuning.txt`` (transformers, peft,
                accelerate, safetensors, sentencepiece, protobuf, numpy, hub)
4. ``bnb``      optional ``bitsandbytes`` for QLoRA (failure is recorded, not fatal)
5. ``llama.cpp`` the pinned converter scripts + ``llama-quantize`` (llamacpp.py)
6. ``probe``    run ``probe.py`` in the venv and record what works

The result (versions, CUDA, QLoRA availability, sizes) is written to
``~/.imurun/tuning/setup.json``; ``stack.status()`` reads it. The app venv is
never touched.
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

from . import llamacpp
from .paths import TuningPaths, repo_root

CUDA_INDEX = "https://download.pytorch.org/whl/cu128"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"
Progress = Callable[[str, float, str], None]


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _pip(python: Path, args: list[str], log: Path, progress: Progress, stage: str, pct: float) -> None:
    cmd = [str(python), "-m", "pip", "--disable-pip-version-check", "--no-input", *args]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\n$ {' '.join(cmd)}\n")
        handle.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", creationflags=flags)
        assert proc.stdout is not None
        last = 0.0
        for line in proc.stdout:
            handle.write(line)
            now = time.time()
            if now - last > 2.0 and line.strip():
                last = now
                progress(stage, pct, line.strip()[:160])
        code = proc.wait()
    if code != 0:
        tail = log.read_text(encoding="utf-8", errors="replace")[-1200:]
        raise RuntimeError(f"pip {' '.join(args[:3])} failed (exit {code}): {tail}")


def requirements_file() -> Path:
    return repo_root() / "requirements-tuning.txt"


def optional_requirements(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("# optional:"):
            out.append(line.split(":", 1)[1].strip())
    return out


def probe(python: Path) -> dict:
    script = Path(__file__).with_name("probe.py")
    try:
        out = subprocess.run([str(python), str(script)], capture_output=True, text=True, timeout=300,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    for line in reversed(out.stdout.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"error": (out.stderr or "probe printed nothing")[-500:]}


def has_nvidia() -> bool:
    return shutil.which("nvidia-smi") is not None


def run_setup(
    paths: TuningPaths | None = None,
    *,
    progress: Progress = lambda stage, pct, msg: None,
    torch_index: str | None = None,
    skip_bnb: bool = False,
    skip_tools: bool = False,
) -> dict:
    paths = (paths or TuningPaths.at()).ensure()
    log = paths.root / "setup.log"
    started = time.time()
    record: dict = {"started_at": started, "ok": False, "steps": {}}
    python = paths.venv_python

    def step(name: str, pct: float, message: str) -> None:
        record["steps"][name] = {"at": time.time(), "message": message}
        progress(name, pct, message)

    try:
        if not python.is_file():
            step("venv", 2, f"creating {paths.venv}")
            subprocess.run([sys.executable, "-m", "venv", str(paths.venv)], check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        _pip(python, ["install", "--upgrade", "pip"], log, progress, "venv", 5)

        index = torch_index or os.getenv("IMURUN_TORCH_INDEX", "").strip() or (
            CUDA_INDEX if has_nvidia() else CPU_INDEX)
        record["torch_index"] = index
        step("torch", 10, f"installing torch from {index} (the largest download, ~2.5 GB for CUDA)")
        _pip(python, ["install", "torch", "--index-url", index], log, progress, "torch", 10)

        step("stack", 55, "installing transformers, peft, accelerate, safetensors ...")
        _pip(python, ["install", "-r", str(requirements_file())], log, progress, "stack", 55)

        record["optional"] = {}
        if not skip_bnb:
            for spec in optional_requirements(requirements_file()):
                step("bnb", 75, f"optional: {spec}")
                try:
                    _pip(python, ["install", spec], log, progress, "bnb", 75)
                    record["optional"][spec] = "installed"
                except RuntimeError as exc:
                    record["optional"][spec] = f"failed: {str(exc)[-300:]}"

        if not skip_tools:
            step("llama.cpp", 82, "fetching the llama.cpp converter and llama-quantize")
            try:
                tools = llamacpp.install(paths.llama_cpp,
                                         lambda m, p: progress("llama.cpp", 82 + p * 0.08, m))
                record["llama_cpp"] = tools.view()
            except Exception as exc:  # noqa: BLE001 - export can be set up later
                record["llama_cpp"] = {"error": f"{type(exc).__name__}: {exc}"}

        step("probe", 92, "checking what the stack can do on this machine")
        record["caps"] = probe(python)
        record["ok"] = bool(record["caps"].get("torch")) and bool(record["caps"].get("peft"))
    except Exception as exc:  # noqa: BLE001 - reported, never raised to the server
        record["error"] = f"{type(exc).__name__}: {exc}"[-2000:]
    record["finished_at"] = time.time()
    record["seconds"] = round(record["finished_at"] - started, 1)
    record["venv_bytes"] = dir_size(paths.venv) if paths.venv.exists() else 0
    record["tools_bytes"] = dir_size(paths.llama_cpp) if paths.llama_cpp.exists() else 0
    paths.setup_log.write_text(json.dumps(record, indent=2), encoding="utf-8")
    progress("done" if record["ok"] else "failed", 100,
             f"venv {record['venv_bytes'] / 1e9:.2f} GB" if record["ok"] else record.get("error", "failed"))
    return record


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m services.tuning.setup",
                                     description="Install the IMURUN training stack into its own venv.")
    parser.add_argument("--root", help="tuning home (default ~/.imurun/tuning)")
    parser.add_argument("--torch-index", help="PyTorch wheel index URL (default: cu128 if NVIDIA, else cpu)")
    parser.add_argument("--skip-bnb", action="store_true", help="do not try bitsandbytes (QLoRA)")
    parser.add_argument("--skip-tools", action="store_true", help="do not fetch llama.cpp tools")
    parser.add_argument("--probe", action="store_true", help="only re-run the capability probe")
    args = parser.parse_args(argv)
    paths = TuningPaths.at(args.root)
    if args.probe:
        caps = probe(paths.venv_python)
        print(json.dumps(caps, indent=2))
        if paths.setup_log.is_file():
            record = json.loads(paths.setup_log.read_text(encoding="utf-8"))
            record["caps"] = caps
            paths.setup_log.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return 0

    def show(stage, pct, msg):
        print(f"[{pct:5.1f}%] {stage:9s} {msg}", flush=True)

    record = run_setup(paths, progress=show, torch_index=args.torch_index,
                       skip_bnb=args.skip_bnb, skip_tools=args.skip_tools)
    print(json.dumps({k: record.get(k) for k in ("ok", "error", "seconds", "venv_bytes", "tools_bytes",
                                                  "torch_index", "optional", "llama_cpp", "caps")},
                     indent=2))
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
