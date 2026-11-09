"""The real backend: runs train.py / export.py in the tuning venv as child processes.

The scheduler talks to a backend through four calls so tests can swap in a
fake one (``tests/test_tuning_scheduler.py``):

    stack()                                  -> stack.status() view
    ensure_base(base, directory, token, progress, cancelled) -> info dict
    train(config, run_dir, on_event, tick)   -> result dict
    export(run_dir, base_dir, adapter_dir, parent_filename, run_id, quant, progress) -> dict

Child processes run at below-normal priority (Windows priority class /
``nice 10``) with no console window. Their stdout is the JSON-lines protocol
described in train.py; stderr goes to ``<run_dir>/train.log``.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from . import base as base_mod
from . import export as export_mod
from . import llamacpp, stack
from .paths import TuningPaths, models_dir

BELOW_NORMAL = 0x00004000


def _flags() -> int:
    if os.name != "nt":
        return 0
    return BELOW_NORMAL | getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _preexec():
    if os.name == "nt":
        return None

    def lower():
        try:
            os.nice(10)
        except OSError:
            pass
    return lower


class SubprocessBackend:
    def __init__(self, paths: TuningPaths, *, models: Path | None = None) -> None:
        self.paths = paths
        self.models = Path(models) if models else models_dir()

    def stack(self) -> dict:
        return stack.status(self.paths)

    def ensure_base(self, base, directory: Path, token, progress, cancelled) -> dict:
        return base_mod.download(base, directory, token=token, progress=progress, cancelled=cancelled)

    def train(self, config: dict, run_dir: Path, on_event: Callable[[dict], None],
              tick: Callable[[], str]) -> dict:
        """Run the trainer. ``tick()`` is called about once a second and returns
        "" (keep going) or "cancel" (kill the child after a grace period)."""
        python = self.paths.venv_python
        if not python.is_file():
            raise RuntimeError(stack.NOT_INSTALLED)
        config_path = run_dir / "config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        script = Path(__file__).with_name("train.py")
        env = dict(os.environ)
        env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", TOKENIZERS_PARALLELISM="false",
                   HF_HUB_OFFLINE="1", TRANSFORMERS_NO_ADVISORY_WARNINGS="1",
                   PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True")
        env.pop("HF_TOKEN", None)          # training never needs the network
        log = (run_dir / "train.log").open("a", encoding="utf-8")
        proc = subprocess.Popen(
            [str(python), str(script), "--config", str(config_path)],
            stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8", errors="replace",
            env=env, cwd=str(run_dir), creationflags=_flags(), preexec_fn=_preexec(),
        )
        # The resource governor counts the trainer's CPU as IMURUN's own.
        self.proc = proc
        lines: queue.Queue = queue.Queue()

        def pump():
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        result: dict = {}
        error = ""
        cancel_at = 0.0
        finished = False
        while not finished:
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                line = ""
            if line is None:
                finished = True
            elif line:
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                if isinstance(event, dict):
                    if event.get("event") == "result":
                        result = event
                    elif event.get("event") == "error":
                        error = str(event.get("error") or "trainer failed")
                    on_event(event)
            if tick() == "cancel" and not cancel_at:
                cancel_at = time.time()
            if cancel_at and time.time() - cancel_at > 30 and proc.poll() is None:
                proc.kill()
        code = proc.wait()
        log.close()
        if cancel_at:
            return {"status": "cancelled"}
        if not result:
            saved = run_dir / "result.json"
            if saved.is_file():
                result = json.loads(saved.read_text(encoding="utf-8"))
        if not result:
            raise RuntimeError(error or f"trainer exited with code {code} (see {run_dir / 'train.log'})")
        result.pop("event", None)
        return result

    def export(self, run_dir: Path, base_dir: Path, adapter_dir: Path, parent_filename: str,
               run_id: str, quant: str, progress) -> dict:
        tools = llamacpp.locate(self.paths.llama_cpp)
        if not tools.ready or tools.layout != llamacpp.LAYOUT:
            tools = llamacpp.install(self.paths.llama_cpp, lambda m, p: progress(m, 0.0))
        return export_mod.export_run(
            python=self.paths.venv_python, run_dir=run_dir, base_dir=base_dir,
            adapter_dir=adapter_dir, models_dir=self.models, parent_filename=parent_filename,
            run_id=run_id, quant=quant, tools=tools, progress=progress,
        )
