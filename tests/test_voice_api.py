"""/api/voice/* on a real in-process ApiServer, with fake engines (no faster-whisper/piper needed)."""
from __future__ import annotations

import http.client
import json
import math
import struct

import pytest

from services.persona.store import PersonaStore
from services.server.api import ApiServer
from services.skills.store import SkillsStore
from services.voice import audio, service as voice_service
from services.voice.catalogue import BUILTIN
from services.voice.service import VoiceService
from services.voice.worker import WorkerClient, WorkerError

HEAVY = ("faster_whisper", "piper", "onnxruntime", "av", "edge_tts")


class FakeModel:
    available = True

    def generate(self, messages):
        return "ok"

    def context_limit(self):
        return 4096


class FakeWorker:
    threads = 2
    idle_seconds = 600.0

    def __init__(self):
        self.calls = []

    def request(self, header, payload=b"", timeout=300.0):
        self.calls.append((header, payload))
        if header["op"] == "transcribe":
            if payload.startswith(b"BAD"):
                raise WorkerError("could not decode", "audio")
            lang = header.get("language") or getattr(self, "detect", "hi")
            return {"ok": True, "text": "नमस्ते", "language": lang, "language_probability": 0.9,
                    "confidence": 0.8, "duration": 0.5, "segments": [], "timings": {}}, b""
        if header["op"] == "speak":
            return ({"ok": True, "rate": 16000, "duration": 0.1, "timings": {}},
                    audio.wav_bytes(b"\0\0" * 1600, 16000))
        return {"ok": True}, b""

    def unload(self):
        pass

    def close(self):
        pass

    def status(self):
        return {"running": False}


class FakeSystem:
    backend = "fake"
    available = True

    def reason(self):
        return ""

    def voices(self, refresh=False):
        return []

    def for_language(self, lang):
        return []

    def speak(self, *args, **kwargs):
        raise RuntimeError("no system voice")


class FakeJob:
    def __init__(self, kind, name):
        self.kind, self.name = kind, name

    def view(self):
        return {"kind": self.kind, "name": self.name, "state": "downloading"}


def _tone(seconds=0.5):
    pcm = b"".join(struct.pack("<h", int(3000 * math.sin(i / 7))) for i in range(int(16000 * seconds)))
    return audio.wav_bytes(pcm, 16000)


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("IMURUN_VOICE_HOME", raising=False)
    # Never fetch the real catalogue from a test.
    monkeypatch.setattr(voice_service.Catalogue, "voices",
                        lambda self, refresh=False, allow_network=True: dict(BUILTIN))
    server = ApiServer(
        persona_store=PersonaStore(tmp_path / "persona.json"), sessions_root=tmp_path / "sessions",
        data_root=tmp_path, model=FakeModel(), skills=SkillsStore(tmp_path / "skills"), static_root=None,
    ).start()
    worker = FakeWorker()
    server._voice = VoiceService(data_root=tmp_path, settings=server._settings, publish=server._events.publish,
                                 worker=worker, system=FakeSystem())
    yield server, worker, tmp_path
    server.stop()


def installed(monkeypatch, *modules):
    real = voice_service.has_module
    monkeypatch.setattr(voice_service, "has_module",
                        lambda name: name in modules or (name not in HEAVY and real(name)))


def fake_whisper(tmp_path, size="small"):
    d = tmp_path / "voice" / "models" / "whisper" / size
    d.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (d / name).write_bytes(b"x")


def fake_piper(tmp_path, key):
    d = tmp_path / "voice" / "voices" / "piper" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.onnx").write_bytes(b"x")
    (d / f"{key}.onnx.json").write_text("{}")


def call(server, method, path, body=None, content_type="application/json", token=True):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    headers = {"Authorization": f"Bearer {server.token}"} if token else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        headers["Content-Type"] = content_type
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    out_headers = dict(resp.getheaders())
    conn.close()
    is_json = out_headers.get("Content-Type", "").startswith("application/json")
    return resp.status, (json.loads(raw) if is_json and raw else raw), out_headers


def test_auth_required(api):
    server, _, _ = api
    assert call(server, "GET", "/api/voice/status", token=False)[0] == 401


def test_without_engines_everything_is_503_with_reason(api, monkeypatch):
    server, worker, _ = api
    installed(monkeypatch)
    server._voice.system = type("NoSys", (FakeSystem,), {"available": False, "backend": "",
                                                          "reason": lambda self: "no espeak-ng"})()
    code, body, _ = call(server, "POST", "/api/voice/transcribe", _tone(), "audio/wav")
    assert code == 503 and body["error"] == "stt_unavailable" and "faster-whisper" in body["reason"]
    code, body, _ = call(server, "POST", "/api/voice/speak", {"text": "hello", "lang": "en"})
    assert code == 503 and body["error"] == "no_voice" and "piper" in body["reason"]
    code, status, _ = call(server, "GET", "/api/voice/status")
    assert code == 200 and status["stt"]["available"] is False and status["stt"]["reason"]
    assert status["tts"]["available"] is False and status["summary"].startswith("unavailable")
    assert status["tts"]["engines"]["online"]["enabled"] is False
    assert not worker.calls


def test_status_line_in_api_status(api, monkeypatch):
    server, _, _ = api
    installed(monkeypatch)
    code, body, _ = call(server, "GET", "/api/status")
    assert code == 200 and isinstance(body["voice"], str) and body["voice"].startswith(("partial", "unavailable"))


def test_transcribe_starts_download_when_model_missing(api, monkeypatch):
    server, worker, _ = api
    installed(monkeypatch, "faster_whisper", "av")
    started = []

    def fake_install(self, kind, name, wait=False):
        started.append((kind, name))
        return FakeJob(kind, name)

    monkeypatch.setattr(VoiceService, "install", fake_install)
    code, body, _ = call(server, "POST", "/api/voice/transcribe", _tone(), "audio/wav")
    assert code == 503 and body["error"] == "model_downloading" and body["job"]["name"] == "small"
    assert started == [("whisper", "small")] and not worker.calls


def test_transcribe_raw_and_multipart(api, monkeypatch):
    server, worker, tmp_path = api
    installed(monkeypatch, "faster_whisper", "av")
    fake_whisper(tmp_path)
    code, body, _ = call(server, "POST", "/api/voice/transcribe?lang=hi-IN", _tone(), "audio/wav")
    assert code == 200
    assert body["text"] == "नमस्ते" and body["language"] == body["lang"] == "hi"
    assert body["answer_language"] == "hi" and "duration_seconds" in body
    assert body["engine"] == "faster-whisper-small"
    header = worker.calls[-1][0]
    assert header["language"] == "" and "hi" in header["candidates"]  # auto-detect is on by default
    server._settings.write("voice", {"autoDetectLanguage": False})
    boundary = "b0und"
    multipart = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"lang\"\r\n\r\nde\r\n"
                 f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"x.webm\"\r\n"
                 f"Content-Type: audio/webm\r\n\r\n").encode() + b"\x1aE\xdf\xa3" + b"\0" * 64 \
        + f"\r\n--{boundary}--\r\n".encode()
    code, body, _ = call(server, "POST", "/api/voice/transcribe", multipart,
                         f"multipart/form-data; boundary={boundary}")
    assert code == 200 and body["language"] == "de"
    assert worker.calls[-1][0]["language"] == "de" and worker.calls[-1][1].startswith(b"\x1aE\xdf\xa3")


def test_transcribe_input_errors(api, monkeypatch):
    server, _, tmp_path = api
    installed(monkeypatch, "faster_whisper", "av")
    fake_whisper(tmp_path)
    assert call(server, "POST", "/api/voice/transcribe", b"", "audio/wav")[0] == 400
    assert call(server, "POST", "/api/voice/transcribe", b"hello there", "text/plain")[0] == 415
    code, body, _ = call(server, "POST", "/api/voice/transcribe", b"BAD" + b"\0" * 10, "audio/webm")
    assert code == 415 and "decode" in body["reason"]


def test_webm_without_decoder_is_503(api, monkeypatch):
    server, _, tmp_path = api
    installed(monkeypatch, "faster_whisper")  # no PyAV
    monkeypatch.setattr(voice_service, "ffmpeg_path", lambda: "")
    fake_whisper(tmp_path)
    code, body, _ = call(server, "POST", "/api/voice/transcribe", b"\x1aE\xdf\xa3" + b"\0" * 64, "audio/webm")
    assert code == 503 and body["error"] == "decoder_unavailable"
    assert call(server, "POST", "/api/voice/transcribe", _tone(), "audio/wav")[0] == 200  # WAV still works


def test_speak_piper_returns_wav_with_headers(api, monkeypatch):
    server, worker, tmp_path = api
    installed(monkeypatch, "piper", "onnxruntime")
    fake_piper(tmp_path, "hi_IN-priyamvada-medium")
    code, body, headers = call(server, "POST", "/api/voice/speak", {"text": "**आज** मौसम साफ है।", "lang": "en"})
    assert code == 200 and body[:4] == b"RIFF"
    assert headers["Content-Type"] == "audio/wav"
    assert headers["X-Voice-Engine"] == "piper" and headers["X-Voice-Language"] == "hi"  # script beats lang=en
    assert headers["X-Voice-Id"] == "piper:hi_IN-priyamvada-medium" and headers["X-Voice-Offline"] == "true"
    sent = worker.calls[-1]
    assert sent[1].decode() == "आज मौसम साफ है।" and sent[0]["model"].endswith("hi_IN-priyamvada-medium.onnx")


def test_speak_chosen_voice_and_speaker(api, monkeypatch):
    server, worker, tmp_path = api
    installed(monkeypatch, "piper", "onnxruntime")
    fake_piper(tmp_path, "bn_BD-google-medium")
    server._settings.write("voice", {"voices": {"bn": "piper:bn_BD-google-medium#3"}})
    code, _, _ = call(server, "POST", "/api/voice/speak", {"text": "আজ আকাশ মেঘলা", "language": "bn"})
    assert code == 200 and worker.calls[-1][0]["speaker"] == 3


def test_speak_voice_missing_starts_download(api, monkeypatch):
    server, _, _ = api
    installed(monkeypatch, "piper", "onnxruntime")
    monkeypatch.setattr(VoiceService, "install", lambda self, kind, name, wait=False: FakeJob(kind, name))
    code, body, _ = call(server, "POST", "/api/voice/speak", {"text": "Guten Tag", "lang": "de"})
    assert code == 503 and body["error"] == "voice_downloading" and body["job"]["name"] == "de_DE-thorsten-medium"


def test_online_fallback_never_silent(api, monkeypatch):
    server, _, _ = api
    installed(monkeypatch)
    used = []

    def fake_online(*args, **kwargs):
        used.append(args)
        return b"ID3fake", "en-US-AriaNeural"

    monkeypatch.setattr(voice_service.online, "speak", fake_online)
    assert call(server, "POST", "/api/voice/speak", {"text": "hello", "lang": "en"})[0] == 503
    assert not used
    server._settings.write("voice", {"onlineFallback": True})
    code, _, headers = call(server, "POST", "/api/voice/speak", {"text": "hello", "lang": "en"})
    assert code == 200 and headers["X-Voice-Engine"] == "online" and headers["X-Voice-Offline"] == "false"
    assert headers["Content-Type"] == "audio/mpeg" and used


def test_speak_bad_input(api):
    server, _, _ = api
    assert call(server, "POST", "/api/voice/speak", {"text": "   "})[0] == 400
    assert call(server, "POST", "/api/voice/speak", {"text": "x" * 6000})[0] == 400
    assert call(server, "POST", "/api/voice/speak", b"not json")[0] == 400


def test_voices_matrix_and_install_validation(api, monkeypatch):
    server, _, tmp_path = api
    installed(monkeypatch, "piper", "onnxruntime", "faster_whisper")
    fake_piper(tmp_path, "en_US-lessac-medium")
    code, body, _ = call(server, "GET", "/api/voice/voices")
    assert code == 200
    langs = body["languages"]
    for code_ in ("en", "hi", "bn", "de"):
        assert langs[code_]["stt"] is True and langs[code_]["voices"]
    en = {v["voice"]: v for v in langs["en"]["voices"]}
    assert en["piper:en_US-lessac-medium"]["installed"] is True
    assert en["piper:en_US-lessac-medium"]["offline"] is True
    assert body["stt_model"] == "small" and any(m["name"] == "large-v3" for m in body["stt_models"])
    assert call(server, "POST", "/api/voice/voices/install", {"engine": "whisper", "name": "huge"})[0] == 400
    assert call(server, "POST", "/api/voice/voices/install", {"engine": "piper", "name": "xx_XX-nope"})[0] == 400
    assert call(server, "POST", "/api/voice/voices/install", {"engine": "tape", "name": "x"})[0] == 400


def test_install_job_publishes_progress(api, monkeypatch):
    server, _, _ = api
    events = []
    server._voice._publish = lambda event, data: events.append((event, dict(data)))

    def fake_fetch(url, target, on_bytes=None, cancel=None, timeout=60.0):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 10)
        if on_bytes:
            on_bytes(10, 10)
        return 10

    monkeypatch.setattr(voice_service.download, "fetch_file", fake_fetch)
    code, body, _ = call(server, "POST", "/api/voice/voices/install", {"voice": "piper:de_DE-thorsten-medium"})
    assert code == 202 and body["job"]["kind"] == "piper"
    server._voice._jobs[body["job"]["id"]]._thread.join(10)
    assert events[-1][0] == "voice.model.download" and events[-1][1]["state"] == "done"
    assert server._voice.catalogue.installed("de_DE-thorsten-medium")


def test_settings_section_defaults(api):
    server, _, _ = api
    code, body, _ = call(server, "GET", "/api/settings/voice")
    values = body["values"]
    assert code == 200 and values["onlineFallback"] is False and values["sttModel"] == "small"
    assert values["threads"] == 2 and {"lang", "voiceURI", "rate", "sounds"} <= set(values)


def test_unknown_voice_route_404(api):
    server, _, _ = api
    assert call(server, "GET", "/api/voice/nope")[0] == 404


# ------------------------------------------------- the real worker process, with fake engines

def test_worker_subprocess_protocol(tmp_path):
    client = WorkerClient(cwd=tmp_path, threads=1, idle_seconds=0, env={"IMURUN_VOICE_FAKE": "1"})
    try:
        reply, _ = client.request({"op": "ping"})
        assert reply["fake"] is True and client.running
        reply, wav = client.request({"op": "speak", "model": "m", "config": "c"}, "नमस्ते".encode())
        assert wav[:4] == b"RIFF" and reply["duration"] > 0
        reply, _ = client.request({"op": "transcribe", "content_type": "audio/wav"}, _tone(0.25))
        assert reply["text"] == "fake transcript" and abs(reply["duration"] - 0.25) < 0.01
        with pytest.raises(WorkerError):
            client.request({"op": "explode"})
        assert client.running  # an error reply does not kill the worker
        client.unload()
        assert not client.running
        client.request({"op": "ping"})  # respawns on demand
        assert client.started == 2
    finally:
        client.close()
    assert not any(p.suffix == ".wav" for p in tmp_path.rglob("*"))


def test_speech_model_per_language(api, monkeypatch):
    """REQUESTS F1: a language can have its own Whisper model."""
    server, worker, tmp_path = api
    installed(monkeypatch, "faster_whisper", "av")
    monkeypatch.setattr(VoiceService, "whisper_installed", lambda self, size: True)
    server._settings.write("voice", {"sttModelByLanguage": {"bn": "large-v3-turbo", "hi": "small", "xx": "nope"}})
    assert server._voice.config()["sttModelByLanguage"] == {"bn": "large-v3-turbo"}  # same-as-default and unknown dropped
    # Forced language with its own model: one pass on that model.
    server._settings.write("voice", {"autoDetectLanguage": False})
    worker.calls.clear()
    code, body, _ = call(server, "POST", "/api/voice/transcribe?lang=bn", _tone(), "audio/wav")
    assert code == 200 and body["model"] == "large-v3-turbo" and body["engine"] == "faster-whisper-large-v3-turbo"
    assert len(worker.calls) == 1 and worker.calls[0][0]["model_dir"].endswith("large-v3-turbo")
    # Auto-detect on a CPU-only machine: default model first, then a second
    # pass with the language fixed. (On a GPU, large-v3-turbo is the default.)
    server._settings.write("voice", {"autoDetectLanguage": True, "device": "cpu"})
    worker.calls.clear()
    worker.detect = "bn"
    code, body, _ = call(server, "POST", "/api/voice/transcribe", _tone(), "audio/wav")
    assert code == 200 and body["model"] == "large-v3-turbo" and body["language"] == "bn"
    assert [c[0]["model_dir"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for c in worker.calls] == ["small", "large-v3-turbo"]
    assert worker.calls[1][0]["language"] == "bn" and worker.calls[1][0]["candidates"] == []
    # Detected language without an override: one pass, default model.
    worker.calls.clear()
    worker.detect = "de"
    code, body, _ = call(server, "POST", "/api/voice/transcribe", _tone(), "audio/wav")
    assert code == 200 and body["model"] == "small" and len(worker.calls) == 1
    # The language's model is missing: first-pass text is returned with model_pending.
    worker.calls.clear()
    worker.detect = "bn"
    monkeypatch.setattr(VoiceService, "whisper_installed", lambda self, size: size == "small")
    monkeypatch.setattr(VoiceService, "install", lambda self, kind, name, wait=False: FakeJob(kind, name))
    code, body, _ = call(server, "POST", "/api/voice/transcribe", _tone(), "audio/wav")
    assert code == 200 and body["model"] == "small" and body["model_pending"]["model"] == "large-v3-turbo"
    assert body["model_pending"]["error"] == "model_downloading"
