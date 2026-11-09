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
7. **Every FLEURS clip (and any IEEE-float WAV) failed with 415 "not a
   readable WAV file (unknown format: 3)".** Cause: `wav_info` / `decode_16k`
   used the stdlib `wave` module, which only reads integer PCM; float32 WAV
   is what FLEURS ships and what Web Audio recorders that skip the int16 step
   write, and WAVE_FORMAT_EXTENSIBLE headers failed too. Fix: our own RIFF
   chunk parser (PCM 8/16/24/32-bit, float 32/64, extensible, streaming
   headers with size 0) - `services/voice/audio.py:81`.
8. **The GPU path failed with "Library cublas64_12.dll is not found or cannot
   be loaded"** although `nvidia-cublas-cu12` was installed and
   `os.add_dll_directory` pointed at it. Cause: CTranslate2 loads cuBLAS
   lazily with the plain Windows search order, which ignores
   `AddDllDirectory`. Fix: prepend `site-packages/nvidia/*/bin` to `PATH`
   before the first CUDA call - `services/voice/gpu.py:75`.
9. **3 s of room noise came back as "You" / "I'll see you next time."**
   (whisper `small` and `large-v3-turbo`, VAD off; with VAD on, some loud
   noise clips still passed VAD). Cause: Whisper's training-data
   hallucinations on non-speech. Fix: Silero VAD with explicit parameters,
   a digital-silence short cut, and a filter that drops known subtitle
   phrases when the model was unsure or VAD kept < 1 s of speech -
   `services/voice/engines.py:244`, `:279`.
10. **A streamed WebM cut mid-cluster failed to decode** (every partial
    before the last chunk). Cause: PyAV raises on the truncated last
    cluster. Fix: keep the frames decoded before the error -
    `services/voice/audio.py` `_decode_pyav`.
11. **Whisper could take the GPU below the resource governor's VRAM headroom.**
   Cause: `gpu.fits` used a fixed 300 MB headroom and the voice worker never
   saw the governor's budget. Fix: `routes._govern` passes
   `governor.budget().vram_headroom` (`services/voice/routes.py:50`) ->
   `service._vram_headroom_mb` -> worker header `vram_headroom_mb` ->
   `WhisperSTT._choose` / `gpu.fits` use the larger value
   (`services/voice/engines.py:137`, `services/voice/gpu.py:131`).
12. **Docs and comments cited `docs/voice/BENCHMARKS.md`, which never existed.**
   Cause: previous agent was killed before writing it. Fix: references removed
   or reworded as "not measured" (`cleanup.py:22`, `engines.py:36`, `gpu.py:28`,
   `vocabulary.py:13`, CHANGELOG).
