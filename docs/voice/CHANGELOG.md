# Voice changelog

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
