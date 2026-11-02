"""Voice layer units that need no engine: languages, text, audio helpers, catalogue."""
from __future__ import annotations

import math
import struct

import pytest

from services.voice import audio, languages, text
from services.voice.catalogue import BUILTIN, DEFAULT_VOICE, Catalogue
from services.voice.engines import devanagari_to_bengali
from services.voice.paths import VoicePaths


def _tone(seconds=0.5, rate=16000):
    frames = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(4000 * math.sin(i / 10))) for i in range(frames))
    return audio.wav_bytes(pcm, rate)


@pytest.mark.parametrize("value,code", [("hi-IN", "hi"), ("hi_IN", "hi"), ("Hindi", "hi"), ("বাংলা", "bn"),
                                        ("DE", "de"), ("auto", ""), ("", ""), ("en-GB", "en"), ("iw", "he")])
def test_normalize(value, code):
    assert languages.normalize(value) == code


def test_answer_directive():
    assert languages.answer_directive("en") == ""
    assert languages.answer_directive("") == ""
    line = languages.answer_directive("hi-IN")
    assert "Hindi" in line and "हिन्दी" in line and line.startswith("- ")
    assert "Bengali" in languages.answer_directive("bn")


def test_voice_language_follows_script():
    assert languages.voice_language("en", "आज मौसम साफ है।") == "hi"
    assert languages.voice_language("mr", "आज हवामान छान आहे") == "mr"   # same script: keep request
    assert languages.voice_language("", "আজ আকাশ মেঘলা") == "bn"
    assert languages.voice_language("de", "Guten Tag") == "de"
    assert languages.voice_language("", "Hello") == "en"


def test_core_languages_supported_by_whisper():
    for code in languages.CORE:
        assert languages.whisper_supports(code)


def test_speakable_strips_markdown():
    md = "# Title\n\n- **Bold** item with [a link](https://x.y)\n\n```py\nprint(1)\n```\nSee https://example.com [1]"
    out = text.speakable(md)
    assert "**" not in out and "#" not in out and "print" not in out
    assert "a link" in out and "(code block)" in out and "[1]" not in out


def test_sentences_split_on_danda_and_length():
    parts = text.sentences("आज मौसम साफ है। कल बारिश होगी। Fine. Ok?")
    assert parts == ["आज मौसम साफ है।", "कल बारिश होगी।", "Fine.", "Ok?"]
    long = ", ".join(["word"] * 120)
    assert all(len(p) <= text.MAX_CHARS + 1 for p in text.sentences(long))


def test_sniff_and_wav_roundtrip():
    wav = _tone()
    assert audio.sniff(wav) == "wav"
    assert audio.sniff(b"\x1aE\xdf\xa3" + b"\0" * 20) == "webm"
    assert audio.sniff(b"OggS" + b"\0" * 20) == "ogg"
    assert audio.sniff(b"ID3" + b"\0" * 20) == "mp3"
    assert audio.sniff(b"????", "audio/webm;codecs=opus") == "webm"
    assert audio.sniff(b"????", "text/plain") == ""
    info = audio.wav_info(wav)
    assert info.rate == 16000 and abs(info.duration - 0.5) < 0.01
    with pytest.raises(audio.AudioError):
        audio.wav_info(b"RIFF....WAVEjunk")


def test_decode_wav_resamples_to_16k():
    pytest.importorskip("numpy")
    samples = audio.decode_16k(_tone(1.0, rate=44100))
    assert abs(samples.shape[0] - 16000) <= 1
    assert samples.dtype.name == "float32"


def test_multipart():
    wav = _tone(0.1)
    body = (b"--XX\r\nContent-Disposition: form-data; name=\"lang\"\r\n\r\nhi-IN\r\n"
            b"--XX\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"a.wav\"\r\n"
            b"Content-Type: audio/wav\r\n\r\n" + wav + b"\r\n--XX--\r\n")
    data, kind, fields = audio.parse_multipart(body, "multipart/form-data; boundary=XX")
    assert data == wav and kind == "audio/wav" and fields == {"lang": "hi-IN"}
    with pytest.raises(audio.AudioError):
        audio.parse_multipart(b"--XX--", "multipart/form-data; boundary=XX")


def test_devanagari_to_bengali():
    assert devanagari_to_bengali("आज कोलकाता") == "আজ কোলকাতা"
    assert devanagari_to_bengali("वह") == "বহ"
    assert devanagari_to_bengali("already বাংলা") == "already বাংলা"


def test_builtin_catalogue_covers_core(tmp_path):
    cat = Catalogue(VoicePaths(tmp_path))
    voices = cat.voices(allow_network=False)
    assert set(DEFAULT_VOICE) == set(languages.CORE)
    for code, key in DEFAULT_VOICE.items():
        assert key in voices and cat.for_language(code)[0]["key"] == key
    files = cat.files("hi_IN-priyamvada-medium")
    assert files[0][0].startswith("https://huggingface.co/rhasspy/piper-voices/resolve/main/hi/hi_IN/")
    assert all(str(target).startswith(str(tmp_path)) for _, target, _ in files)
    assert not cat.installed("hi_IN-priyamvada-medium")
    assert len(BUILTIN) >= 9


def test_paths_never_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("IMURUN_VOICE_HOME", raising=False)
    paths = VoicePaths.at(tmp_path)
    assert paths.root == tmp_path / "voice"
    assert paths.piper_model("../../evil").parent.parent == paths.root / "voices" / "piper"
