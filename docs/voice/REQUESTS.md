# Voice: open requests and questions

## To other owners (filed)

- Frontend: `docs/frontend/API-REQUESTS.md` #33 (use `/api/voice/*`).
- Agentic/server: `docs/agentic/REQUESTS.md` #13 (`language` on
  `POST /api/turns` -> one "Answer in <language>" line in the loop).
- Setup: `docs/setup/CODE-REQUESTS.md` #8 (`install.py --voice`, a `voice`
  extra).

## From the frontend (2026-11-02)

The shell now uses `/api/voice/*` (`frontend/design-lab/voice-server.js`,
`docs/frontend/CHANGELOG.md`). Questions 2 and 4 below are answered in the UI:
a first-run "Which languages do you speak?" picker sets `languages` and picks
`sttModel` (`base` for English/German only, otherwise `small`), and the
threads field explains the 2 vs 4 trade-off. One request:

- ~~**F1. A speech model per language.**~~ Done 2026-11-02 (`voice.sttModelByLanguage`; the transcription reports `model`, and `model_pending` when the language's model is still downloading). The owner asked for `large-v3-turbo`
  as a Bengali-only option. The UI saves it as
  `voice.sttModelByLanguage = {"bn": "large-v3-turbo"}` and says it applies
  once the server supports it; today `sttModel` is one model for every
  language. Wanted: when the detected (or forced) language has an entry
  there, transcribe with that model (download on first use like `sttModel`),
  and report the model used in the transcription (`model`).

## Open questions for the product owner

1. **Online fallback.** edge-tts gives natural neural voices for Hindi,
   Bengali and German on machines without Piper, but it sends the reply
   text to Microsoft. It is implemented, off by default, labelled in
   settings and status. Should the shell offer the switch at all?
2. **Default model.** `small` (486 MB, ~5 s per utterance on 2 threads) is
   the only size that handles Hindi and Bengali. English/German-only users
   would be better served by `base` (1.6 s). Ask the user's languages at
   first run and pick?
3. **Bengali quality.** whisper `small` + re-scripting reaches CER ~0.3 on
   synthetic speech and fails on ~1 in 6 clips. Options: a Bengali
   fine-tuned CTranslate2 Whisper (community conversions exist, not
   evaluated), or `large-v3-turbo` with 4+ threads for Bengali only.
4. **Threads.** 4 threads cut STT from ~5 s to ~3.5 s. Default stays 2 so a
   transcription does not slow the FISH engine's decode; raise it?

## Not built (yet)

- Streaming dictation (`POST /api/voice/stream`, partials over SSE).
- MMS-TTS (torch, separate venv) for languages Piper lacks (ta, gu, pa, kn).
- Server-side voice activity detection for hands-free mode (Whisper's VAD
  filter is on, but the shell decides when recording stops).
