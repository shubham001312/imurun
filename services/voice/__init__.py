"""Server-side voice: speech-to-text and text-to-speech (see docs/voice/README.md).

Importing this package is cheap and never imports an engine: faster-whisper
and Piper run in a separate worker process (``worker.py``), started on the
first request and stopped after ``voice.idleUnloadMinutes`` idle. Without the
optional packages (``requirements-voice.txt``) every ``/api/voice/*`` route
answers 503 with the reason, and ``/api/status.voice`` says so.
"""
