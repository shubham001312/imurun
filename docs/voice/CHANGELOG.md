# Voice changelog

## 2026-11-03 - audit of the dictation pass (voice owner)

- Audit: `cleanup`, `polish`, `stream`, `vocabulary`, `gpu` are imported and
  used by `service.py`/`engines.py`/`routes.py`; `eval.py` is a standalone
  CLI (`python -m services.voice.eval`), not imported by the server by design.
  `gpu.py` had no TODO/stub (the only `pass` is an `except` in NVML probing).
- GPU selection now honours the resource governor: `routes._govern` gives the
  service `governor.budget().vram_headroom`; it is sent to the worker as
  `vram_headroom_mb` and `gpu.fits` uses max(300 MB, governor headroom).
  No change to `services/resources` was needed.
- Removed references to the never-written `docs/voice/BENCHMARKS.md`.
- Tests: unit tests for the governor headroom and `eval.score`.

## 2026-11-03 - dictation accuracy ("Wispr Flow" pass)

No benchmark results file exists (an earlier draft referred to
`docs/voice/BENCHMARKS.md`; it was never written, the references were removed).
Real microphone input and WER on real sets are not measured.

- **Evaluation harness** `python -m services.voice.eval run|report|synth`
  (`services/voice/eval.py`): WER/CER per set, latency p50/p95, empty/garbage
  counts, VRAM, deltas between runs; built-in synthesised sets (product terms,
  disfluent dictation, Hinglish, room noise); `--legacy` reproduces the
  pre-2026-11-03 decoding for the baseline row.
- **GPU**: the worker runs Whisper on CUDA (`int8_float16`) when CTranslate2
  sees a device, cuBLAS 12 loads (`nvidia-cublas-cu12`, optional in
  `requirements-voice.txt`) and NVML reports room for the model + 300 MB
  (`services/voice/gpu.py`); else CPU int8 as before. A CUDA failure at load
  or decode retries on the CPU. Settings `device` (auto/cuda/cpu),
  `sttModelGpu` (default `large-v3-turbo`; `sttModel` is the CPU model),
  `gpuIdleSeconds` (default 120: the GPU model is dropped after idle and its
  VRAM goes back to the FISH engine; the worker and CPU voices stay).
- **Decoding**: explicit Silero VAD parameters (300 ms padding), temperature
  ladder per device (bounded on CPU), Whisper's own thresholds set
  explicitly, digital-silence short cut, subtitle-hallucination filter;
  replies carry `device`, `compute_type`, `device_reason`, `speech_seconds`,
  `dropped`.
- **Context biasing** (`services/voice/vocabulary.py`): a glossary
  `initial_prompt` from `voice.corrections`, `voice.vocabulary`, names in the
  conversation (`context` on `POST /api/voice/transcribe`, multipart field or
  query) and built-in product names; skipped for a forced non-Latin language.
  Routes `GET/POST /api/voice/vocabulary`, `POST /api/voice/correction`.
- **Dictation clean-up** (`services/voice/cleanup.py`): fillers (en/de/hi/bn),
  stutters, self-corrections, spoken punctuation (en/de/hi/bn), English
  numbers, user corrections; `text` is the cleaned text, `text_raw` Whisper's.
  Optional model pass (`services/voice/polish.py`, `voice.cleanupModel`, off):
  the loaded FISH model, only when idle, greedy, cancelled at the budget from
  inside its own token callback.
- **Streaming dictation** (`services/voice/stream.py`): `POST
  /api/voice/stream?id&seq[&final=1]`, partials over SSE `voice.partial`,
  `voice.final`; whole-stream re-decode as the overlap, commit after 20 s,
  VAD endpointing, language locked per session; partials stop on CPU.
- Float32 / extensible WAV input (BUGS-FIXED #7), truncated WebM (#10).
- Tests: `tests/test_voice_dictation.py` (engine-free), three opt-in real
  tests in `tests/test_voice_engines_real.py` (noise -> empty, GPU latency,
  eval harness).
- Frontend asks: `docs/frontend/API-REQUESTS.md` #34.

## 2026-11-02 — speech model per language (frontend F1)

- `voice.sttModelByLanguage` (`{"bn": "large-v3-turbo"}`): a forced language uses its model
  directly; under auto-detection the default model runs first and, when the detected language
  has an entry, a second pass with that model and the language fixed replaces the result.
  The reply carries `model` (the model that produced the text) and `model_pending` when the
  language's model is not installed yet (auto-install starts the download; the first-pass text
  is returned meanwhile). Entries equal to `sttModel` or with unknown names are dropped.

## 2026-11-02 - server-side voice layer (first version)

- New package `services/voice/` (languages, audio, download, catalogue,
  system, online, engines, worker, service, routes, text, CLI).
- STT: faster-whisper (CTranslate2 int8, CPU), default model `small`,
  downloaded on first use to `~/.imurun/voice/models/whisper/`, language
  auto-detection restricted to `voice.languages`, Bengali re-scripting.
- TTS: Piper voices on demand (default en_US-lessac, de_DE-thorsten,
  hi_IN-priyamvada, bn_BD-google); system voices (SAPI / say / espeak-ng);
  edge-tts online fallback, off by default and labelled.
- Engines run in a child worker process at below-normal priority, 2 threads,
  unloaded after 10 idle minutes.
- Routes `/api/voice/status|voices|jobs|transcribe|speak|voices/install|unload`;
  `/api/status.voice` summary line; `voice` settings section;
  `voice.model.download` SSE events.
- `services/server/api.py`: one import, two 3-line dispatch blocks, one
  status field, and `media-src 'self' blob:` in the CSP (the shell plays
  fetched audio from `blob:` URLs - frontend request #13).
- `services/server/settings.py`: `voice` section in `DEFAULTS` (imports
  `services.voice.service.DEFAULTS`; also closes frontend request #4).
- `requirements-voice.txt`; `python -m services.voice doctor|install|speak|transcribe|roundtrip`.
- Tests: `tests/test_voice_core.py` (20), `tests/test_voice_api.py` (17),
  `tests/test_voice_engines_real.py` (4, opt-in).
- Measured round trips and latencies: see README.md.
