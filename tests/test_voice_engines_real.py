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


# ------------------------------------------------- GPU path, silence guard, eval harness (2026-11-03)

def _turbo_ready(service):
    if not service.whisper_installed("large-v3-turbo"):
        pytest.skip("whisper large-v3-turbo not downloaded")


def test_room_noise_gives_empty_transcript(service):
    """3 s of synthetic room noise must not become "Thank you for watching"."""
    import numpy as np

    from services.voice.audio import wav_bytes

    rng = np.random.default_rng(3)
    white = rng.standard_normal(48000)
    spectrum = np.fft.rfft(white) / np.sqrt(np.arange(1, 24002))
    pink = np.fft.irfft(spectrum, 48000)
    pink = 0.02 * pink / np.max(np.abs(pink))
    for signal in (np.zeros(48000), pink, rng.standard_normal(48000) * 0.03):
        wav = wav_bytes((np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes(), 16000)
        heard = service.transcribe(wav, "audio/wav", "")
        assert heard["text"] == "" and heard["text_raw"] == "", heard


def test_gpu_turbo_latency(service):
    """large-v3-turbo on CUDA: a ~5 s utterance well under 1.5 s once warm."""
    from services.voice.catalogue import DEFAULT_VOICE

    _turbo_ready(service)
    if not service.catalogue.installed(DEFAULT_VOICE["en"]):
        pytest.skip("English Piper voice not downloaded")
    speech = service.speak("Load the Phi three model in fish and run it on Vulkan, then compare it with Llama.", "en")
    service.transcribe(speech.audio, "audio/wav", "")          # spawn + load + CUDA warm-up
    heard = service.transcribe(speech.audio, "audio/wav", "")
    if heard.get("device") != "cuda":
        pytest.skip(f"not on the GPU here: {heard.get('device_reason')}")
    assert heard["model"] == "large-v3-turbo"
    assert heard["timings"]["transcribe"] < 1.5, heard["timings"]
    assert "Phi-3" in heard["text"] or "phi" in heard["text_raw"].lower()


def test_eval_harness_runs(tmp_path):
    """python -m services.voice.eval synth + run on a tiny set."""
    import json
    import subprocess
    import sys

    from services.voice.service import VoiceService

    svc = VoiceService(data_root=os.path.expanduser("~/.imurun"))
    try:
        if not svc.whisper_installed("small") or not svc.catalogue.installed("en_US-lessac-medium"):
            pytest.skip("small / en_US-lessac-medium not downloaded")
    finally:
        svc.close()
    run = lambda *a: subprocess.run([sys.executable, "-m", "services.voice.eval", *a],  # noqa: E731
                                    capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert run("synth", "--data", str(tmp_path), "--set", "disfluent").returncode == 0
    out = tmp_path / "runs.jsonl"
    done = run("run", "--data", str(tmp_path), "--sets", "disfluent", "--limit", "3", "--cleanup",
               "--device", "auto", "--out", str(out))
    assert done.returncode == 0, done.stderr[-2000:]
    summary = json.loads(out.read_text(encoding="utf-8").splitlines()[-1])
    entry = summary["sets"]["disfluent"]
    assert entry["n"] == 3 and entry["wer_clean"] <= entry["wer"]
