# Voice: server-side speech-to-text and text-to-speech

Owner scope: `services/voice/**`, `/api/voice/*`, the `voice` settings
section, `docs/voice/`. The shell (`frontend/design-lab/voice.js`) is the
frontend owner's; what it needs from this layer is in
`docs/frontend/API-REQUESTS.md` #33.

Why it exists: the browser Web Speech API is missing or refuses service in
pywebview (WebView2 / WebKit), and in Chrome/Edge it streams audio to the
vendor's cloud. This layer runs both directions on the machine.

## What works (measured on this machine, 2026-11-01/02)

Windows 11, 16 logical CPUs, Python 3.12 venv, CPU only. Round trip = Piper
speaks a fixed sentence, Whisper transcribes the WAV, compared to the source
text (`python -m services.voice roundtrip`). These are **synthetic-voice**
numbers; **real microphone speech has not been measured** (no human
recordings were available to the agent).

| Language | STT (whisper `small`, int8, 2 threads, beam 5) | TTS (Piper default voice) | Round trip |
|---|---|---|---|
| English `en` | detected `en` (p 0.99) | `en_US-lessac-medium` | WER 0.08 / CER 0.11 - the only difference is "twelve" → "12" |
| German `de` | detected `de` (p 0.999) | `de_DE-thorsten-medium` | WER 0.09 / CER 0.09 - "zwölf" → "12" |
| Hindi `hi` | detected `hi` (p 0.99) | `hi_IN-priyamvada-medium` | CER 0.08-0.12 over 6 runs (spelling: ताप्मान, दिग्री) |
| Bengali `bn` | detected `bn` (constrained, see below) | `bn_BD-google-medium` (16 speakers, speaker 0) | CER 0.31-0.42 in 5 of 6 runs, **1.0 (garbage) in 1 of 6**. Works, not reliably |

Latency (warm worker):

| Step | Time |
|---|---|
| TTS, one sentence (3-5 s of audio) | 0.15-0.28 s (Piper, all four languages) |
| TTS, first use of a voice | +2-5 s (onnxruntime session); 4 voices stay loaded |
| STT, one 3-5 s utterance, `small`, 2 threads | 4.4-6.2 s |
| same, 4 threads (`voice.threads: 4`) | 3.2-4.1 s |
| worker start + whisper `small` load | ~1-2 s, once per idle period |

Whisper always encodes a 30 s window, so STT time is roughly constant for
short utterances: the 4-6 s is the price of one spoken turn on 2 threads.

Other model sizes, same clips:

| Model | Download | en / de | hi / bn | Time per utterance (2 threads) |
|---|---|---|---|---|
| `base` | 148 MB | CER 0.11 / 0.00 | **unusable**: writes Urdu script for Hindi, garbage for Bengali | 1.5-1.8 s |
| `small` (default) | 486 MB | CER 0.11 / 0.09 | 0.08 / ~0.35 | 4.4-6.2 s |
| `large-v3-turbo` | 1.62 GB | CER 0.11 / 0.09 | 0.19 / 0.27 | 23-42 s |

So: `small` is the default; `base` is a fair choice for English/German-only
users who want speed; turbo is not worth it on CPU.

Also measured:

- WebM/Opus (what `MediaRecorder` produces) decodes via PyAV without an
  ffmpeg executable: all four languages went through `POST
  /api/voice/transcribe` with `audio/webm;codecs=opus` over real HTTP.
- Windows SAPI (system voice): this machine has only `Microsoft David
  Desktop` and `Microsoft Zira Desktop` (en-US). English round trip CER
  0.11, 2.0 s per sentence (a PowerShell process per call). Hindi, Bengali,
  German: **no system voice here, not tested**. (The OneCore voices under
  `Speech_OneCore` are not visible to `System.Speech`.)
- The worker runs at `BELOW_NORMAL` priority on Windows (checked:
  `/api/voice/status.worker.priority == "below_normal"`).

Not tested: macOS `say`, Linux `espeak-ng` (code paths written, no machine),
the online `edge-tts` engine against the real service (only with a fake),
real microphone audio, noisy rooms, code-switched speech (Hinglish).

## Dictation pipeline (what `POST /api/voice/transcribe` and `/stream` run)

audio -> `audio.decode_16k` -> Whisper (`engines.WhisperSTT`, VAD, glossary
prompt from `vocabulary.build_prompt`) -> `cleanup.clean` (fillers, stutters,
self-corrections, spoken punctuation, English numbers, user corrections) ->
optional `polish` (the loaded FISH model, off by default, only when idle, cut
at `cleanupModelMs`). The reply has `text` (final), `text_raw` (Whisper's) and
`cleanup` (`applied`, `changes`, `model`). Query: `cleanup=0|1`, `polish=0|1`,
`context=...`.

- `POST /api/voice/stream?id=S&seq=N[&final=1&lang=..]` takes MediaRecorder
  chunks; partials arrive as SSE `voice.partial`, the end as `voice.final`
  (also in the final POST reply). `POST /api/voice/stream/cancel?id=S`.
- `GET/POST /api/voice/vocabulary`, `POST /api/voice/correction`.
- No STT engine installed: 503 `stt_unavailable` with the install hint in
  `reason` (tested), never a traceback.
- GPU: CUDA only when CTranslate2 sees a device, cuBLAS 12 loads and free VRAM
  >= model need + max(300 MB, the resource governor's VRAM headroom).
- `python -m services.voice.eval` is a developer harness (WER/CER on a
  dataset directory you supply); the server does not use it.

Not measured: WER of cleanup/prompting on real speech sets, streaming latency
on real microphone audio. Settings `cleanup`, `cleanupModel`, `cleanupModelMs`,
`vocabulary`, `corrections`, `contextBiasing`, `sttModelGpu`, `device`,
`gpuIdleSeconds`, `streamEndpointSeconds` are defined in `service.DEFAULTS`.

## Capability matrix

| Language | STT | TTS offline (Piper) | TTS system voice | TTS online (opt-in) | Tested |
|---|---|---|---|---|---|
| en | faster-whisper, offline, model on demand | 30+ en_US/en_GB voices, on demand (63 MB each) | Windows: David/Zira (installed by default) · macOS `say` · Linux espeak-ng | edge-tts `en-US-AriaNeural` | STT+Piper+SAPI round trip: yes |
| hi | same | 3 voices (priyamvada, pratham, rohan), on demand | Windows only with the Hindi language pack's speech voice · espeak-ng (robotic) | `hi-IN-SwaraNeural` | STT+Piper: yes; system/online: no |
| bn | same (weak, see above) | 1 voice, 16 speakers (bn_BD-google-medium), on demand | espeak-ng (robotic); Windows has no Bengali SAPI voice in standard packs | `bn-IN-TanishaaNeural` | STT+Piper: yes (unreliable); system/online: no |
| de | same | 10 voices (thorsten, kerstin, eva_k, ...), on demand | Windows German language pack · macOS · espeak-ng | `de-DE-KatjaNeural` | STT+Piper: yes; system/online: no |
| 50+ others | Whisper's 99 languages | Piper catalogue: ar bg ca cs cy da el es et eu fa fi fr he hu hy id is it ja ka kk ko ku lb lt lv ml mr ne nl no pl pt ro ru sk sl sq sr sv sw te th tr uk ur vi zh | whatever the OS has | 24 configured | **not tested** |

The full, live matrix is `GET /api/voice/voices` (per language: engine,
voice id, installed, quality, offline, download size).

MMS-TTS (`facebook/mms-tts-*`, torch) was **not built**: the Piper catalogue
has Hindi and Bengali voices (checked 2026-11-01), so the torch stack (and
its separate venv) is not needed for the four core languages. It remains the
option for a language Piper lacks (e.g. Tamil, Gujarati, Punjabi); see
REQUESTS.md.

## Architecture

```
shell ──HTTP──> api.py ──(path startswith /api/voice/)──> services/voice/routes.py
                                                              │
                                                     VoiceService (service.py)
              ┌──────────────────────┬───────────────────────┼──────────────────────┐
     downloads (download.py)   system voices (system.py)   online (online.py)   WorkerClient (worker.py)
     urllib -> ~/.imurun/voice  SAPI / say / espeak-ng      edge-tts, OFF         │ stdin/stdout frames
     voice.model.download SSE   child process, low prio     by default            ▼
                                                                         voice worker process
                                                                         (BELOW_NORMAL / nice 10)
                                                                         engines.py: WhisperSTT, PiperTTS
```

- **The server process never imports an engine.** `has_module` uses
  `importlib.util.find_spec`; faster-whisper, CTranslate2, onnxruntime and
  Piper load only in the worker (`python -m services.voice.worker`, same
  interpreter). Reasons: process priority covers the engines' own thread
  pools; killing the process really frees the RAM; a native crash cannot
  take down the server.
- **Resources.** `voice.threads` (default 2) bounds CTranslate2
  `cpu_threads`, onnxruntime `intra_op_num_threads` and `OMP_NUM_THREADS`.
  One request at a time (a lock). The worker exits after
  `voice.idleUnloadMinutes` (default 10) without requests and is respawned
  on the next one (`POST /api/voice/unload` does it now). It never downloads
  (`HF_HUB_OFFLINE=1`, `local_files_only`); its working directory is
  `~/.imurun/voice/tmp`.
- **Downloads** run in the server (threads), stream to `<file>.part` and are
  renamed when complete (resumable with HTTP Range). Progress is published
  as `voice.model.download` every 0.5 s. Sources: `Systran/faster-whisper-*`
  and `rhasspy/piper-voices` on Hugging Face.
- **Language handling.**
  1. STT: with `autoDetectLanguage` on, Whisper detects; if its pick is not
     in `voice.languages` (default en, hi, bn, de) the best of those from
     the same detection pass is used and the audio decoded again. This
     fixes Hindi-heard-as-Urdu-script and Bengali-heard-as-Gujarati.
     Turn it off (or set `lang`) to force one language.
  2. Bengali re-scripting: whisper `small` writes Bengali speech in
     Devanagari letters even with `language="bn"` (8 of 8 samples). The
     Devanagari and Bengali Unicode blocks are parallel (+0x80), so the
     worker shifts them (`engines.devanagari_to_bengali`); CER went from
     1.0 to ~0.3. The reply says `script_fixed: true` when it did this.
  3. The detected code comes back as `language` and `answer_language`;
     the shell sends it with the turn so the model answers in it (needs the
     one-line loop change in `docs/agentic/REQUESTS.md` #13;
     `languages.answer_directive()` is the line).
  4. TTS picks the voice from `lang`, but the text's script wins when they
     disagree (`lang=en` with Devanagari text speaks Hindi).
- **Sentence chunking.** The shell calls `POST /api/voice/speak` once per
  sentence and prefetches the next while the current one plays (TTS is
  ~20x faster than real time, so speech starts ~0.2 s after the first
  sentence of the reply is complete). The server does not stream one
  response per reply; `services/voice/text.py` (`speakable`, `sentences`)
  is the reference splitter (`. ! ? । ॥ 。`, max 220 chars), matching the
  browser one.
- **Formats.** In: WAV natively (stdlib + numpy), WebM/Opus, OGG, MP3,
  FLAC, M4A through PyAV (bundled FFmpeg libraries), or an `ffmpeg` on PATH
  if PyAV is missing. Out: WAV (16-bit mono, Piper's rate 16/22.05 kHz),
  MP3 only from the online engine.
- **Engine order for TTS** (`voice.ttsEngine: auto`): the user's chosen
  voice for that language, then Piper (installed voice, else it starts the
  download and falls through), then a system voice, then online only if
  `voice.onlineFallback` is true. The reply's `X-Voice-Fallback` header
  says why an earlier tier was skipped.

## Settings (`GET/PUT /api/settings/voice`)

| Key | Default | Meaning |
|---|---|---|
| `lang`, `voiceURI`, `rate`, `sounds` | `""`, `""`, 1, true | the shell's existing browser-voice settings (unchanged meaning; `lang` is also the STT hint when auto-detect is off) |
| `engine` | `auto` | for the shell: `auto` (server when `/api/voice/status` says available), `server`, `browser` |
| `sttEngine` | `faster-whisper` | or `off` |
| `sttModel` | `small` | `tiny` `base` `small` `medium` `large-v3` `large-v3-turbo` |
| `sttBeamSize` | 5 | 1 is greedy; it looped for 69 s on one Bengali clip and was not faster for short clips |
| `ttsEngine` | `auto` | `auto` `piper` `system` `online` |
| `voices` | `{}` | per language: `{"hi": "piper:hi_IN-rohan-medium", "bn": "piper:bn_BD-google-medium#3", "en": "system:Microsoft Zira Desktop"}` (`#n` = speaker) |
| `autoDetectLanguage` | true | detect the spoken language |
| `languages` | `["en","hi","bn","de"]` | the candidates detection is restricted to |
| `replyInSameLanguage` | true | sets `answer_language` in the transcription |
| `speakReplies` | false | for the shell: read every reply aloud |
| `inputDevice` | `""` | for the shell: `MediaDeviceInfo.deviceId` (the server never opens a microphone) |
| `onlineFallback` | **false** | edge-tts: **sends the reply text to Microsoft**. Never the default |
| `autoInstall` | true | first use of a missing model/voice starts its download |
| `threads` | 2 | engine threads (1-16) |
| `idleUnloadMinutes` | 10 | 0 = never unload |

## Install, per OS

Common (in the app's venv; the voice packages are optional):

```
python -m pip install -r requirements-voice.txt
python -m services.voice doctor
python -m services.voice install whisper small            # 486 MB, or let first use do it
python -m services.voice install piper hi_IN-priyamvada-medium
```

- **Windows**: nothing else. SAPI voices for other languages: Settings >
  Time & language > Language > add the language > include "Speech". (Only
  `System.Speech` "Desktop" voices are used; verify with `doctor`.)
- **macOS**: nothing else; `say` voices come from System Settings >
  Accessibility > Spoken Content > System voice > Manage voices. Not tested.
- **Linux**: `sudo apt install espeak-ng` (or dnf/pacman) for the system
  fallback; Piper bundles its own espeak-ng data and does not need it. Not
  tested.
- **Online fallback** (optional): `pip install edge-tts`, then
  `PUT /api/settings/voice {"onlineFallback": true}`.

Data: `~/.imurun/voice/` (`models/whisper/<size>/`, `voices/piper/<key>/`,
`cache/piper-voices.json`, `tmp/`). Override with `IMURUN_VOICE_HOME`.
Nothing is written to the working directory.

## Tests

- `tests/test_voice_core.py` - languages, Markdown→speech text, sentence
  splitting, WAV/multipart, catalogue, Devanagari→Bengali (no engines).
- `tests/test_voice_api.py` - every route on a real `ApiServer` with a fake
  worker; 503 reasons without engines; download job events; settings; the
  real worker subprocess with `IMURUN_VOICE_FAKE=1`.
- `tests/test_voice_engines_real.py` - opt-in `IMURUN_VOICE_TESTS=1`: Piper
  → Whisper round trip for en/de/hi (CER bounds) and bn (language only).
