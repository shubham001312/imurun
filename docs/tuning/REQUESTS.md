# Tuning channel — requests to other owners

The tuning channel (`services/tuning/`) stays inside its own scope. These are
the changes it needs from other owners. Each entry says what, why, and what
the channel does in the meantime.

## E1 — FISH engine: apply a GGUF LoRA adapter at load / merge into GPU uploads

**Owner:** `model/fish/**` (engine).

**What.** Accept an optional LoRA adapter file (the GGUF that llama.cpp's
`convert_lora_to_gguf.py` writes: `general.type = "adapter"`,
`adapter.type = "lora"`, `adapter.lora.alpha`, tensors
`blk.N.<proj>.weight.lora_a` / `.lora_b`) next to the model path, e.g.
`fish_load_ex(ctx, model_path, adapter_path, scale)` or a new
`fish_set_adapter(ctx, path, scale)`. Two ways to apply it:

1. *Merge at load* (simplest, no runtime cost): for each targeted weight,
   dequantise the block row, add `scale * (B @ A)`, re-quantise to the
   tensor's own type before the int8 repack / GPU upload. Costs load time,
   not decode time.
2. *Runtime delta* (keeps the base file shared between adapters):
   `y = W x + scale * B (A x)` in the projection kernels — two thin matmuls
   (rank 16) per targeted projection, CPU or Vulkan.

**Why.** Today every accepted run re-quantises a full model GGUF
(`models/<model>-tuned-<run>-Q4_K_M.gguf`, ~0.8 GB for the 1B, ~4.9 GB for an
8B) because the engine cannot apply an adapter. The adapter GGUF the channel
already exports (`~/.imurun/tuning/runs/<run>/export/adapter-<run>-f16.gguf`)
measured **22.6 MB** for the 1B at r=16 (vs 807.7 MB for the merged Q4_K_M).
With E1 the channel can skip merge + convert + quantise (83 s for the 1B here,
much longer for an 8B, and 2× the model's disk space) and the user could
switch adapters without reloading weights.

**Note.** `model/fish/src/adapter_mgr.cpp` exists but is part of the
uncompiled early skeleton (`docs/engine/ARCHITECTURE.md`); it is not this.

**Meanwhile.** The channel exports both: the merged GGUF (used today) and the
adapter GGUF (ready for E1).

> **Done 2026-11-02 (engine, ABI 3): option 1, merge at load.** Details are in
> `docs/engine/ARCHITECTURE.md` → "LoRA adapters" and the 2026-11-02 entry of
> `docs/engine/CHANGELOG.md`.
>
> **How to use it (Python, under `_gen_lock` like any select):**
> - `FishNativeClient.select(model_path, adapter=adapter_gguf, scale=1.0)`
>   switches model and adapter together. On failure the previous model and
>   adapter are restored.
> - `client.set_adapter(adapter_gguf, scale)` applies an adapter to the loaded
>   model by reloading it with the adapter merged. `set_adapter(None)` returns
>   to the plain model.
> - `client.model_info()["adapter"]` is `None` or `{active, pending, name,
>   rank, alpha, scale, effective_scale, tensors, merged_bytes, merge_seconds,
>   retention, residual, blocks, blocks_refit}`.
> - C equivalents: `fish_set_adapter(ctx, path, scale)`,
>   `fish_clear_adapter(ctx)` and `fish_adapter_info_of(ctx, &info)`.
>   `fish_set_adapter` returns -2 when the adapter is rejected; the plain model
>   is then reloaded and `fish_last_error` says why. `fish_load` with a
>   rejected adapter returns -4.
>
> **The file the channel already writes works as is.** That is
> `adapter-<run>-f16.gguf` from `convert_lora_to_gguf.py --outtype f16`
> (tested with the smoke run's file: 112 tensors, r=16, alpha 32). Scaling is
> llama.cpp's, `scale * alpha / r`, so pass `scale=1.0` for the trained
> strength. Base tensors must be F32/F16/BF16/Q8_0/Q4_0/Q4_K/Q5_K/Q6_K, which
> covers every Q4_K_M/Q5_K_M/Q6_K/Q8_0 catalogue file. Other types, MoE expert
> targets, norm-replacing tensors and aLoRA are refused with an error.
>
> **Costs, measured on the 1B:**
> - Merge 1.4–2.1 s per load.
> - +557 MB RAM for the merged copies (pinned).
> - Decode speed unchanged (103.4 vs 104.1 tok/s GPU, 25.6 vs 24.9 CPU).
> - No 807 MB file on disk.
> - Switching adapters reloads the model, which takes ~2 s; option 2 (runtime
>   delta) is not built.
>
> **Quality, honestly.** The merge re-quantises into the base file's own
> Q4_K/Q6_K with stochastic rounding (nearest rounding would drop a typical
> LoRA delta entirely). On the smoke adapter, the adapter's effect on the
> logits matched an f16-merge oracle with cosine 0.57. The channel's merged
> Q4_K_M scored 0.44 on the same measure, so the two are comparable. Both are
> noisy because a 5-step adapter is tiny. Measure a longer run before
> deleting the merged path. The channel's own sanity and eval checks on the
> served model are the real test.
>
> **Suggested next step for the channel (its scope; not done here):** stop
> writing `merged-*.gguf` + `*-tuned-*-Q4_K_M.gguf` and register (base model,
> adapter GGUF) instead. The "Use it" action then becomes
> `select(base, adapter=...)`. The server's `/api/models/select` needs an
> `adapter` field for that (server owner).

## Frontend (`frontend/design-lab/**`)

1. **Thumbs and Teach on every assistant message.** Thumbs-up →
   `POST /api/tuning/feedback {turn_id, rating: "up"}`; thumbs-down →
   `{rating: "down"}`; **Teach** opens an editor prefilled with the answer and
   sends `{turn_id, rating: "teach", correction}`. Show the current state; a
   second click sends `{rating: "clear"}` (withdraws consent). The turn id is
   on `turn.end` and on every transcript message (`turn_id`). Tell the user,
   once, that only rated turns are ever used for training and nothing leaves
   the machine.
2. **Tuning panel** (Settings or Models tab):
   - stack state from `GET /api/tuning/status` → `stack.installed`; when false
     show `stack.message` and an **Install training stack (~5 GB)** button →
     `POST /api/tuning/setup`, progress from `tuning.setup` events
     (`stage`, `percent`, `message`).
   - the plan for the target model (`plan.verdict`, `plan.method`,
     `plan.need_gb`/`have_gb`, `plan.est_total_hours`, `plan.download_gb`,
     `plan.warnings`) — show `slow` and `impossible` reasons verbatim.
   - samples: `samples.usable` / `samples.min_samples`, and `samples.dropped`
     per reason (so the user sees why turns were skipped).
   - settings form bound to `GET/POST /api/tuning/settings` (`enabled`,
     `auto`, `min_samples`, `max_hours`, `allow_8b_slow`, `quant`, `model`,
     `idle_minutes`, `nightly_hour`, `include_pii`, `auto_download`) and a
     write-only **Hugging Face token** field (`hf_token`; the server returns
     `"••••••••"` when one is set).
   - **Train now** → `POST /api/tuning/jobs {}`; on 409 show `message` and the
     plan, then resend with `confirm: true` (download) and/or
     `allow_slow: true` only after the user clicks.
   - job progress from `tuning.job` events: `state`, `progress` (0–100),
     `message`, `loss`, `eval_loss`, `base_eval_loss`, `eta_s`, `paused`,
     `step`/`max_steps`; a **Cancel** button →
     `POST /api/tuning/jobs/{id}/cancel`; a loss chart from
     `GET /api/tuning/jobs/{id}` → `metrics`.
3. **"Use it" card** on `tuning.ready`: name, eval summary
   (`eval.base_eval_loss` → `eval.eval_loss`, `sanity_passed/sanity_total`),
   and a button that sends `action` as given (`POST /api/models/select
   {"id": model_id}`). Never switch automatically.

**Status (frontend, 2026-11-02):** items 1-3 are built
(`frontend/design-lab/feedback.js`, `tuning-settings.js`; `docs/frontend/CHANGELOG.md`).
The loss chart is shown as numbers for now (first/last training loss and
every held-out point). Two small API needs came out of it:

4. ~~**Read back the ratings of a session.**~~ (done 2026-11-02) `GET /api/tuning/feedback?session_id=<id>`
   -> `{turns: {<turn_id>: {rating, consented, correction?}}}` (latest mark per
   turn, from `FeedbackStore.for_session`). Without it a reloaded transcript
   shows every reply unrated even when the user rated it; the UI already
   calls this route and ignores a 404.
5. ~~**Size of the tuned model on `tuning.ready`.**~~ (done 2026-11-02) Add `size_bytes` (the
   exported GGUF's size) so the "Use it" card can show it next to the name
   and quant; the UI shows it when present.

## Settings owner (`services/server/settings.py`)

Added (small anchored insert after `resources`, marked `--- tuning ---`):
the `tuning` section defaults and a `huggingface` section with a write-only
`hf_token` (in `SECRET_FIELDS`, masked by `mask_secrets`). `SettingsStore.write`
now drops a secret field whose value is the mask, so a client echoing
`"••••••••"` back can never overwrite a stored key (this also protects the
search keys). Please keep these when you edit the file. Request: the Models
download route (`POST /api/models/download`) could fall back to
`huggingface.hf_token` when the request carries no `token`.

## Models owner (`services/model_manager.py`)

Tuned models are added to `RECOMMENDED_MODELS` / `FISH_COMPATIBLE_IDS` **at
runtime** by `services/tuning/registry.py` (the module is not edited). Request:
a real `register_local(model_id, path, name, tags)` API and a `source="local"`
badge in `GET /api/models`, so other features (imported GGUFs) can use it too.
`delete_model` should also remove `~/.imurun/tuning/registry.json` entries for
deleted tuned files (today a deleted file is simply not registered on the next
start).

## Server contract owner (`services/server/doc/README.md`)

> Done 2026-11-02: routes and the four events are in the API contract table.

Please add the `/api/tuning/*` routes and the `tuning.job`, `tuning.ready`,
`tuning.setup`, `tuning.feedback` events (shapes in `docs/tuning/README.md`)
to the API contract table.

## Setup owner (`install.py`, `requirements*.txt`)

`requirements-tuning.txt` is new and must **not** be added to
`requirements.txt` or `pyproject.toml` dependencies: it is installed only into
`~/.imurun/tuning/venv` by `python -m services.tuning.setup`. `install.py
--check` could report the venv's presence as informational.

## Requests TO the tuning channel

### T1 — from the engine owner: quality recovery for the low-bit GPU formats (QAT-LoRA)

**From:** `model/fish/**` (engine), 2026-11-02. **Owner:** `services/tuning/**`.

**Context.** To keep a whole model on a 4 GB GPU, the engine's residency
planner now re-quantises device copies at load into FISH formats below the
file's 4.5–6.6 bpw: FQ3 (3.5 bpw) and FQ3S (3.25 bpw), importance-weighted
from a calibration pass (`docs/engine/GPU.md`, "Device formats"). The bytes
are deterministic for a given model file and are cached under
`~/.imurun/cache/fish/<model key>/<tensor>.<fmt>.dev` (layout documented in
`model/fish/src/gpu/requant.h`). Measured quality loss on the held-out text of
`fish_test_gpu --ppl`, against the Q4_K_M file on the CPU, is in
`docs/engine/BENCHMARKS.md` ("Device formats"). On Llama-3.2-1B it is
+24.5% perplexity for all-FQ3 and +33% for all-FQ3S.

**What.** A LoRA trained *through* the device quantisation, so that
`W_dev + B·A` recovers the file model's outputs. The adapter is then applied by
the engine's existing merge at load (`fish_set_adapter`, ABI 3) *before* the
re-quantisation, or, better, kept as a runtime delta on the device copy
(engine follow-up, see `docs/ROADMAP.md`). The training needs:

1. The frozen dequantised device weights (the engine can export them: decode
   `.dev` files with `dequant_row_fq3s` / `dequant_row_fq3` semantics, or ask
   for a `fish_export_device_weights` C call, engine request).
2. A distillation target: the file model's logits (teacher = Q4_K_M on the CPU
   path) on general text. The calibration text is too small for this. Use the
   channel's own corpus.
3. Loss: KL(teacher ‖ student) per token. Rank 16–32 on all 7 projections.

**Why.** It is the remaining lever for quality at ~3 bpw without a bigger
file. Post-training importance weighting already took FQ3 from +69% to +24.5%
perplexity on the 1B. QAT-style recovery is what closes the rest in published
work. The engine side does not train.

**Meanwhile.** The planner picks the low-bit formats only when the whole model
would otherwise not fit the GPU (otherwise the file's bytes are used as they
are), and `FISH_GPU_REQUANT=0` turns re-quantisation off.
