# Voice in the IMURUN shell

## Today: browser speech engines (first step)

`frontend/design-lab/voice.js` adds two features using only APIs built into
the browser or WebView.

### Dictation (speech to text)

- The mic button in the composer (`#composer-mic`) starts
  `SpeechRecognition` (or `webkitSpeechRecognition`) with
  `interimResults: true` and `continuous: true`. Interim text appears in the
  message box as you speak, appended to what was already typed. Click again,
  press Enter, or stop speaking to end it.
- Language: Settings > Voice > Language (empty = the browser language).
- **Privacy.** In Chrome and Edge the recognizer streams audio to the
  vendor's cloud service; IMURUN never sees the audio. Before the first
  recording the shell asks the user to confirm this (stored as
  `imurun.voice.consent.v1`). This is the reason a local engine is planned.

| Environment | Behaviour |
|---|---|
| Chrome, Edge (desktop) | Works online; needs microphone permission |
| Firefox | No recognition API: button disabled, reason in its tooltip and in Settings > Voice |
| Safari | `webkitSpeechRecognition` works on recent versions |
| WebView2 (pywebview on Windows) | The object exists but the service usually refuses (`service-not-allowed` or `network`). The shell shows the reason once and disables the button for the session |
| Non-secure origin (LAN IP over http) | Disabled: recognition needs https or localhost |

Errors are mapped to plain messages: microphone refused, no microphone,
nothing heard, service unreachable (needs internet), language not supported.

### Read aloud (text to speech)

- Every finished reply has "Read aloud"; it toggles to "Stop reading".
- The Markdown answer is reduced to speakable text: code blocks become
  "(code block)", links read their label, URLs read "a link", list and table
  markup are removed.
- Text is spoken in sentence chunks of up to ~220 characters because Chrome
  stops long utterances after about 15 seconds. Hindi danda (।) ends a
  sentence too.
- Voice selection (`pickVoice`): the saved voice if it exists for the
  language; otherwise the best match for the language ranked natural/neural/
  online first, then Google voices, then the platform default, then local
  voices; voices named "compact" or "espeak" are ranked down. A language with
  no installed voice says so in the picker.
- Rate: 0.6x to 1.6x.
- Firefox and Safari support `speechSynthesis`; if a window has none, the
  buttons are hidden (`body.no-tts`) and Settings > Voice explains why.

### Settings > Voice

Stored in the server settings section `voice` (and cached locally):

```json
{ "lang": "en-IN", "voiceURI": "", "rate": 1, "sounds": true }
```

Languages always offered: en-IN, en-US, en-GB, hi-IN, bn-IN, de-DE, plus every
language reported by `speechSynthesis.getVoices()` (named with
`Intl.DisplayNames`). "Play a sample" speaks a sentence in the chosen
language (Hindi, Bengali and German samples are built in). `sounds` controls
the first-launch chime.

## Planned: a local speech engine on the server

So that voice works offline and audio never leaves the machine, the server
will provide STT and TTS (engine to be decided, e.g. a Whisper-class model
for STT and a Piper-class model for TTS). The frontend will prefer the
server engine when `GET /api/voice/status` reports it available, and fall
back to the browser engines otherwise.

### What the frontend expects

The full request list is item 13 in `API-REQUESTS.md`. In short:

```text
GET  /api/voice/status
  -> { "stt": { "available": true, "engine": "whisper-small", "languages": ["en", "hi", "bn", "de"] },
       "tts": { "available": true, "engine": "piper",
                "voices": [ { "id": "hi_IN-priyamvada", "name": "Priyamvada", "lang": "hi-IN" } ] } }

POST /api/voice/transcribe?lang=hi-IN
  Content-Type: audio/webm;codecs=opus   (MediaRecorder default) or audio/wav
  body: recorded audio (max a few MB; the server's body cap applies)
  -> { "text": "...", "lang": "hi-IN", "duration_seconds": 4.2,
       "segments": [ { "start": 0.0, "end": 1.8, "text": "..." } ] }

POST /api/voice/speak
  { "text": "...", "lang": "hi-IN", "voice_id": "hi_IN-priyamvada", "rate": 1.0 }
  -> audio/ogg (opus) or audio/wav bytes
```

Optional streaming dictation on the existing event broker:
`voice.partial {request_id, text}` and `voice.final {request_id, text}`.

### Frontend work when the endpoints exist

1. `voice.js`: on start, call `/api/voice/status`; if `stt.available`,
   record with `MediaRecorder` (`getUserMedia({audio: true})`) and post the
   blob on stop; show partials if the broker sends them.
2. If `tts.available`, fetch `/api/voice/speak`, play through an `Audio`
   element from a `blob:` URL, and keep the same Read aloud / Stop UI.
3. Settings > Voice gains an "Engine" choice: Local (server) / Browser.
4. Server CSP needs `media-src 'self' blob:` for playback (see
   `API-REQUESTS.md`).
