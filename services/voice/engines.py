"""The heavy engines. Imported ONLY inside the voice worker process.

* ``WhisperSTT`` - faster-whisper (CTranslate2), CPU int8, ``cpu_threads``
  bounded (default 2), language auto-detection.
* ``PiperTTS`` - Piper ONNX voices; the onnxruntime session is built here
  (not with ``PiperVoice.load``) so the thread count can be bounded and
  Piper's ``download_dir`` never defaults to the working directory.
* ``FakeSTT`` / ``FakeTTS`` - deterministic stand-ins (``IMURUN_VOICE_FAKE=1``)
  so the worker protocol can be tested without either package.

Module level imports are stdlib only; numpy / faster_whisper / piper are
imported when an engine is first used.
"""
from __future__ import annotations

import importlib.util
import json
import math
import struct
import time
from collections import OrderedDict
from pathlib import Path

from .audio import TARGET_RATE, decode_16k, wav_bytes, wav_info


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class WhisperSTT:
    def __init__(self) -> None:
        self._model = None
        self._key: tuple | None = None

    def load(self, model_dir: str, threads: int) -> float:
        key = (model_dir, threads)
        if self._model is not None and self._key == key:
            return 0.0
        from faster_whisper import WhisperModel

        start = time.perf_counter()
        self._model = WhisperModel(model_dir, device="cpu", compute_type="int8",
                                   cpu_threads=max(1, int(threads)), num_workers=1,
                                   local_files_only=True)
        self._key = key
        return time.perf_counter() - start

    def unload(self) -> None:
        self._model = None
        self._key = None

    def _run(self, samples, language: str, beam_size: int, vad: bool):
        segments, info = self._model.transcribe(
            samples, language=language or None, beam_size=max(1, int(beam_size)),
            vad_filter=bool(vad), condition_on_previous_text=False,
            # Bounded fallback: the default 6-temperature ladder turned one
            # greedy Bengali decode into a 69 s hallucination loop.
            temperature=(0.0, 0.4), max_new_tokens=224,
        )
        items = []
        for seg in segments:  # the generator does the actual decoding
            items.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip(),
                          "avg_logprob": round(seg.avg_logprob, 3),
                          "no_speech_prob": round(seg.no_speech_prob, 3)})
        return items, info

    def transcribe(self, audio: bytes, content_type: str, *, model_dir: str, threads: int,
                   language: str = "", beam_size: int = 1, vad: bool = True,
                   candidates: list[str] | None = None) -> dict:
        timings = {"load": round(self.load(model_dir, threads), 3)}
        start = time.perf_counter()
        samples = decode_16k(audio, content_type)
        timings["decode"] = round(time.perf_counter() - start, 3)
        duration = float(samples.shape[0]) / TARGET_RATE
        if duration < 0.1:
            return {"text": "", "language": language or "", "language_probability": 0.0,
                    "confidence": 0.0, "duration": round(duration, 3), "segments": [], "timings": timings}
        start = time.perf_counter()
        allowed = set(candidates or [])
        items, info = self._run(samples, language, beam_size, vad)
        timings["transcribe"] = round(time.perf_counter() - start, 3)
        raw_language = info.language or ""
        lang_out, lang_p = info.language or language or "", float(info.language_probability or 0.0)
        if not language and allowed and lang_out not in allowed:
            # Whisper labels Hindi speech as Urdu (same spoken language, other
            # script) and short Bengali as Hindi often enough that an
            # unconstrained pick prints the wrong script. Keep the best of the
            # user's languages (from the same detection pass) and decode again.
            probs = [(code, p) for code, p in (info.all_language_probs or []) if code in allowed]
            if probs:
                lang_out, lang_p = max(probs, key=lambda item: item[1])
                start = time.perf_counter()
                items, _ = self._run(samples, lang_out, beam_size, vad)
                timings["redecode"] = round(time.perf_counter() - start, 3)
        if items:
            weights = [max(0.01, s["end"] - s["start"]) for s in items]
            mean = sum(s["avg_logprob"] * w for s, w in zip(items, weights)) / sum(weights)
            confidence = round(math.exp(mean), 3)
        else:
            confidence = 0.0
        text = " ".join(s["text"] for s in items).strip()
        script_fixed = False
        if lang_out == "bn":
            fixed = devanagari_to_bengali(text)
            if fixed != text:
                script_fixed = True
                text = fixed
                for s in items:
                    s["text"] = devanagari_to_bengali(s["text"])
        return {
            "text": text,
            "language": lang_out,
            "language_probability": round(lang_p, 3),
            "detected_raw": raw_language,
            "script_fixed": script_fixed,
            "confidence": confidence,
            "duration": round(duration, 3),
            "segments": [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in items],
            "timings": timings,
        }


def devanagari_to_bengali(text: str) -> str:
    """Re-script Devanagari letters as Bengali (same ISCII-derived layout, +0x80).

    whisper-small, even with ``language="bn"``, often writes Bengali speech
    phonetically in Devanagari (measured: 8 of 8 round-trip samples). The two
    Unicode blocks are laid out in parallel, so a letter-for-letter shift
    gives readable Bengali; the few Devanagari letters with no Bengali
    code point map to the letter Bengali uses for that sound.
    """
    special = {0x0935: 0x09AC, 0x0933: 0x09B2, 0x0931: 0x09B0, 0x0934: 0x09B2, 0x0929: 0x09A8,
               0x0964: 0x0964, 0x0965: 0x0965}
    if not any(0x0900 <= ord(ch) <= 0x097F for ch in text):
        return text
    out = []
    for ch in text:
        point = ord(ch)
        if point in special:
            out.append(chr(special[point]))
        elif 0x0900 <= point <= 0x097F:
            out.append(chr(point + 0x80))
        else:
            out.append(ch)
    return "".join(out)


class PiperTTS:
    # Four, not three: en/hi/bn/de in turn with three slots evicted on every
    # call and paid the 2-4 s voice load each time (measured).
    MAX_LOADED = 4

    def __init__(self, scratch_dir: str = "") -> None:
        self._voices: OrderedDict[tuple, object] = OrderedDict()
        self._scratch = scratch_dir

    def _load(self, model: str, config: str, threads: int):
        key = (model, threads)
        if key in self._voices:
            self._voices.move_to_end(key)
            return self._voices[key], 0.0
        import onnxruntime
        from piper import PiperVoice
        from piper.config import PiperConfig

        start = time.perf_counter()
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = max(1, int(threads))
        options.inter_op_num_threads = 1
        config_dict = json.loads(Path(config).read_text(encoding="utf-8"))
        voice = PiperVoice(
            config=PiperConfig.from_dict(config_dict),
            session=onnxruntime.InferenceSession(model, sess_options=options,
                                                 providers=["CPUExecutionProvider"]),
            download_dir=Path(self._scratch or Path(model).parent),
        )
        self._voices[key] = voice
        while len(self._voices) > self.MAX_LOADED:
            self._voices.popitem(last=False)
        return voice, time.perf_counter() - start

    def unload(self) -> None:
        self._voices.clear()

    def speak(self, text: str, *, model: str, config: str, threads: int, speaker: int | None = None,
              rate: float = 1.0) -> tuple[bytes, dict]:
        from piper import SynthesisConfig

        voice, load_s = self._load(model, config, threads)
        start = time.perf_counter()
        syn = SynthesisConfig(speaker_id=speaker, length_scale=(1.0 / rate) if rate > 0 else None)
        pcm = bytearray()
        sample_rate = voice.config.sample_rate
        for chunk in voice.synthesize(text, syn_config=syn):
            sample_rate = chunk.sample_rate
            pcm += chunk.audio_int16_bytes
        synth_s = time.perf_counter() - start
        duration = len(pcm) / 2 / sample_rate if sample_rate else 0.0
        return wav_bytes(bytes(pcm), sample_rate), {
            "rate": sample_rate, "duration": round(duration, 3),
            "timings": {"load": round(load_s, 3), "synthesize": round(synth_s, 3)},
        }


class FakeSTT:
    def unload(self) -> None:
        pass

    def transcribe(self, audio: bytes, content_type: str, *, language: str = "", **_ignored) -> dict:
        duration = wav_info(audio).duration if audio[:4] == b"RIFF" else 0.0
        return {"text": "fake transcript", "language": language or "en", "language_probability": 1.0,
                "confidence": 1.0, "duration": round(duration, 3),
                "segments": [{"start": 0.0, "end": round(duration, 2), "text": "fake transcript"}],
                "timings": {"load": 0.0, "decode": 0.0, "transcribe": 0.0}}


class FakeTTS:
    def unload(self) -> None:
        pass

    def speak(self, text: str, **_ignored) -> tuple[bytes, dict]:
        rate = 16000
        frames = int(rate * min(2.0, 0.05 * max(1, len(text))))
        pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(frames))
        return wav_bytes(pcm, rate), {"rate": rate, "duration": round(frames / rate, 3),
                                       "timings": {"load": 0.0, "synthesize": 0.0}}
