# Voice: bugs fixed

Format: symptom -> cause -> fix (file:line).

1. **Every WebM/Opus upload failed with 415 "could not decode".**
   Cause: `faster_whisper.audio.decode_audio` (faster-whisper 1.2.1) calls
   `av.open(..., metadata_errors="ignore")`; PyAV 19 removed that argument
   (TypeError). Fix: decode with our own PyAV loop (and flush the resampler
   tail) instead of faster-whisper's helper - `services/voice/audio.py:164`.
2. **Worker priority stayed "unchanged" on Windows.** Cause: ctypes'
   default `int` types truncated the pointer-sized pseudo-handle from
   `GetCurrentProcess()`, so `SetPriorityClass` failed with
   ERROR_INVALID_HANDLE (6). Fix: `restype`/`argtypes` as `c_void_p` -
   `services/voice/worker.py:87`. (The child was already started with
   `BELOW_NORMAL_PRIORITY_CLASS`, so the effect was only on reporting; POSIX
   uses `os.nice(10)`.)
3. **Speaking en/hi/bn/de in turn took 2-3 s per sentence even when warm.**
   Cause: the Piper voice cache held 3 voices, so a 4-language cycle evicted
   the next voice every time and paid the 2-5 s onnxruntime load. Fix:
   `MAX_LOADED = 4` - `services/voice/engines.py:155`. Warm sentences now
   take 0.15-0.28 s.
4. **Bengali transcripts came back in Devanagari script** (CER 1.0) with
   whisper `small`, even with `language="bn"`. Cause: the model's Bengali
   output is weak and writes the sounds in Hindi letters. Fix: shift
   Devanagari to the parallel Bengali block when the language is `bn`
   (`devanagari_to_bengali`) - `services/voice/engines.py:108`; CER ~0.3.
5. **A Bengali clip took 69 s to transcribe** (greedy decoding looped and
   the default 6-step temperature fallback re-decoded it). Fix: default beam
   5, fallback limited to `(0.0, 0.4)`, `max_new_tokens=224` -
   `services/voice/engines.py:62`, `services/voice/service.py` DEFAULTS.
6. **Unconstrained detection picked the wrong language** (Bengali speech ->
   `gu` with large-v3-turbo; Hindi -> Urdu script is a known Whisper habit).
   Fix: when the detected language is not in `voice.languages`, take the best
   of those from the same pass's probabilities and decode again -
   `services/voice/engines.py:88`. A first version ran a separate
   `detect_language` pass on every request (+3 s); now the second decode
   happens only when needed.
