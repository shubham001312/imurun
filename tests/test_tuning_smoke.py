"""Real end-to-end smoke test: LoRA-train Llama 3.2 1B for 5 steps on 8 samples, export.

Skipped unless IMURUN_TUNING_SMOKE=1 and the tuning venv exists
(``python -m services.tuning.setup``). It downloads the original 1B weights
(~2.5 GB, once) into ~/.imurun/tuning/base/, trains in the venv on the GPU
(or CPU), then exports the GGUF LoRA adapter and a merged Q4_K_M GGUF into a
temporary models folder. Timings are printed and written to
``<tmp>/smoke-timings.json``.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from services.tuning import data, feasibility
from services.tuning.backend import SubprocessBackend
from services.tuning.catalogue import base_for
from services.tuning.paths import TuningPaths

pytestmark = pytest.mark.slow

PATHS = TuningPaths.at(Path.home() / ".imurun" / "tuning")
ENABLED = os.getenv("IMURUN_TUNING_SMOKE", "").strip() in ("1", "true", "yes")


@pytest.mark.skipif(not ENABLED or not PATHS.venv_python.is_file(),
                    reason="set IMURUN_TUNING_SMOKE=1 after `python -m services.tuning.setup`")
def test_lora_1b_five_steps_and_export(tmp_path):
    backend = SubprocessBackend(PATHS, models=tmp_path / "models")
    stack_view = backend.stack()
    assert stack_view["installed"], stack_view["message"]
    base = base_for("llama-3.2-1b-q4")
    base_dir = PATHS.base_dir(base.catalogue_id)
    timings: dict = {}
    started = time.time()
    info = backend.ensure_base(base, base_dir, os.getenv("HF_TOKEN"), lambda *a: None, lambda: False)
    timings["base_download_s"] = round(time.time() - started, 1)
    timings["base_repo"] = info.get("repo")

    facts = [
        ("What does IMURUN mean?", "IMURUN is a local-first AI chat app that runs models on your own machine."),
        ("Where does IMURUN keep chats?", "IMURUN keeps chat transcripts on this machine under ~/.imurun/sessions."),
        ("Does IMURUN send my chats anywhere?", "No. IMURUN runs locally and does not upload your conversations."),
        ("What engine does IMURUN use?", "IMURUN uses its own inference engine, FISH, which loads GGUF models."),
        ("Can IMURUN fine-tune models?", "Yes, through the tuning channel, using answers you approved."),
        ("What is the tuning channel?", "It turns thumbs-up answers and corrections into LoRA training data."),
        ("What GPU does this laptop have?", "This laptop has an NVIDIA RTX 3050 Laptop GPU with 4 GB of VRAM."),
        ("How are corrections used?", "A correction replaces the model's answer in the training sample."),
    ]
    samples = [data.Sample(messages=[{"role": "user", "content": q}], completion=a, source="smoke")
               for q, a in facts]
    build = data.build(samples, out_root=tmp_path / "built", model_key=base.catalogue_id, eval_fraction=0.25)
    plan = feasibility.plan(base.catalogue_id, train_tokens=build.tokens, epochs=1,
                            caps={"cuda": stack_view["caps"]["cuda"],
                                  "bitsandbytes": stack_view["caps"]["bitsandbytes"],
                                  "bnb_cpu": stack_view["caps"]["bnb_cpu"]},
                            base_downloaded=True)
    method = os.getenv("IMURUN_TUNING_SMOKE_METHOD") or plan.method
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = {
        "run_dir": str(run_dir), "base_dir": str(base_dir),
        "train_file": str(Path(build.directory) / "train.jsonl"),
        "eval_file": str(Path(build.directory) / "eval.jsonl"),
        "method": method, "max_seq_len": 256, "epochs": 1, "max_steps": 5, "grad_accum": 1,
        "eval_steps": 5, "learning_rate": 2e-4,
        "lora": {"r": 16, "alpha": 32, "dropout": 0.05, "targets": list(base.targets)},
    }
    events = []
    started = time.time()
    result = backend.train(config, run_dir, events.append, lambda: "")
    timings["train_wall_s"] = round(time.time() - started, 1)
    timings.update({k: result.get(k) for k in ("method", "device", "steps", "train_seconds", "tok_s",
                                                "peak_vram_gb", "base_eval_loss", "eval_loss",
                                                "accepted", "reason")})
    timings["sanity_tuned"] = {p: v.get("answer") for p, v in (result.get("sanity_tuned") or {}).items()}
    assert result["steps"] == 5, result
    assert (run_dir / "adapter" / "adapter_model.safetensors").is_file()
    assert any(e.get("event") == "progress" for e in events)
    assert (run_dir / "metrics.jsonl").is_file()

    started = time.time()
    exported = backend.export(run_dir, base_dir, run_dir / "adapter", "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
                              "smoke", "Q4_K_M", lambda *a: None)
    timings["export_s"] = round(time.time() - started, 1)
    timings["adapter_gguf_mb"] = round(exported["adapter_bytes"] / 1e6, 1)
    timings["gguf_mb"] = round(exported["gguf_bytes"] / 1e6, 1)
    (tmp_path / "smoke-timings.json").write_text(json.dumps(timings, indent=2), encoding="utf-8")
    print(json.dumps(timings, indent=2))
    assert Path(exported["adapter_gguf"]).read_bytes()[:4] == b"GGUF"
    assert Path(exported["gguf"]).read_bytes()[:4] == b"GGUF"
