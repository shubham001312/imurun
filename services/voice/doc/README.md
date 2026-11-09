# services/voice

Server-side STT (faster-whisper) and TTS (Piper, system voices, opt-in
online). The full documentation - architecture, measured results, the
capability matrix, settings and install per OS - is `docs/voice/README.md`;
the HTTP contract is in `services/server/doc/README.md` ("Voice").

| Module | Role | Imports engines? |
|---|---|---|
| `routes.py` | `/api/voice/*` handlers, called from `api.py` | no |
| `service.py` | decisions, settings, downloads, engine order | no |
| `worker.py` | child process + client (framed stdin/stdout) | worker side only |
| `engines.py` | WhisperSTT, PiperTTS (+ fakes) | yes, lazily, in the worker |
| `system.py` | SAPI / say / espeak-ng | no (child processes) |
| `online.py` | edge-tts, off by default | only when enabled |
| `catalogue.py`, `download.py` | Piper voices list, HF downloads with progress | no |
| `languages.py`, `text.py`, `audio.py`, `paths.py` | helpers | no (numpy lazily in `decode_16k`) |

CLI: `python -m services.voice doctor | install | speak | transcribe | roundtrip`.
