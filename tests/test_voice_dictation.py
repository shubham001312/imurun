"""Dictation quality layer without engines: clean-up, biasing prompt, decoding
config, GPU rules, WAV variants, the model pass's guards, and the new routes
(transcribe context/cleanup, vocabulary, corrections, streaming) on a real
ApiServer with a fake worker."""
from __future__ import annotations

import json
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from services.voice import audio, cleanup, engines, gpu, polish, vocabulary
from services.voice.cleanup import clean, normalize_for_score, numbers_to_digits

from test_voice_api import FakeJob, _tone, api, call, fake_whisper, installed  # noqa: F401 - fixtures


# ------------------------------------------------------------------ clean-up

@pytest.mark.parametrize("raw, expected", [
    ("Um, so I want to, uh, send the report on Tuesday, no wait, Wednesday.",
     "So I want to send the report on Wednesday."),
    ("Can you, uh, can you check the the logs for me?", "Can you check the logs for me?"),
    ("Set the timer for ten minutes, I mean fifteen minutes.", "Set the timer for 15 minutes."),
    ("You know, I think the model is, uh, pretty fast.", "I think the model is pretty fast."),
    ("Book a table for four people, actually make that six people.", "Book a table for six people."),
    ("The meeting is at three, no sorry, at four pm.", "The meeting is at four pm."),
    ("Please write a short email to Rahul comma thanking him for the help full stop",
     "Please write a short email to Rahul, thanking him for the help."),
    ("What is the, uh, what is the capital of France question mark", "What is the capital of France?"),
    ("Open the file, uh, sorry, open the folder.", "Open the folder."),
    ("Move the call to Thursday, no, Friday.", "Move the call to Friday."),
    ("first buy milk new line second call mom", "First buy milk\nSecond call mom."),
])
def test_cleanup_english(raw, expected):
    assert clean(raw, "en").text == expected


@pytest.mark.parametrize("text", [
    "Is it ready? No, not yet.",
    "He said no to the offer.",
    "This is a very very good point.",
    "I had had enough.",
    "It ended in a long period.",
    "I like, really like this.",
])
def test_cleanup_leaves_ordinary_speech_alone(text):
    assert clean(text, "en").text == text


def test_cleanup_other_languages():
    assert clean("Ähm, ich möchte, äh, morgen kommen, nein warte, übermorgen.", "de").text == \
        "Ich möchte übermorgen kommen."
    assert clean("Das ist ein wichtiger Punkt, den wir besprechen.", "de").text == \
        "Das ist ein wichtiger Punkt, den wir besprechen."
    assert clean("मुझे कल उम्म नहीं नहीं परसों जाना है", "hi").text == "मुझे परसों जाना है"
    # Reduplication is grammar in Hindi, not a stutter.
    assert clean("मुझे धीरे धीरे चलना है", "hi").text == "मुझे धीरे धीरे चलना है"
    assert clean("पहला काम कॉमा दूसरा काम", "hi").text == "पहला काम, दूसरा काम"


def test_cleanup_corrections_and_changes():
    result = clean("load five 3 in fish", "en", corrections=[{"heard": "five 3", "fixed": "Phi-3"}])
    assert result.text == "Load Phi-3 in fish." and "correction:five 3->Phi-3" in result.changes
    assert clean("", "en").text == "" and clean("   ", "hi").changes == []


def test_numbers():
    assert numbers_to_digits("twenty five copies and three point five liters, fifty percent") == \
        "25 copies and 3.5 liters, 50%"
    assert numbers_to_digits("on the twenty first of May") == "on the 21st of May"
    assert numbers_to_digits("three apples") == "three apples"            # below ten stays a word
    assert numbers_to_digits("one hundred and twenty") == "120"
    assert normalize_for_score("Twelve degrees, 25% of THE twenty-one cats") == \
        ["12", "degrees", "25", "percent", "of", "the", "21", "cats"]
    assert normalize_for_score("आज २५ डिग्री है।", "hi") == ["आज", "25", "डिग्री", "है"]


# ------------------------------------------------------------------ biasing prompt

def test_prompt_has_product_terms_and_user_words_first():
    prompt = vocabulary.build_prompt(vocabulary=["Rahul Verma"], corrections=[{"heard": "five 3", "fixed": "Phi-3"}])
    assert prompt.startswith("Glossary: Phi-3, Rahul Verma, ")
    assert "IMURUN" in prompt and "GGUF" in prompt and prompt.endswith(".")
    assert len(prompt) <= vocabulary.MAX_PROMPT_CHARS


def test_prompt_context_terms():
    terms = vocabulary.context_terms(["Please ask Priya about the Qwen2.5 run on DeepSeek. The GGUF is ready.",
                                      "What about it? See https://Example.com/Thing"])
    assert "Priya" in terms and "Qwen2.5" in terms and "DeepSeek" in terms and "GGUF" in terms
    assert "Please" not in terms and "What" not in terms and "Example" not in terms


def test_prompt_respects_script_and_budget():
    assert vocabulary.build_prompt(language="hi", vocabulary=["Phi-3"]) == ""   # no Latin glossary for Hindi
    assert vocabulary.build_prompt(language="hi", vocabulary=["दिल्ली"], product_terms=False) == "Glossary: दिल्ली."
    long = vocabulary.build_prompt(vocabulary=[f"Term{i:03d}" for i in range(400)])
    assert len(long) <= vocabulary.MAX_PROMPT_CHARS
    assert vocabulary.build_prompt(product_terms=False) == ""


# ------------------------------------------------------------------ decoding config

def test_decoding_and_vad_config():
    assert engines.DECODE["condition_on_previous_text"] is False
    assert engines.DECODE["no_speech_threshold"] == 0.6 and engines.DECODE["compression_ratio_threshold"] == 2.4
    assert engines.DECODE["word_timestamps"] is False
    assert engines.VAD["speech_pad_ms"] >= 200 and 0.3 <= engines.VAD["threshold"] <= 0.7
    assert len(engines.TEMPERATURES["cpu"]) <= 2          # BUGS-FIXED #5: bounded on CPU
    assert engines.TEMPERATURES["cuda"][0] == 0.0


def test_hallucination_filter():
    seg = lambda text, nsp=0.1, lp=-0.3: {"text": text, "no_speech_prob": nsp, "avg_logprob": lp,  # noqa: E731
                                           "start": 0.0, "end": 1.0}
    kept, dropped = engines.WhisperSTT.drop_hallucinations(
        [seg("Thank you for watching!"), seg("Hello there.")], speech_seconds=0.4)
    assert [s["text"] for s in kept] == ["Hello there."] and dropped == 1
    # Real speech that happens to say "thank you" is kept when the model was sure.
    kept, _ = engines.WhisperSTT.drop_hallucinations([seg("Thank you.")], speech_seconds=3.0)
    assert len(kept) == 1
    kept, dropped = engines.WhisperSTT.drop_hallucinations([seg("anything", nsp=0.9, lp=-1.2)], 3.0)
    assert kept == [] and dropped == 1


def test_gpu_rules(monkeypatch):
    assert gpu.fits("large-v3-turbo", 1400) and not gpu.fits("large-v3-turbo", 1200)
    assert gpu.fits("small", 800) and gpu.fits("small", None)
    stt = engines.WhisperSTT()
    monkeypatch.setattr(stt, "cuda_status", lambda: (True, ""))
    monkeypatch.setattr(gpu, "memory", lambda index=0: {"free_mb": 900, "total_mb": 4096})
    device, ctype, reason = stt._choose("large-v3-turbo", "auto", "auto")
    assert device == "cpu" and "900 MB free" in reason
    assert stt._choose("large-v3-turbo", "cpu", "auto")[0] == "cpu"
    device, _, _ = stt._choose("small", "auto", "auto")
    assert device == "cuda"
    assert stt._choose("large-v3-turbo", "cuda", "auto")[0] == "cuda"   # forced: let CUDA try
    monkeypatch.setattr(stt, "cuda_status", lambda: (False, "no CUDA device (CPU only)"))
    assert stt._choose("small", "auto", "auto") == ("cpu", "int8", "no CUDA device (CPU only)")


def test_cuda_library_dirs_are_prepended(monkeypatch, tmp_path):
    bin_dir = tmp_path / "nvidia" / "cublas" / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setattr(gpu, "_site_dirs", lambda: [tmp_path])
    monkeypatch.setattr(gpu, "_PATH_DONE", False)
    monkeypatch.setenv("PATH", "C:/elsewhere")
    assert gpu.prepare_cuda_path() == [str(bin_dir)]
    import os

    assert os.environ["PATH"].startswith(str(bin_dir))


# ------------------------------------------------------------------ WAV variants

def _float_wav(samples, rate=16000, extensible=False):
    data = struct.pack(f"<{len(samples)}f", *samples)
    if extensible:
        fmt = struct.pack("<HHIIHHHHI", 0xFFFE, 1, rate, rate * 4, 4, 32, 22, 32, 4) + \
            struct.pack("<H", 3) + b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    else:
        fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.parametrize("extensible", [False, True])
def test_float_wav_is_readable(extensible):
    np = pytest.importorskip("numpy")
    blob = _float_wav([0.0, 0.5, -0.5, 0.25] * 4000, extensible=extensible)
    info = audio.wav_info(blob)
    assert info.fmt == 3 and info.frames == 16000 and abs(info.duration - 1.0) < 1e-6
    samples = audio.decode_16k(blob)
    assert samples.dtype == np.float32 and samples.shape[0] == 16000 and abs(float(samples[1]) - 0.5) < 1e-6


def test_pcm_wav_still_readable():
    info = audio.wav_info(_tone(0.25))
    assert info.fmt == 1 and info.width == 2 and info.frames == 4000
    with pytest.raises(audio.AudioError):
        audio.wav_info(b"RIFF\x00\x00\x00\x00WAVEjunk")


# ------------------------------------------------------------------ model pass guards

def test_polish_skips_without_loaded_idle_model():
    assert polish.polish(None, "hello there", "en")["reason"] == "no FISH model loaded"
    lock = threading.Lock()
    busy = SimpleNamespace(loaded=True, _gen_lock=lock)
    lock.acquire()
    try:
        assert polish.polish(SimpleNamespace(_native=busy), "hello there", "en")["reason"] == "engine busy"
    finally:
        lock.release()
    unloaded = SimpleNamespace(loaded=False, _gen_lock=threading.Lock())
    assert polish.polish(SimpleNamespace(_native=unloaded), "hi", "en")["used"] is False


def test_polish_uses_answer_and_respects_budget():
    pytest.importorskip("ctypes")

    class Native:
        loaded = True

        def __init__(self, answer, delay=0.0):
            self._gen_lock = threading.Lock()
            self.answer, self.delay, self.cancelled = answer, delay, False

        def _generate_chat(self, messages, params, callback):
            with self._gen_lock:
                assert messages[0]["role"] == "system" and params.temperature == 0.0
                time.sleep(self.delay)
                for word in self.answer.split(" "):
                    callback((word + " ").encode(), 0, None)
                    if self.cancelled:
                        return 0
            return 1

        def cancel(self):
            self.cancelled = True

    good = polish.polish(Native("Load Phi-3 in FISH."), "Load five 3 in fish.", "en", budget_ms=2000)
    assert good["used"] is True and good["text"] == "Load Phi-3 in FISH."
    chatty = polish.polish(Native("Sure! Here is the corrected text you wanted"), "load it now", "en")
    assert chatty["used"] is False and "rejected" in chatty["reason"]
    slow = Native("Load Phi-3 in FISH.", delay=0.5)
    late = polish.polish(slow, "Load five 3 in fish.", "en", budget_ms=100)
    assert late["used"] is False and "over budget" in late["reason"]
    time.sleep(0.7)
    assert slow.cancelled is True       # cancelled from inside its own token callback
    assert polish.acceptable("नमस्ते दुनिया", "Namaste duniya") is False   # script changed


# ------------------------------------------------------------------ routes

def _ready(server, monkeypatch, tmp_path):
    installed(monkeypatch, "faster_whisper", "av")
    fake_whisper(tmp_path, "small")
    fake_whisper(tmp_path, "large-v3-turbo")


def test_transcribe_returns_clean_and_raw_text(api, monkeypatch):
    server, worker, tmp_path = api
    _ready(server, monkeypatch, tmp_path)
    worker.text = "Um, send it on Tuesday, no wait, Wednesday"
    original = worker.request

    def request(header, payload=b"", timeout=300.0):
        reply, data = original(header, payload, timeout)
        if header["op"] == "transcribe":
            reply = dict(reply, text=worker.text, language="en")
        return reply, data

    worker.request = request
    code, body, _ = call(server, "POST", "/api/voice/transcribe", _tone(), "audio/wav")
    assert code == 200 and body["text_raw"] == "Um, send it on Tuesday, no wait, Wednesday"
    assert body["text"] == "Send it on Wednesday." and body["cleanup"]["applied"] is True
    assert "filler:um" in body["cleanup"]["changes"] and body["cleanup"]["model"]["used"] is False
    code, body, _ = call(server, "POST", "/api/voice/transcribe?cleanup=0", _tone(), "audio/wav")
    assert body["text"] == body["text_raw"] and body["cleanup"]["applied"] is False


def test_transcribe_context_reaches_prompt(api, monkeypatch):
    server, worker, tmp_path = api
    _ready(server, monkeypatch, tmp_path)
    server._settings.write("voice", {"vocabulary": ["Rahul"]})
    context = json.dumps([{"role": "user", "content": "Ask Priyanka about the Qwen2.5 build"}])
    from urllib.parse import quote

    code, body, _ = call(server, "POST", "/api/voice/transcribe?context=" + quote(context), _tone(), "audio/wav")
    header = worker.calls[-1][0]
    assert code == 200 and body["prompted"] is True
    assert "Rahul" in header["prompt"] and "Priyanka" in header["prompt"] and "Qwen2.5" in header["prompt"]
    assert header["device"] == "auto" and header["model_dir"].endswith("large-v3-turbo")
    assert header["cpu_model_dir"].endswith("small")
    # Multipart field form, and biasing off.
    boundary = "xyz"
    parts = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"context\"\r\n\r\nMeet Ananya at Vercel\r\n"
             f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"a.wav\"\r\n"
             f"Content-Type: audio/wav\r\n\r\n").encode() + _tone() + f"\r\n--{boundary}--\r\n".encode()
    call(server, "POST", "/api/voice/transcribe", parts, f"multipart/form-data; boundary={boundary}")
    assert "Ananya" in worker.calls[-1][0]["prompt"]
    server._settings.write("voice", {"contextBiasing": False})
    call(server, "POST", "/api/voice/transcribe", _tone(), "audio/wav")
    assert worker.calls[-1][0]["prompt"] == ""


def test_plan_cpu_only_and_gpu_download(api, monkeypatch):
    server, worker, tmp_path = api
    installed(monkeypatch, "faster_whisper", "av")
    fake_whisper(tmp_path, "small")
    worker.info = {"cuda": {"cuda": False, "reason": "no CUDA device (CPU only)"}}
    call(server, "POST", "/api/voice/transcribe", _tone(), "audio/wav")
    header = worker.calls[-1][0]
    assert header["device"] == "cpu" and header["model_dir"].endswith("small")
    # CUDA works but turbo is missing: download it, transcribe with small meanwhile.
    worker.info = {"cuda": {"cuda": True, "free_mb": 2000}}
    monkeypatch.setattr(type(server._voice), "install", lambda self, kind, name, wait=False: FakeJob(kind, name))
    code, body, _ = call(server, "POST", "/api/voice/transcribe", _tone(), "audio/wav")
    assert code == 200 and body["model_pending"]["model"] == "large-v3-turbo"
    assert worker.calls[-1][0]["model_dir"].endswith("small") and worker.calls[-1][0]["device"] == "auto"
    status = call(server, "GET", "/api/voice/status")[1]
    assert status["gpu"]["cuda"] is True and status["settings"]["sttModelGpu"] == "large-v3-turbo"


def test_vocabulary_and_corrections_routes(api):
    server, _, _ = api
    code, body, _ = call(server, "GET", "/api/voice/vocabulary")
    assert code == 200 and body["vocabulary"] == [] and "IMURUN" in body["product_terms"]
    code, body, _ = call(server, "POST", "/api/voice/vocabulary", {"add": ["Rahul", "rahul", "Phi-3"]})
    assert body["vocabulary"] == ["Rahul", "Phi-3"]
    code, body, _ = call(server, "POST", "/api/voice/vocabulary", {"remove": ["RAHUL"]})
    assert body["vocabulary"] == ["Phi-3"]
    code, body, _ = call(server, "POST", "/api/voice/correction", {"heard": "jemma", "fixed": "Gemma"})
    assert code == 200 and body["corrections"] == [{"heard": "jemma", "fixed": "Gemma"}]
    assert "Gemma" in body["vocabulary"] and body["prompt_preview"].startswith("Glossary: Gemma")
    assert call(server, "POST", "/api/voice/correction", {"heard": "x"})[0] == 400
    code, body, _ = call(server, "POST", "/api/voice/correction", {"remove": "jemma"})
    assert body["corrections"] == []
    assert server._settings.read("voice")["vocabulary"] == ["Phi-3", "Gemma"]


def test_streaming_partials_and_final(api, monkeypatch):
    server, worker, tmp_path = api
    _ready(server, monkeypatch, tmp_path)
    events = []
    server._voice._publish = lambda event, data: events.append((event, data))
    original = worker.request
    wav = _tone(1.0)

    def request(header, payload=b"", timeout=300.0):
        reply, data = original(header, payload, timeout)
        if header["op"] == "transcribe":
            # The session re-sends the whole stream: more chunks, more words.
            text = "Hello" if len(payload) <= len(wav) else "Hello world, um, again"
            reply = dict(reply, text=text, language="en", duration=3.0,
                         segments=[{"start": 0.0, "end": 1.5, "text": text}])
        return reply, data

    worker.request = request
    code, body, _ = call(server, "POST", "/api/voice/stream?id=s1&seq=0", wav, "audio/wav")
    assert code == 200 and body["accepted"] is True and body["final"] is False
    deadline = time.time() + 5
    while not any(e == "voice.partial" for e, _ in events) and time.time() < deadline:
        time.sleep(0.05)
    partial = next(d for e, d in events if e == "voice.partial")
    assert partial["id"] == "s1" and partial["text"].startswith("Hello") and partial["endpoint"] is True
    assert call(server, "POST", "/api/voice/stream?id=s1&seq=0", wav, "audio/wav")[0] == 200   # repeat ignored
    assert call(server, "POST", "/api/voice/stream?id=s1&seq=5", wav, "audio/wav")[0] == 409   # gap
    code, body, _ = call(server, "POST", "/api/voice/stream?id=s1&seq=1&final=1", wav, "audio/wav")
    assert code == 200 and body["final"] is True and body["text"] == "Hello world again."
    assert body["text_raw"] == "Hello world, um, again"
    assert any(e == "voice.final" and d["id"] == "s1" for e, d in events)
    assert server._voice.streams.get("s1") is None
    assert call(server, "POST", "/api/voice/stream?seq=0", wav, "audio/wav")[0] == 400        # no id
    assert call(server, "POST", "/api/voice/stream/cancel?id=nope", b"", "application/json")[1] == {"ok": False}


def test_worker_reports_gpu_release(tmp_path):
    from services.voice.worker import WorkerClient

    client = WorkerClient(cwd=tmp_path, threads=1, idle_seconds=0, env={"IMURUN_VOICE_FAKE": "1"})
    try:
        reply, _ = client.request({"op": "ping"})
        assert "cuda" in reply
        reply, _ = client.request({"op": "probe"})
        assert reply["cuda"] is False
        reply, _ = client.request({"op": "release_gpu"})
        assert reply["released"] is False
        status = client.status()
        assert status["on_gpu"] is False and status["gpu_idle_release_s"] == 120.0
    finally:
        client.close()


def test_gpu_fits_respects_governor_headroom():
    assert gpu.fits("small", 800)
    assert not gpu.fits("small", 800, headroom_mb=1024)   # governor wants 1 GB free
    assert gpu.fits("small", 1500, headroom_mb=1024)


def test_whisper_choose_uses_governor_headroom(monkeypatch):
    stt = engines.WhisperSTT()
    monkeypatch.setattr(stt, "cuda_status", lambda: (True, ""))
    monkeypatch.setattr(gpu, "memory", lambda index=0: {"free_mb": 1000, "total_mb": 4096})
    assert stt._choose("small", "auto", "auto")[0] == "cuda"
    stt.vram_headroom_mb = 1024
    device, _, why = stt._choose("small", "auto", "auto")
    assert device == "cpu" and "1024 MB headroom" in why


def test_eval_score_ignores_case_punctuation():
    from services.voice import eval as ev

    row = ev.score("Hello, world.", "hello world", "en")
    assert row["wer_err"] == 0 and row["cer"] == 0
