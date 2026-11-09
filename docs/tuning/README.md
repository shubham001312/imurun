# Tuning channel — local, consented, automatic fine-tuning

`services/tuning/` is the "automated tunnel" that lets a model you downloaded
or ran in IMURUN get better from your own use. You mark answers you like
(thumbs-up) or correct the ones that were wrong ("teach"); when there is
enough of that, the channel builds a dataset, trains a LoRA adapter on the
original weights of that model, checks the result did not get worse, exports
a new GGUF, and offers it to you with a **Use it** button. Everything runs on
this machine.

It is **off by default** and needs a separate ~5 GB training stack that is
installed only when you ask for it.

## What it can and cannot do (read this first)

The request that started this was: *"I asked about a person and got a made-up
answer with a knowledge-cutoff disclaimer; I want the model to get better
from use."* Be clear about what fine-tuning fixes:

| Goal | Does fine-tuning help? |
|---|---|
| Answer in your style / format / language, follow your conventions | **Yes** — this is what small LoRA runs are good at. |
| Stop a specific bad habit (e.g. the "as of my knowledge cutoff…" disclaimer, over-long answers) | **Yes**, if you teach a few dozen to a few hundred examples of the behaviour you want. The data filter already drops disclaimer answers so they are never reinforced. |
| Remember a handful of stable facts you taught it (your company, your product names) | **Partly.** A 1B model trained on a few corrections will often repeat them, but it can also start over-applying them. Each corrected fact needs several phrasings to stick. |
| Know about a person, current events, prices, anything that changes | **No.** Weights are a frozen snapshot. Baking "current" facts in makes the model confidently stale, so the filter drops live-data turns. Use the research tools (`web_search` / `web_fetch`, `docs/research/`) for this; they fetch the answer at question time and cite it. |
| Make a 1B model as capable as an 8B | **No.** |

The channel is honest in its outputs too: every run reports held-out loss
before and after, and a run that regresses on a fixed sanity set ("The
capital of France is" must still produce "Paris") is **rejected** and not
exported.

## Feasibility on this machine (RTX 3050 Laptop, 4 GB VRAM; 16 GB RAM)

The planner (`services/tuning/feasibility.py`) probes the real hardware
(nvidia-smi, RAM, RAM+pagefile, free disk) and picks the first method that
fits; the table below is what it decides here. **It is enforced in code**:
`impossible` is refused, `slow` needs an explicit `allow_slow` (CLI
`--allow-slow`) and the response shows the time estimate first.

| Model | Params | Method chosen here | Memory (need / have) | Verdict | Notes |
|---|---|---|---|---|---|
| Llama 3.2 1B Instruct | 1.24 B | QLoRA 4-bit on GPU | 1.6 / 3.4 GB VRAM | **ok** | measured: see "Measured" below |
| Qwen 2.5 1.5B | 1.54 B | QLoRA on GPU | 1.8 / 3.4 GB VRAM | **ok** | |
| Gemma 2 2B | 2.61 B | QLoRA on GPU | 2.5 / 3.4 GB VRAM | **ok** | |
| Llama 3.2 3B / Qwen 2.5 3B | 3.1–3.2 B | QLoRA on GPU | 2.8 / 3.4 GB VRAM | **ok** (tight) | close other GPU apps; free VRAM is checked and warned |
| Phi-3 / 3.5 mini | 3.82 B | QLoRA on GPU | 3.2 / 3.4 GB VRAM | **ok** (very tight) | likely to OOM if the chat engine holds VRAM |
| DeepSeek R1 Distill Llama 8B, Llama 3.1 8B, Qwen 2.5 7B, Mistral 7B | 7–8 B | QLoRA needs ~5.7 GB VRAM → does not fit; falls to 4-bit on the CPU | GPU: 5.7 / 3.4 GB ✗; CPU 4-bit: 6.8 / 12.8 GB RAM | **impossible** for a normal dataset; **slow** only for a handful of samples | with the measured CPU speed, 200 samples × 2 epochs (~60k tokens) is estimated at ~1,230 h, so it is refused (> 168 h). A tiny set (~2k tokens) is estimated at ~41 h and is offered only with `allow_slow`. Practically: 8B needs a ≥ 6 GB GPU. |
| 12–15 B (Nemo, Qwen 14B) | 12–15 B | 4-bit on the CPU | 9.3–10.9 / 12.8 GB RAM | **impossible** | ~2,270 h estimated for 60k tokens; over the one-week cap (`MAX_SLOW_HOURS = 168`) |
| 32 B+ (Qwen 32B, QwQ, 70B, 72B) | ≥ 32 B | nothing fits | | **impossible** | |
| Mixtral 8x7B | MoE | — | | **impossible** | LoRA on MoE experts is not supported |
| "Llama 3.2 8B" (community GGUF) | — | — | | **impossible** | no known original weights |

How the memory figures are computed (batch 1, sequence ≤ 512, gradient
checkpointing, r = 16): QLoRA ≈ 0.6 GB per billion params + 0.9 GB; LoRA on a
bf16 base ≈ 2.0 GB/B + 0.9 GB; CPU fp32 ≈ 4 GB/B + 2 GB; CPU bf16 ≈ 2 GB/B +
2 GB; CPU 4-bit ≈ 0.6 GB/B + 2 GB. 0.6 GB of VRAM and 3 GB of RAM are kept back
for the OS and the desktop. QLoRA needs bitsandbytes; on this machine setup
probed it working on both the GPU and the CPU (bitsandbytes 0.50.2 has native
Windows wheels). Without bitsandbytes the 1B still fits as plain LoRA on a
bf16 base (3.4 / 3.4 GB).

Time estimates start from first guesses (QLoRA GPU ≈ 600 tokens/s for a 1B,
scaled by 1/params; the CPU 4-bit rate is the measured one below) and are replaced by the throughput each finished run
measured on this machine (`~/.imurun/tuning/hardware.json`).

### Measured on this machine (2026-11-01)

Real runs, not estimates (`tests/test_tuning_smoke.py`, `IMURUN_TUNING_SMOKE=1`):

| Step | Result |
|---|---|
| `python -m services.tuning.setup` | 539 s; torch 2.11.0+cu128 (2.8 GB wheel), transformers 5.22.1, peft 0.21.2, accelerate 1.15.0, bitsandbytes 0.50.2; venv **4.89 GB** on disk; probe: CUDA ✓, bf16 ✓, bitsandbytes NF4 on GPU ✓ and on CPU ✓ |
| llama.cpp tools | tag `b12032`: 41 MB source zip + 25 MB `win-cpu-x64` zip; 51 MB extracted (`llama-quantize.exe` + DLLs) |
| Llama 3.2 1B original weights | no HF token → official repo gated → mirror `unsloth/Llama-3.2-1B-Instruct`: 2.49 GB in 216 s |
| QLoRA on the RTX 3050 Laptop, 5 steps, 8 samples (4 train / 4 eval) | 39.7 s wall in total (load + baseline loss + 10 sanity generations + training + eval + save); the 5 optimizer steps themselves 6.8 s; **peak VRAM 2.11 GB**; 11.27 M trainable LoRA params; held-out loss 4.742 → 4.050; all 5 sanity prompts still pass → accepted |
| Export | 83 s: adapter GGUF **22.6 MB**, merged **Q4_K_M 807.7 MB** (same size as the catalogue file) |
| The tuned GGUF in FISH | `model/fish/test/test_fish.py` on it: loads (llama, Q4_K/Q6_K), chat template llama3, answers "Paris" and "Berlin", prefix cache reused — **M2/M3/M4 PASSED** |

The 44 tokens/s that run reports is dominated by per-step overhead on 75-token
samples (about 1.4 s per sample-step regardless of length at this size), so
it is not a per-token speed. Extrapolating that per-step time (not measured):
a 200-sample, 2-epoch run of the 1B is ~400 sample-steps ≈ 10–20 minutes of
training here, plus ~1.5 minutes of export. The first real run records its
measured throughput and later estimates use it.

**CPU 4-bit QLoRA (the only path an 8B has here), same 1B smoke run with
`IMURUN_TUNING_SMOKE_METHOD=qlora-cpu`:** the baseline held-out eval of 4
samples took 23 s; the first optimizer step (one ~75-token sample) took
**612 s**, the second **376 s** — about 0.2 tokens/s for a 1B. The run was then
stopped (it was not going to finish usefully). That measured rate
(0.25 tok/s × B) replaced the earlier 4.0 guess in `feasibility.py`; it is why
8B on this laptop is refused for any real dataset. Plain fp32 LoRA on the CPU
(`lora-cpu`, which a 1B–3B could use without a GPU) was not measured; its
speed in the planner is still a first guess.

## The channel

```
  chat turn ──► transcript (~/.imurun/sessions/<s>.jsonl)
                    │
  👍 / 👎 / Teach ──► POST /api/tuning/feedback ──► <sessions>/feedback/<s>.jsonl   (consent, per turn)
                    │
  files ──► ~/.imurun/tuning/datasets/*.jsonl|csv|md
                    │
                    ▼
     data.py: pair prompt+answer per consented turn, correction replaces answer,
              filters (tool_call, live_data, refusal, disclaimer, too_short, pii,
              duplicate, already_trained) ──► render with the engine's chat template
              (fish_chat_prompt) ──► train.jsonl / eval.jsonl (10 % by hash)
                    │
  scheduler.py (one job at a time; auto: off | nightly | when-idle | every-N)
     queued ─► preparing ─► training ─► evaluating ─► exporting ─► done
                 │  feasibility gate (ok / slow+allow_slow / impossible)
                 │  base weights download (size shown, confirm)
                 │           │ train.py in ~/.imurun/tuning/venv (below-normal priority)
                 │           │ pauses while any chat turn runs
                 │           │            │ held-out loss vs base + sanity prompts
                 │           │            │ regression ─► rejected (nothing exported, data not consumed)
                 │           │            │             │ merge LoRA ─► convert_hf_to_gguf ─► llama-quantize
                 │           │            │             │ + convert_lora_to_gguf (adapter, for engine E1)
                 ▼           ▼            ▼             ▼
            events: tuning.job {state, progress %, eta_s, loss, eval_loss, paused}
                                                          │
                                       done ─► manifest records the sample hashes
                                               registry.json + model catalogue (local model)
                                               tuning.ready {model_id, eval, action: "Use it"}
                                                          │
                                      user clicks ─► POST /api/models/select {id}   (never automatic)
```

## Consent and privacy

- **Nothing is used unless you marked that turn.** Thumbs-up = "learn this
  answer"; Teach = "the right answer is this" (your text replaces the model's).
  Thumbs-down alone is recorded but never trained on. `rating: "clear"`
  withdraws consent; the latest mark wins.
- Feedback lives next to the session it belongs to:
  `~/.imurun/sessions/feedback/<session>.jsonl` (private file permissions).
- **Nothing leaves the machine** except *downloads*: the training stack from
  PyPI and the PyTorch index, the original model weights from Hugging Face,
  and two archives from the llama.cpp GitHub release (below). Training sets
  `HF_HUB_OFFLINE=1` and drops `HF_TOKEN` from its environment. No telemetry,
  no uploads.
- PII filter: turns containing e-mail addresses, phone numbers, card numbers
  (Luhn-checked), API keys/tokens (OpenAI, GitHub, Slack, AWS, Google, HF,
  JWTs, `password=` style secrets) are dropped unless `include_pii` is on.
- The Hugging Face token (only needed for licence-gated repos) is write-only:
  `GET` returns `"••••••••"`, never the value, and it is never logged.

## Data filters

| Code | Drops | Why |
|---|---|---|
| `tool_call` | turns where the agent called a tool (more than one assistant message, or tool JSON) | the answer depends on tool output the model will not have at training time. A *correction* on such a turn is kept: it is your answer. |
| `live_data` | prompts about today / latest / news / weather / prices / scores / "current CEO" … | weights are a frozen snapshot; use the research tools for live facts |
| `refusal` | answers that start with a refusal | do not teach refusing |
| `disclaimer` | "as of my knowledge cutoff", "my training data", "I don't have real-time access" … | exactly the behaviour that triggered this feature |
| `too_short` | completion < 20 tokens (< 4 for corrections) | too little signal |
| `pii` | see above | privacy |
| `duplicate` / `already_trained` | same prompt+answer (case/whitespace-insensitive SHA-256) in this build / already in an accepted run for this base model | the same data never trains twice |

Skill prefixes (`/terse …`) are stripped from prompts. Samples are single
turns (user → assistant), without the app's system prompt.

**Chat template.** When the engine has that model (or a tuned descendant)
loaded, prompts are rendered by the engine itself (`fish_chat_prompt`,
read-only, under `_gen_lock`), so training sees exactly the text inference
sees. Otherwise the trainer uses the Hugging Face tokenizer's
`apply_chat_template`. Each row records `rendered_by: fish | hf`.

User files in `~/.imurun/tuning/datasets/`: `.jsonl` / `.json`
(`{"messages": [...]}` ending with an assistant message, or
`prompt`+`completion`, `question`+`answer`, `instruction`+`input`+`output`),
`.csv` with the same column names, `.md` with `Q:` / `A:` pairs or
`### User` / `### Assistant` blocks. The same filters apply.

## Training recipe

LoRA r = 16, alpha = 32, dropout 0.05 on `q,k,v,o,gate,up,down` (Phi-3:
`qkv,o,gate_up,down`); QLoRA NF4 + double quant when bitsandbytes works
(else a bf16/fp16 base); gradient checkpointing; batch 1 × grad-accum 4;
AdamW (paged 8-bit for QLoRA) with a cosine schedule, LR 2e-4, 5 % warm-up;
1–3 epochs (`epochs`, default 2); max sequence 512 tokens; eval on the held-out
split twice per epoch with early stopping (patience 2) and the best checkpoint
restored; checkpoints kept (last 2), so `{"resume": job_id}` continues an
interrupted run. A new run continues from the last **accepted** adapter of
the same base model, so learning accumulates.

Logs: `~/.imurun/tuning/runs/<run_id>/` → `config.json`, `metrics.jsonl`
(every step: loss, learning rate, grad norm; every eval: `eval_loss`),
`result.json`, `train.log`, `export.log`, `job.json`, `control.json`.

**Acceptance.** Before training: held-out loss and the sanity set on the
untuned base (adapter disabled). After: the same. Rejected when the required
prompt ("The capital of France is" → Paris) fails, when any sanity prompt the
base answered now fails, or when held-out loss is more than 2 % worse than the
base's.

**Pausing.** The trainer polls `control.json` after every optimizer step. The
scheduler sets `pause` while any chat turn is running and clears it when the
turn ends; a pause frees the CUDA cache but keeps the weights resident (the
model is not unloaded). `max_hours` sets a deadline after which training stops
early but still evaluates and saves. Export runs at below-normal priority and
does not pause.

## Export, and exactly what gets downloaded

1. **Training stack** (`python -m services.tuning.setup`, once): a venv at
   `~/.imurun/tuning/venv` with torch (CUDA 12.8 build from
   `download.pytorch.org/whl/cu128` when an NVIDIA GPU is present, else the
   CPU build), transformers, peft, accelerate, safetensors, numpy,
   huggingface_hub, sentencepiece, protobuf (`requirements-tuning.txt`), and
   optionally bitsandbytes. Measured here: **2.8 GB torch wheel, 4.89 GB
   installed, 539 s**. The app venv is never touched.
2. **llama.cpp tools** (with setup, or at first export), from the newest
   llama.cpp build release that ships a CPU binary for this OS (pinned after
   the first fetch in `tools/llama.cpp/VERSION.json`; override with
   `IMURUN_LLAMACPP_TAG`): the source archive
   `github.com/ggml-org/llama.cpp/archive/refs/tags/<tag>.zip` (41 MB; only
   the top-level `*.py` converters, the `conversion/` package and `gguf-py/`
   are extracted — no C++ is built) and `llama-<tag>-bin-win-cpu-x64.zip`
   (25 MB; only `llama-quantize.exe` and its DLLs are extracted). Measured
   tag: `b12032`. The pip `gguf` package is not used because it does not
   ship `convert_hf_to_gguf.py` and must match the converter's own `gguf-py`.
3. **Original weights** per model (on the first run for that model, size
   shown first, `confirm` required): `*.safetensors`, `config.json`,
   tokenizer files, from the official repo (`catalogue.py`), or — when the
   official repo is licence-gated and no token is set — the listed mirror that
   re-hosts the same files (e.g. `unsloth/Llama-3.2-1B-Instruct`; the
   original licence still applies). Measured: Llama 3.2 1B = 2.49 GB in 216 s
   from the mirror. Meta's duplicate `original/` folder and Mistral's
   `consolidated.safetensors` are skipped.

Then per accepted run:

- `export/adapter-<run>-f16.gguf` — the LoRA adapter as GGUF (for engine
  request E1, `REQUESTS.md`; the engine cannot apply it yet).
- the adapter is merged into the base **one safetensors shard at a time**
  (`W += alpha/r · B·A` in fp32, cast back), so an 8B (5 GB shards) merges in
  ~10 GB of RAM; `convert_hf_to_gguf.py` writes an f16 GGUF (or Q8_0 / F16
  directly), `llama-quantize` makes the chosen quant;
- result: `models/<base-file-stem>-tuned-<run>-<quant>.gguf`, e.g.
  `Llama-3.2-1B-Instruct-tuned-r202611012300abc-Q4_K_M.gguf`, registered as a
  local model. The merged safetensors and the f16 intermediate are deleted.

## Settings (`tuning` section; `GET/POST /api/tuning/settings`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | master switch for automatic runs (manual "Train now" works regardless) |
| `auto` | `"off"` | `off` \| `nightly` \| `when-idle` \| `every-N-approved-samples` |
| `min_samples` | `200` | usable samples needed for a run (this is the *N* of `every-N-approved-samples`) |
| `max_hours` | `4.0` | plan is `slow` beyond this; training stops early at this deadline |
| `allow_8b_slow` | `false` | let automatic runs take `slow` plans (8B-class on this machine) |
| `quant` | `"Q4_K_M"` | `Q4_K_M` \| `Q5_K_M` \| `Q6_K` \| `Q8_0` \| `F16` |
| `model` | `"selected"` | which model to tune: the engine's selected model, or a catalogue id |
| `epochs` | `2` | 1–3 |
| `idle_minutes` | `15` | `when-idle`: minutes with no chat turn |
| `nightly_hour` | `2` | `nightly`: local hour |
| `include_pii` | `false` | keep samples the PII filter would drop |
| `auto_download` | `false` | allow automatic runs to download base weights (otherwise start the first run by hand) |
| `max_seq_len` | `512` | tokens per sample (longer costs VRAM) |

`hf_token` (stored in the `huggingface` section, write-only) is accepted by the
same `POST`. Every automatic mode also requires: no running turn, at least two
idle minutes, no other job, and a feasibility verdict of `ok` (or `slow` with
`allow_8b_slow`).

## API (all behind the bearer token)

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/tuning/status` | `{enabled, auto, stack{installed, message, caps, export_tools}, hardware, selected_model, target_model, trainable, plan, samples{usable, min_samples, dropped, sources}, feedback{up, down, teach, consented}, active_job, last_job, tuned_models, automation, setup, paths}` |
| GET | `/api/tuning/settings` | `{tuning: {...}, hf_token: "" \| "••••••••"}` |
| POST | `/api/tuning/settings` | partial `tuning` keys (flat or under `tuning`) and/or `hf_token`; 400 on invalid values |
| GET | `/api/tuning/plan/{model_id}` | `{plan}` (verdict, method, need/have GB, estimates, options, warnings) |
| POST | `/api/tuning/jobs` | `{model_id?, confirm?, allow_slow?, resume?, epochs?, max_steps?, min_samples?, quant?, max_hours?}` → 202 `{job}`; 409 `{needs: ["confirm"] \| ["allow_slow"], plan, message}`; 400 `not_enough_samples` / `not_feasible` / cannot be tuned; 503 `training_stack_missing` |
| GET | `/api/tuning/jobs` | `{jobs: [...]}` newest first |
| GET | `/api/tuning/jobs/{id}` | `{job, metrics: [...metrics.jsonl rows]}` |
| POST | `/api/tuning/jobs/{id}/cancel` | 200 `{job}`; 409 if already finished |
| POST | `/api/tuning/feedback` | `{turn_id, rating: up \| down \| teach \| clear, correction?, session_id?}` → `{feedback{consented}, counts}`; 404 if no transcript holds the turn |
| GET | `/api/tuning/datasets` | `{folder, files, usable, dropped, sources, preview, manifest, built}` |
| POST | `/api/tuning/setup` | `{torch_index?, skip_bnb?}` → 202; installs in the background |

Events on `/api/events`:

| Event | Payload |
|---|---|
| `tuning.job` | the job view: `{id, model_id, parent, trigger, state, progress (0-100), message, eta_s, loss, eval_loss, base_eval_loss, step, max_steps, paused, plan, build, result, export, new_model_id, error}` — on every state change and at most once a second during progress |
| `tuning.ready` | `{job_id, model_id, parent, name, path, quant, eval{base_eval_loss, eval_loss, sanity_passed, sanity_total, samples, steps, train_seconds, reason}, action: {label: "Use it", method: "POST", path: "/api/models/select", body: {id}}}` |
| `tuning.setup` | `{running, stage, percent, message, ok?, error?, venv_bytes?}` |
| `tuning.feedback` | `{turn_id, session_id, rating, consented}` |

CLI (works without the server): `python -m services.tuning status`,
`plan --model ID`, `build --model ID` (dataset dry run), `run --model ID
[--confirm] [--allow-slow] [--max-steps N] [--min-samples N]`, and
`python -m services.tuning.setup [--probe]`.

## Using the result

When a run finishes you get `tuning.ready`. The tuned model is now in the
Models list (`GET /api/models`, `source: "local"`, tag `tuned`). Click **Use
it** (or `POST /api/models/select {"id": "<model_id>"}`) to switch; switching
back to the original is the same call with the original id. The channel never
switches by itself. Delete a tuned model by deleting its GGUF in `models/`.

## Files on disk

`~/.imurun/tuning/`: `venv/` (~4.9 GB), `base/<model>/` (original weights),
`datasets/` (your files), `built/` (assembled datasets), `runs/<run>/`
(job, config, metrics, checkpoints, adapter, export), `tools/llama.cpp/`,
`manifest.json`, `registry.json`, `hardware.json`, `setup.json`,
`setup.log`. Remove the folder to remove everything the channel created except
the tuned GGUFs in `models/`.

## Not real yet (do not claim)

- The engine cannot load a LoRA adapter (request **E1**); each accepted run
  produces a full re-quantised GGUF.
- No UI yet: thumbs/teach buttons, the Tuning panel and the "Use it" card are
  frontend requests (`REQUESTS.md`). Today the channel is driven through the
  API or the CLI.
- Training pauses but does not release GPU memory held by the model weights;
  if the chat engine needs that VRAM during a pause it runs with what is left.
- 8B-class models cannot be tuned on this 4 GB laptop in practice (measured CPU 4-bit speed ~0.2 tok/s for a 1B; a normal dataset is refused).
- Multi-turn samples and the app's system prompt are not part of training
  samples yet.
