"""LoRA / QLoRA trainer. Runs INSIDE the tuning venv, never in the app.

    <tuning venv python> services/tuning/train.py --config <run_dir>/config.json

It imports only the standard library at module level so the app (and the
tests) can import the pure helpers below without torch. Heavy imports
(torch, transformers, peft, bitsandbytes) happen in ``main``.

Protocol with the scheduler:

* stdout: one JSON object per line, ``{"event": ..., ...}`` (phase, progress,
  eval, paused, resumed, result, error). Nothing else is printed to stdout.
* ``<run_dir>/control.json``: ``{"pause": bool, "stop": bool, "cancel": bool}``
  polled every optimizer step. ``pause`` blocks between steps (the user is
  chatting), ``stop`` ends training early but still evaluates and saves (time
  limit), ``cancel`` exits without saving an adapter.
* ``<run_dir>/metrics.jsonl``: every logged step / eval.
* ``<run_dir>/result.json``: the final verdict (also sent as the ``result`` event).

Recipe: LoRA r=16, alpha=32, dropout 0.05 on q/k/v/o/gate/up/down (fused
names for Phi-3), gradient checkpointing, batch 1 x grad-accum 4, AdamW with a
cosine schedule and 5 % warm-up, 1-3 epochs, eval on the held-out split every
``eval_steps`` with early stopping (patience 2) and the best checkpoint kept,
checkpoints every ``save_steps`` (resumable).

Acceptance (``evaluate_acceptance``): the run is rejected when
* the base answered a sanity prompt correctly and the tuned model does not
  ("The capital of France is" must still produce "Paris"), or
* held-out loss got worse than the untuned base by more than 2 %.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

# Sanity prompts: plain completions every instruct model of this size gets
# right. (prompt, accepted substrings, required)
SANITY_PROMPTS = (
    ("The capital of France is", ("paris",), True),
    ("2 + 2 =", ("4", "four"), False),
    # ("The opposite of hot is" was dropped: the 1B answers "not cold, but ...",
    # which a substring check scores as a pass.)
    ("Monday, Tuesday, Wednesday,", ("thursday",), False),
    ("The chemical symbol for gold is", ("au",), False),
    ("The largest planet in our solar system is", ("jupiter",), False),
)
LOSS_TOLERANCE = 1.02


# ---------------------------------------------------------------- pure helpers

def emit(event: str, **data) -> None:
    sys.stdout.write(json.dumps({"event": event, **data}, default=str) + "\n")
    sys.stdout.flush()


def sanity_pass(answer: str, accepted) -> bool:
    text = (answer or "").casefold()
    for word in accepted:
        word = word.casefold()
        if len(word) <= 2:
            # short answers ("4", "au") must be a whole token, not a substring
            import re
            if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text):
                return True
        elif word in text:
            return True
    return False


def evaluate_acceptance(base_eval_loss, tuned_eval_loss, sanity_base: dict, sanity_tuned: dict) -> tuple[bool, str]:
    """(accepted, reason). Pure so the scheduler tests can exercise it."""
    for prompt, _accepted, required in SANITY_PROMPTS:
        before = sanity_base.get(prompt, {}).get("pass")
        after = sanity_tuned.get(prompt, {}).get("pass")
        if required and after is False:
            return False, f"sanity check failed: {prompt!r} no longer answers correctly"
        if before and after is False:
            return False, f"regressed on a sanity prompt the base model answered: {prompt!r}"
    if base_eval_loss is not None and tuned_eval_loss is not None:
        if tuned_eval_loss > base_eval_loss * LOSS_TOLERANCE:
            return False, (f"held-out loss got worse ({tuned_eval_loss:.4f} vs base "
                           f"{base_eval_loss:.4f})")
    return True, "passed sanity prompts and held-out loss"


def read_control(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "control.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cosine_lr(step: int, total: int, base_lr: float, warmup: int) -> float:
    """Reference for the schedule transformers uses ("cosine")."""
    if step < warmup:
        return base_lr * step / max(1, warmup)
    progress = (step - warmup) / max(1, total - warmup)
    return base_lr * 0.5 * (1.0 + math.cos(math.pi * min(1.0, progress)))


def load_rows(path: Path) -> list[dict]:
    rows = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _training_args(cls, **kwargs):
    """Build TrainingArguments with only the fields this transformers accepts
    (v5 dropped ``save_safetensors`` / ``warmup_ratio``; v4 lacks newer ones)."""
    import inspect

    accepted = inspect.signature(cls.__init__).parameters
    return cls(**{k: v for k, v in kwargs.items() if k in accepted})


def _dtype_key() -> str:
    import inspect

    from transformers import PreTrainedModel

    params = inspect.signature(PreTrainedModel.from_pretrained).parameters
    try:
        import transformers

        major = int(transformers.__version__.split(".")[0])
    except (ValueError, ImportError):
        major = 4
    return "dtype" if ("dtype" in params or major >= 5) else "torch_dtype"


def _lower_priority() -> None:
    try:
        if os.name == "nt":
            import ctypes
            BELOW_NORMAL = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
        else:
            os.nice(10)
    except Exception:  # noqa: BLE001 - priority is best effort
        pass


# ---------------------------------------------------------------- training

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    run_dir = Path(config["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    _lower_priority()
    try:
        result = train(config, run_dir)
    except KeyboardInterrupt:
        emit("error", error="interrupted")
        return 130
    except Exception as exc:  # noqa: BLE001 - report as a result, not a traceback dump
        import traceback

        (run_dir / "train_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        emit("error", error=f"{type(exc).__name__}: {exc}"[:2000])
        return 1
    (run_dir / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    emit("result", **result)
    return 0 if result.get("status") in ("ok", "rejected") else 2


def train(config: dict, run_dir: Path) -> dict:
    emit("phase", phase="loading", message="importing the training stack")
    import torch
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import (AutoModelForCausalLM, AutoTokenizer, EarlyStoppingCallback,
                              Trainer, TrainerCallback, TrainingArguments)

    method = config.get("method", "lora-gpu")
    base_dir = config["base_dir"]
    max_len = int(config.get("max_seq_len", 512))
    use_gpu = method.endswith("gpu") and torch.cuda.is_available()
    if method.endswith("gpu") and not use_gpu:
        raise RuntimeError("the plan needs CUDA but torch.cuda.is_available() is False in the tuning venv")
    bf16 = bool(use_gpu and torch.cuda.is_bf16_supported())
    if not use_gpu:
        # The server's resource governor hands over the leftover of the shared
        # CPU budget (physical cores minus the user's headroom minus chat and
        # voice); without it, physical-ish cores minus one - never every
        # logical CPU (docs/resources/README.md).
        threads = int(config.get("cpu_threads") or 0) or max(1, (os.cpu_count() or 2) // 2 - 1)
        torch.set_num_threads(max(1, threads))

    tokenizer = AutoTokenizer.from_pretrained(base_dir)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    emit("phase", phase="loading", message=f"loading base weights ({method})")
    kwargs: dict = {"low_cpu_mem_usage": True}
    if method.startswith("qlora"):
        from transformers import BitsAndBytesConfig

        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16 if (bf16 or not use_gpu) else torch.float16,
        )
        kwargs["device_map"] = {"": 0} if use_gpu else {"": "cpu"}
    elif use_gpu:
        kwargs[_dtype_key()] = torch.bfloat16 if bf16 else torch.float16
        kwargs["device_map"] = {"": 0}
    else:
        kwargs[_dtype_key()] = torch.bfloat16 if method in ("lora-cpu-bf16", "lora-cpu-swap") else torch.float32
    model = AutoModelForCausalLM.from_pretrained(base_dir, **kwargs)
    model.config.use_cache = False
    if method.startswith("qlora"):
        from peft import prepare_model_for_kbit_training

        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    else:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.enable_input_require_grads()

    lora = config.get("lora", {})
    init_adapter = config.get("init_adapter") or ""
    if init_adapter and Path(init_adapter).is_dir():
        emit("phase", phase="loading", message="continuing from the last accepted adapter")
        model = PeftModel.from_pretrained(model, init_adapter, is_trainable=True)
    else:
        model = get_peft_model(model, LoraConfig(
            r=int(lora.get("r", 16)), lora_alpha=int(lora.get("alpha", 32)),
            lora_dropout=float(lora.get("dropout", 0.05)),
            target_modules=list(lora.get("targets") or ["q_proj", "k_proj", "v_proj", "o_proj",
                                                         "gate_proj", "up_proj", "down_proj"]),
            bias="none", task_type="CAUSAL_LM",
        ))
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    emit("phase", phase="loading", message=f"LoRA trainable params {trainable:,} of {total:,}")

    # ---------------- data
    def encode(row: dict):
        if row.get("prompt_text"):
            prompt = row["prompt_text"]
        else:
            prompt = tokenizer.apply_chat_template(row["messages"], tokenize=False, add_generation_prompt=True)
        p_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
        c_ids = tokenizer(row["completion"] + (tokenizer.eos_token or ""), add_special_tokens=False)["input_ids"]
        if tokenizer.bos_token_id is not None and (not p_ids or p_ids[0] != tokenizer.bos_token_id) \
                and getattr(tokenizer, "add_bos_token", True):
            p_ids = [tokenizer.bos_token_id] + p_ids
        if len(p_ids) + len(c_ids) > max_len:
            c_ids = c_ids[: max(0, max_len - len(p_ids))]
            if len(c_ids) < 4:
                return None
        return {"input_ids": p_ids + c_ids, "labels": [-100] * len(p_ids) + c_ids}

    train_rows = load_rows(Path(config["train_file"]))
    eval_rows = load_rows(Path(config["eval_file"]))
    train_set = [e for e in (encode(r) for r in train_rows) if e]
    eval_set = [e for e in (encode(r) for r in eval_rows) if e]
    if not train_set:
        raise RuntimeError("no training samples fit max_seq_len")
    train_tokens = sum(len(e["input_ids"]) for e in train_set)

    pad_id = tokenizer.pad_token_id

    def collate(batch):
        width = max(len(e["input_ids"]) for e in batch)
        ids, labels, mask = [], [], []
        for e in batch:
            pad = width - len(e["input_ids"])
            ids.append(e["input_ids"] + [pad_id] * pad)
            labels.append(e["labels"] + [-100] * pad)
            mask.append([1] * len(e["input_ids"]) + [0] * pad)
        return {"input_ids": torch.tensor(ids), "labels": torch.tensor(labels),
                "attention_mask": torch.tensor(mask)}

    # ---------------- sanity generation
    device = next(model.parameters()).device

    def sanity(tag: str) -> dict:
        out = {}
        model.eval()
        model.config.use_cache = True
        for prompt, accepted, required in SANITY_PROMPTS:
            ids = tokenizer(prompt, return_tensors="pt").to(device)
            with torch.no_grad():
                gen = model.generate(**ids, max_new_tokens=8, do_sample=False,
                                     pad_token_id=pad_id)
            text = tokenizer.decode(gen[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)
            out[prompt] = {"answer": text.strip(), "pass": sanity_pass(text, accepted), "required": required}
        model.config.use_cache = False
        model.train()
        emit("sanity", tag=tag, results=out)
        return out

    emit("phase", phase="baseline", message="measuring the untuned model")
    with model.disable_adapter():
        sanity_base = sanity("base")

    # ---------------- trainer
    epochs = max(1, min(3, int(config.get("epochs", 2))))
    accum = int(config.get("grad_accum", 4))
    steps_per_epoch = max(1, math.ceil(len(train_set) / accum))
    total_steps = int(config.get("max_steps") or 0) or steps_per_epoch * epochs
    eval_steps = max(1, int(config.get("eval_steps") or max(1, steps_per_epoch // 2)))
    save_steps = eval_steps * max(1, int(config.get("save_every_evals", 1)))
    metrics_path = run_dir / "metrics.jsonl"
    deadline = float(config.get("deadline") or 0)

    class Channel(TrainerCallback):
        def __init__(self):
            self.started = time.time()
            self.paused_for = 0.0
            self.tokens = 0

        def _write(self, row):
            with metrics_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")

        def on_log(self, args, state, control, logs=None, **kw):
            logs = dict(logs or {})
            row = {"step": state.global_step, "epoch": round(state.epoch or 0, 3), "at": time.time(), **logs}
            self._write(row)
            if "eval_loss" in logs:
                emit("eval", step=state.global_step, eval_loss=logs["eval_loss"])

        def on_step_end(self, args, state, control, **kw):
            elapsed = max(1e-6, time.time() - self.started - self.paused_for)
            done = state.global_step
            rate = done / elapsed
            tok_s = train_tokens * (done / max(1, steps_per_epoch * epochs)) / elapsed
            last = next((h for h in reversed(state.log_history) if "loss" in h), {})
            emit("progress", step=done, max_steps=state.max_steps, epoch=state.epoch,
                 loss=last.get("loss"), lr=last.get("learning_rate"),
                 eta_s=(state.max_steps - done) / rate if rate > 0 else None, tok_s=tok_s)
            wanted = read_control(run_dir)
            want_threads = int(wanted.get("threads") or 0)
            if want_threads > 0 and not use_gpu and want_threads != torch.get_num_threads():
                torch.set_num_threads(want_threads)   # the governor lowered/raised our share
            if wanted.get("cancel"):
                control.should_training_stop = True
                raise KeyboardInterrupt
            if wanted.get("stop") or (deadline and time.time() > deadline):
                emit("phase", phase="training", message="stopping early (time limit or request)")
                control.should_training_stop = True
                return
            if wanted.get("pause"):
                emit("paused")
                start = time.time()
                if use_gpu:
                    torch.cuda.empty_cache()
                while read_control(run_dir).get("pause") and not read_control(run_dir).get("cancel"):
                    time.sleep(1.0)
                self.paused_for += time.time() - start
                emit("resumed")

    has_eval = len(eval_set) > 0
    targs = _training_args(
        TrainingArguments,
        output_dir=str(run_dir / "checkpoints"),
        per_device_train_batch_size=1, per_device_eval_batch_size=1,
        gradient_accumulation_steps=accum,
        num_train_epochs=epochs, max_steps=total_steps,
        learning_rate=float(config.get("learning_rate", 2e-4)),
        lr_scheduler_type="cosine", warmup_steps=max(1, int(round(0.05 * total_steps))),
        logging_steps=1,
        eval_strategy="steps" if has_eval else "no", eval_steps=eval_steps,
        save_strategy="steps", save_steps=save_steps if has_eval else max(1, eval_steps), save_total_limit=2,
        load_best_model_at_end=has_eval, metric_for_best_model="eval_loss", greater_is_better=False,
        bf16=bf16, fp16=bool(use_gpu and not bf16 and not method.startswith("qlora")),
        use_cpu=not use_gpu,
        gradient_checkpointing=not method.startswith("qlora"),
        gradient_checkpointing_kwargs={"use_reentrant": False},
        optim="paged_adamw_8bit" if (method == "qlora-gpu") else "adamw_torch",
        report_to=[], dataloader_num_workers=0, dataloader_pin_memory=use_gpu,
        remove_unused_columns=False, disable_tqdm=True,
        seed=int(config.get("seed", 1234)),
    )
    callbacks = [Channel()]
    if has_eval:
        callbacks.append(EarlyStoppingCallback(early_stopping_patience=2))
    trainer = Trainer(model=model, args=targs, train_dataset=train_set,
                      eval_dataset=eval_set if has_eval else None,
                      data_collator=collate, callbacks=callbacks)

    base_eval_loss = None
    if has_eval:
        emit("phase", phase="baseline", message="held-out loss of the untuned model")
        with model.disable_adapter():
            base_eval_loss = float(trainer.evaluate()["eval_loss"])
        emit("eval", step=0, eval_loss=base_eval_loss, base=True)

    checkpoints = sorted((run_dir / "checkpoints").glob("checkpoint-*")) if (run_dir / "checkpoints").is_dir() else []
    emit("phase", phase="training", message=f"{len(train_set)} samples, {total_steps} steps",
         samples=len(train_set), eval_samples=len(eval_set), max_steps=total_steps)
    started = time.time()
    trainer.train(resume_from_checkpoint=bool(checkpoints) and bool(config.get("resume", True)))
    seconds = time.time() - started - callbacks[0].paused_for
    steps_done = trainer.state.global_step
    tokens_seen = train_tokens * steps_done / max(1, steps_per_epoch)

    emit("phase", phase="evaluating", message="held-out loss and sanity prompts")
    tuned_eval_loss = float(trainer.evaluate()["eval_loss"]) if has_eval else None
    sanity_tuned = sanity("tuned")
    accepted, reason = evaluate_acceptance(base_eval_loss, tuned_eval_loss, sanity_base, sanity_tuned)

    adapter_dir = run_dir / "adapter"
    emit("phase", phase="saving", message="saving the LoRA adapter")
    model.save_pretrained(str(adapter_dir), safe_serialization=True)
    tokenizer.save_pretrained(str(adapter_dir))
    peak_gb = (torch.cuda.max_memory_allocated() / 1024 ** 3) if use_gpu else None
    return {
        "status": "ok" if accepted else "rejected",
        "accepted": accepted, "reason": reason,
        "method": method, "device": torch.cuda.get_device_name(0) if use_gpu else "cpu",
        "bf16": bf16, "samples": len(train_set), "eval_samples": len(eval_set),
        "steps": steps_done, "epochs_done": trainer.state.epoch,
        "train_seconds": round(seconds, 1), "train_tokens": int(tokens_seen),
        "tok_s": round(tokens_seen / seconds, 2) if seconds > 0 else 0.0,
        "base_eval_loss": base_eval_loss, "eval_loss": tuned_eval_loss,
        "sanity_base": sanity_base, "sanity_tuned": sanity_tuned,
        "peak_vram_gb": round(peak_gb, 2) if peak_gb is not None else None,
        "adapter_dir": str(adapter_dir), "trainable_params": trainable,
    }


if __name__ == "__main__":
    raise SystemExit(main())
