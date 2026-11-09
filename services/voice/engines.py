"""The heavy engines. Imported ONLY inside the voice worker process.

* ``WhisperSTT`` - faster-whisper (CTranslate2): CUDA int8_float16 when the
  GPU has room, else CPU int8 with ``cpu_threads`` bounded (default 2);
  language auto-detection, silence/hallucination guards, context prompt.
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


# Decoding options shared by the product and the eval harness
# (``python -m services.voice.eval``); the values were chosen with it, but
# no results file is kept in the repo (docs/voice/README.md "Not measured").
DECODE = {
    # One utterance at a time: carrying text across 30 s windows lets one
    # hallucinated window seed the next one, and dictation rarely spans two.
    "condition_on_previous_text": False,
    # Whisper's own guards: a window whose text compresses too well (a loop),
    # whose mean log-prob is too low, or that the model itself calls silence is
    # re-decoded at a higher temperature or dropped.
    "compression_ratio_threshold": 2.4,
    "log_prob_threshold": -1.0,
    "no_speech_threshold": 0.6,
    "max_new_tokens": 224,
    "word_timestamps": False,
}
# The fallback ladder per device. On CPU every step re-decodes the window
# (4-6 s each with `small`), and the full 6-step ladder turned one Bengali clip
# into a 69 s loop (BUGS-FIXED #5); on the GPU a step costs ~0.2 s.
TEMPERATURES = {"cpu": (0.0, 0.4), "cuda": (0.0, 0.2, 0.4, 0.6, 0.8)}
# Silero VAD (inside faster-whisper). speech_pad keeps soft word onsets and
# endings; 600 ms of silence splits phrases without chopping dictation pauses.
VAD = {"threshold": 0.5, "min_speech_duration_ms": 250, "min_silence_duration_ms": 600,
       "speech_pad_ms": 300}
# Whisper's well-known silence hallucinations (YouTube subtitle credits). A
# segment that is only one of these is dropped when the audio holds little
# speech or the model itself was unsure.
HALLUCINATIONS = frozenset({
    "thank you", "thank you for watching", "thanks for watching", "thank you so much for watching",
    "please subscribe", "subscribe to my channel", "like and subscribe", "you", "bye", "bye bye",
    "i ll see you next time", "see you next time", "see you in the next video", "thank you very much",
    "subtitles by the amara org community", "transcribed by", "subtitles by", "amara org",
    "vielen dank", "danke fürs zuschauen", "untertitel im auftrag des zdf", "untertitel der amara org community",
    "धन्यवाद", "शुक्रिया", "ধন্যবাদ", "music", "applause", "silence",
})
SILENT_RMS = 0.0003   # below this the clip is digital silence: no decode at all


def _norm_phrase(text: str) -> str:
    import re

    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def _is_cuda_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(word in text for word in ("cuda", "cublas", "cudnn", "out of memory", "gpu", "nvrtc"))


class WhisperSTT:
    """faster-whisper on the GPU when it fits, else on the CPU.

    ``device="auto"``: CUDA if CTranslate2 sees a device, cuBLAS 12 loads and
    NVML reports enough free VRAM for the model plus headroom (the FISH engine
    has priority over the card); else CPU int8. A CUDA failure at load or
    decode time falls back to CPU for the rest of the worker's life and is
    reported (``device_reason``).
    """

    def __init__(self) -> None:
        self._model = None
        self._key: tuple | None = None
        self.device = ""
        self.compute_type = ""
        self.device_reason = ""
        self._cuda_broken = ""
        self.vram_headroom_mb = 0   # resource governor's VRAM headroom, set per request

    # ---------------------------------------------------------- device

    def cuda_status(self) -> tuple[bool, str]:
        if self._cuda_broken:
            return False, f"CUDA failed earlier in this worker: {self._cuda_broken}"
        try:
            import ctranslate2

            count = ctranslate2.get_cuda_device_count()
        except Exception as exc:  # noqa: BLE001
            return False, f"ctranslate2 unavailable: {exc}"
        if count <= 0:
            return False, "no CUDA device (CPU only)"
        from . import gpu

        ok, why = gpu.cublas_loadable()
        return (True, "") if ok else (False, why)

    def probe(self) -> dict:
        from . import gpu

        ok, why = self.cuda_status()
        mem = gpu.memory() if ok else {}
        return {"cuda": ok, "reason": why, **mem,
                "loaded": {"device": self.device, "model": Path(self._key[0]).name if self._key else ""}}

    def _choose(self, model_name: str, device: str, compute_type: str) -> tuple[str, str, str]:
        device = (device or "auto").lower()
        if device == "cpu":
            return "cpu", "int8", "cpu requested (voice.device)"
        ok, why = self.cuda_status()
        if not ok:
            return "cpu", "int8", why
        if self._model is not None and self.device == "cuda" and self._key and Path(self._key[0]).name == model_name:
            return "cuda", self.compute_type, ""
        from . import gpu

        mem = gpu.memory()
        free = mem.get("free_mb") if mem else None
        if self._model is not None and self.device == "cuda" and free is not None:
            free += gpu.need_mb(Path(self._key[0]).name)   # the model we would replace
        room = max(gpu.HEADROOM_MB, int(self.vram_headroom_mb or 0))
        if device == "auto" and not gpu.fits(model_name, free, self.vram_headroom_mb):
            return "cpu", "int8", (f"GPU has {free} MB free; {model_name} needs ~{gpu.need_mb(model_name)} MB "
                                   f"+ {room} MB headroom (the FISH engine keeps the card)")
        if compute_type and compute_type != "auto":
            return "cuda", compute_type, ""
        try:
            import ctranslate2

            supported = ctranslate2.get_supported_compute_types("cuda")
        except Exception:  # noqa: BLE001
            supported = {"float16"}
        return "cuda", ("int8_float16" if "int8_float16" in supported else "float16"), ""

    def load(self, model_dir: str, threads: int, device: str = "cpu", compute_type: str = "auto",
             cpu_model_dir: str = "") -> float:
        name = Path(model_dir).name
        chosen, ctype, reason = self._choose(name, device, compute_type)
        if chosen == "cpu" and cpu_model_dir:
            # The GPU model is too slow for the CPU (large-v3-turbo: 23-42 s
            # per utterance): the CPU path has its own model.
            model_dir = cpu_model_dir
        key = (model_dir, threads, chosen, ctype)
        if self._model is not None and self._key == key:
            return 0.0
        from faster_whisper import WhisperModel

        self.unload()
        start = time.perf_counter()
        if chosen == "cuda":
            from . import gpu

            gpu.prepare_cuda_path()
            try:
                self._model = WhisperModel(model_dir, device="cuda", compute_type=ctype, num_workers=1,
                                           local_files_only=True)
            except Exception as exc:  # noqa: BLE001 - any CUDA failure: CPU
                self._cuda_broken = f"{type(exc).__name__}: {exc}"[:300]
                chosen, ctype, reason = "cpu", "int8", f"CUDA load failed: {self._cuda_broken}"
                model_dir = cpu_model_dir or model_dir
        if chosen == "cpu":
            self._model = WhisperModel(model_dir, device="cpu", compute_type="int8",
                                       cpu_threads=max(1, int(threads)), num_workers=1, local_files_only=True)
        self._key = (model_dir, threads, chosen, ctype)
        self.device, self.compute_type, self.device_reason = chosen, ctype, reason
        return time.perf_counter() - start

    @property
    def model_name(self) -> str:
        return Path(self._key[0]).name if self._key else ""

    def unload(self) -> None:
        had = self._model is not None
        self._model = None
        self._key = None
        self.device = ""
        self.compute_type = ""
        if had:
            import gc

            gc.collect()

    def release_gpu(self) -> bool:
        """Drop a CUDA model (frees all of its VRAM, measured); CPU models stay."""
        if self._model is not None and self.device == "cuda":
            self.unload()
            return True
        return False

    # ---------------------------------------------------------- decode

    def _run(self, samples, language: str, beam_size: int, vad: bool, prompt: str = ""):
        segments, info = self._model.transcribe(
            samples, language=language or None, beam_size=max(1, int(beam_size)),
            vad_filter=bool(vad), vad_parameters=dict(VAD) if vad and VAD else None,
            temperature=TEMPERATURES.get(self.device, TEMPERATURES["cpu"]),
            initial_prompt=prompt or None, **DECODE,
        )
        items = []
        for seg in segments:  # the generator does the actual decoding
            items.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip(),
                          "avg_logprob": round(seg.avg_logprob, 3),
                          "no_speech_prob": round(seg.no_speech_prob, 3),
                          "compression_ratio": round(seg.compression_ratio, 2)})
        return items, info

    def _run_safe(self, samples, language, beam_size, vad, prompt, reload: tuple):
        try:
            return self._run(samples, language, beam_size, vad, prompt)
        except Exception as exc:  # noqa: BLE001
            if self.device != "cuda" or not _is_cuda_error(exc):
                raise
            # A CUDA error mid-decode (OOM when the FISH engine grew, a driver
            # reset): CPU for the rest of this worker, same request retried.
            self._cuda_broken = f"{type(exc).__name__}: {exc}"[:300]
            model_dir, threads, cpu_model_dir = reload
            self.unload()
            self.load(cpu_model_dir or model_dir, threads, "cpu")
            self.device_reason = f"CUDA decode failed, retried on CPU: {self._cuda_broken}"
            return self._run(samples, language, beam_size, vad, prompt)

    @staticmethod
    def drop_hallucinations(items: list[dict], speech_seconds: float | None) -> tuple[list[dict], int]:
        kept, dropped = [], 0
        for seg in items:
            phrase = _norm_phrase(seg["text"])
            unsure = seg.get("no_speech_prob", 0.0) > 0.3 or seg.get("avg_logprob", 0.0) < -0.6
            little_speech = speech_seconds is not None and speech_seconds < 1.0
            if not phrase or (phrase in HALLUCINATIONS and (unsure or little_speech)):
                dropped += 1
                continue
            if seg.get("no_speech_prob", 0.0) > 0.8 and seg.get("avg_logprob", 0.0) < -0.8:
                dropped += 1
                continue
            kept.append(seg)
        return kept, dropped

    def transcribe(self, audio: bytes, content_type: str, *, model_dir: str, threads: int,
                   language: str = "", beam_size: int = 5, vad: bool = True,
                   candidates: list[str] | None = None, device: str = "cpu", compute_type: str = "auto",
                   prompt: str = "", offset: float = 0.0, samples=None, cpu_model_dir: str = "",
                   vram_headroom_mb: int = 0) -> dict:
        self.vram_headroom_mb = int(vram_headroom_mb or 0)
        timings = {"load": round(self.load(model_dir, threads, device, compute_type, cpu_model_dir), 3)}
        start = time.perf_counter()
        if samples is None:
            samples = decode_16k(audio, content_type)
        if offset > 0:
            samples = samples[int(offset * TARGET_RATE):]
        timings["decode"] = round(time.perf_counter() - start, 3)
        duration = float(samples.shape[0]) / TARGET_RATE
        base = {"model": self.model_name, "device": self.device, "compute_type": self.compute_type, "device_reason": self.device_reason,
                "duration": round(duration, 3), "timings": timings}
        empty = {"text": "", "language": language or "", "language_probability": 0.0, "detected_raw": "",
                 "script_fixed": False, "confidence": 0.0, "segments": [], "dropped": 0, "speech_seconds": 0.0}
        if duration < 0.1:
            return {**empty, **base}
        import numpy as np

        if float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)))) < SILENT_RMS:
            return {**empty, **base, "silent": True}
        start = time.perf_counter()
        allowed = set(candidates or [])
        reload = (model_dir, threads, cpu_model_dir)
        items, info = self._run_safe(samples, language, beam_size, vad, prompt, reload)
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
                items, _ = self._run_safe(samples, lang_out, beam_size, vad, prompt, reload)
                timings["redecode"] = round(time.perf_counter() - start, 3)
        speech = getattr(info, "duration_after_vad", None)
        items, dropped = self.drop_hallucinations(items, speech if vad else None)
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
        base.update(model=self.model_name, device=self.device, compute_type=self.compute_type,
                    device_reason=self.device_reason)
        return {
            **base,
            "text": text,
            "language": lang_out,
            "language_probability": round(lang_p, 3),
            "detected_raw": raw_language,
            "script_fixed": script_fixed,
            "confidence": confidence,
            "dropped": dropped,
            "speech_seconds": round(float(speech), 3) if speech is not None else None,
            "segments": [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in items],
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

    def probe(self) -> dict:
        return {"cuda": False, "reason": "fake engines (IMURUN_VOICE_FAKE=1)"}

    def release_gpu(self) -> bool:
        return False

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
