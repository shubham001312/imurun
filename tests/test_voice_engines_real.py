"""Real engines, real models: Piper speaks, faster-whisper transcribes it back.

Opt-in (slow, needs requirements-voice.txt and the downloaded models in
~/.imurun/voice or IMURUN_VOICE_HOME):

    set IMURUN_VOICE_TESTS=1
    python -m services.voice install whisper small
    python -m services.voice install piper en_US-lessac-medium   (and de/hi/bn defaults)
    python -m pytest tests/test_voice_engines_real.py
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.getenv("IMURUN_VOICE_TESTS", "").strip() != "1",
                                reason="real voice engines; set IMURUN_VOICE_TESTS=1")

# Loose bounds: the measured CER (docs/voice/README.md) is 0.08-0.12 for en/de/hi.
# Bengali with whisper-small is 0.31-0.42 in 5 of 6 runs and garbage (1.0) in
# the sixth (Piper's sampling noise differs per run), so only its detected
# language is asserted - an honest "works, not reliably" rather than a flaky test.
MAX_CER = {"en": 0.2, "de": 0.2, "hi": 0.25, "bn": None}


@pytest.fixture(scope="module")
def service():
    pytest.importorskip("faster_whisper")
    pytest.importorskip("piper")
    from services.voice.service import VoiceService

    svc = VoiceService(data_root=os.path.expanduser("~/.imurun"))
    if not svc.whisper_installed("small"):
        pytest.skip("whisper small not downloaded")
    yield svc
    svc.close()


@pytest.mark.parametrize("lang", ["en", "de", "hi", "bn"])
def test_roundtrip(service, lang):
    from services.voice.__main__ import ROUNDTRIP, error_rates
    from services.voice.catalogue import DEFAULT_VOICE

    if not service.catalogue.installed(DEFAULT_VOICE[lang]):
        pytest.skip(f"{DEFAULT_VOICE[lang]} not downloaded")
    speech = service.speak(ROUNDTRIP[lang], lang)
    assert speech.engine == "piper" and speech.audio[:4] == b"RIFF"
    heard = service.transcribe(speech.audio, "audio/wav", "")
    assert heard["language"] == lang
    _, cer = error_rates(ROUNDTRIP[lang], heard["text"])
    if MAX_CER[lang] is not None:
        assert cer <= MAX_CER[lang], heard["text"]
