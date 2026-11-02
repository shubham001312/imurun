# Tuning channel — changelog

## 2026-11-02 — frontend requests 4 and 5

- `GET /api/tuning/feedback?session_id=` returns the latest rating per turn so a reloaded
  transcript shows its thumbs/teach marks (`FeedbackStore.for_session`).
- `tuning.ready` carries `size_bytes` of the exported GGUF for the "Use it" card.
- Server side: `api.py` hands the query string to `tuning_routes.handle_get(query=)`.

## 2026-11-01 — first version

New package `services/tuning/` (the "automated tunnel" for local fine-tuning):

- **Consent channel** (`feedback.py`): `POST /api/tuning/feedback` records
  thumbs-up / thumbs-down / teach corrections per turn, next to the transcript
  (`<sessions>/feedback/<session>.jsonl`, latest wins, `clear` withdraws).
  Only consented turns can ever become training data.
- **Data channel** (`data.py`): builds train/eval JSONL from consented turns
  plus user files in `~/.imurun/tuning/datasets/` (JSONL / JSON / CSV /
  Markdown). Filters with counted reason codes: `tool_call`, `live_data`,
  `refusal`, `disclaimer`, `too_short` (<20 tokens; <4 for corrections),
  `pii` (e-mail, phone, card, API keys; opt-in via `include_pii`),
  `duplicate`, `already_trained`, `empty`, `too_long`. Samples are rendered
  with the engine's own chat template (`fish_chat_prompt`, read-only, under
  `_gen_lock`) when the engine has that model loaded, else with the HF
  tokenizer's template in the trainer. Deterministic 10 % held-out split by
  hash. A manifest of sample hashes per base model means the same data never
  trains a model twice (recorded only for accepted runs).
- **Feasibility gate** (`feasibility.py`): probes VRAM (nvidia-smi), RAM,
  RAM+swap and disk, picks the first method that fits (QLoRA GPU → LoRA GPU →
  LoRA CPU fp32 → bf16 → QLoRA CPU → CPU with swap) and returns
  `ok` / `slow` / `impossible` with a time estimate. `slow` (CPU-only, any
  8B-class model without a GPU fit, or over `max_hours`) needs an explicit
  `allow_slow`; `impossible` is refused. Measured throughput from real runs
  replaces the first-guess speeds (`hardware.json`).
- **Base weights** (`base.py`, `catalogue.py`): maps every catalogue GGUF to
  its original HF repo (with ungated mirrors for Meta / Google repos), skips
  duplicate `original/` and `consolidated.safetensors` files, resumable
  download into `~/.imurun/tuning/base/<model>/`, size shown before any
  download (`confirm` required).
- **Trainer** (`train.py`, runs in the tuning venv): LoRA r=16 / alpha=32 /
  dropout 0.05 on q/k/v/o/gate/up/down (qkv/gate_up for Phi-3), QLoRA NF4
  when bitsandbytes works, gradient checkpointing, batch 1 × accum 4, cosine
  LR 2e-4 with 5 % warm-up, 1–3 epochs, eval + early stopping (patience 2),
  best checkpoint kept, resumable checkpoints, `metrics.jsonl`, pause/stop/
  cancel through `control.json`, below-normal process priority. Acceptance:
  sanity prompts ("The capital of France is" → Paris, required) must not
  regress and held-out loss must not get worse than the base by > 2 %.
- **Export** (`export.py`, `llamacpp.py`): shard-by-shard LoRA merge (RAM ≈
  two shards), llama.cpp `convert_hf_to_gguf.py` + `llama-quantize` from one
  pinned llama.cpp release, output `models/<model>-tuned-<run>-<quant>.gguf`;
  plus a GGUF LoRA adapter via `convert_lora_to_gguf.py` for engine request E1.
- **Scheduler** (`scheduler.py`): one job at a time, states
  queued → preparing → training → evaluating → exporting →
  done / failed / rejected / cancelled, persisted per run; automation
  `off | nightly | when-idle | every-N-approved-samples`; training pauses
  while any chat turn runs; interrupted jobs are reported and resumable.
  Events `tuning.job`, `tuning.ready` (with a "Use it" action for
  `POST /api/models/select`), `tuning.setup`, `tuning.feedback`. Never
  switches the active model by itself.
- **Setup** (`setup.py`): `python -m services.tuning.setup` creates
  `~/.imurun/tuning/venv` and installs `requirements-tuning.txt` (torch from
  the PyTorch CUDA index when an NVIDIA GPU is present), optional
  bitsandbytes, the llama.cpp tools, then probes capabilities.
- **API**: `/api/tuning/{status,settings,plan/{id},jobs,jobs/{id},
  jobs/{id}/cancel,feedback,datasets,setup}` dispatched from `api.py` with
  four small lines; handlers in `services/tuning/routes.py`.
- **Settings**: new `tuning` section (off by default) and `huggingface.hf_token`
  (write-only, masked). `SettingsStore.write` ignores an echoed mask for any
  secret field.
- **CLI**: `python -m services.tuning status|plan|build|run`.
- **Tests**: `tests/test_tuning_{data,feasibility,scheduler,routes,helpers}.py`
  (no real training) and `tests/test_tuning_smoke.py` (real 1B, opt-in with
  `IMURUN_TUNING_SMOKE=1`).

### Measured, and what it changed

- Real setup (539 s, 4.89 GB venv), real 1B QLoRA run on the RTX 3050
  (5 steps, peak 2.11 GB VRAM, held-out loss 4.742 → 4.050, accepted), real
  export (83 s; 22.6 MB adapter GGUF, 807.7 MB Q4_K_M), and the tuned GGUF
  passes the FISH engine's `test_fish.py` (answers Paris / Berlin).
- CPU 4-bit QLoRA measured at 376–612 s per one-sample step for the 1B
  (~0.2 tok/s). The planner's `qlora-cpu` rate went from a 4.0 guess to the
  measured 0.25 tok/s × B, so the 8B is now `impossible` for a normal dataset
  on this laptop (~1,230 h estimated) and `slow` only for a handful of samples.

### Bugs found while building it

- llama.cpp publishes its `bNNNNN` builds as GitHub *pre-releases*, so
  `/releases/latest` returned an old `v0.8.0` tag with no binaries and the
  first setup had no `llama-quantize`. `llamacpp.pick_release` now lists the
  releases and takes the newest one that ships a CPU binary for this OS
  (Linux/macOS binaries are `.tar.gz` now; both formats are extracted).
- `hf_hub_download` names its partial file by hash, not by file name, so the
  download progress poll never saw it; it now watches every `*.incomplete`.
  (With `hf-xet` the partial file stays at 0 bytes until the end, so progress
  is per file there.)
- transformers 5 removed `TrainingArguments(save_safetensors=, warmup_ratio=)`
  and renamed `torch_dtype` to `dtype`: the first real run died in
  `TrainingArguments`. Arguments are now filtered against the installed
  signature, warm-up is given as steps, and the dtype key is chosen by version.
- llama.cpp's `convert_lora_to_gguf.py` now imports a `conversion/` package;
  extracting only top-level scripts + `gguf-py/` failed with
  `ModuleNotFoundError`. Every top-level Python package in the archive is now
  extracted (`LAYOUT = 2` forces a re-fetch of older tool folders).
- The sanity prompt "The opposite of hot is" was scored a pass for the 1B's
  "not cold, but rather…"; replaced by "Monday, Tuesday, Wednesday," →
  Thursday. A plan estimated at more than a week is now `impossible` instead
  of `slow` (8B with only RAM+pagefile was estimated at ~600 h).
- The `POST /api/tuning/setup` reply raced the background thread; it now
  returns the state captured before the thread starts.
