"""VoiceService: the one object the routes talk to.

It decides, it never imports a heavy engine: availability is checked with
``importlib.util.find_spec``, downloads run here (so progress reaches the
event bus), and every transcription / Piper synthesis is sent to the voice
worker process (``worker.py``). System voices (SAPI / say / espeak-ng) and
the opt-in online engine run as short child processes / calls from here.

Errors are ``VoiceUnavailable`` (-> HTTP 503 with a reason) or
``VoiceInputError`` (-> 400/415). Nothing raises a traceback to a client.
"""
from __future__ import annotations

import importlib.util
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import cleanup as cleanup_mod
from . import download, online
from .audio import AudioError, ffmpeg_path, sniff, wav_info
from .catalogue import DEFAULT_VOICE, Catalogue
from .languages import CORE, LANGUAGES, WHISPER_LANGUAGES, name, native_name, normalize, voice_language
from .paths import VoicePaths
from .system import SystemTTS
from .stream import StreamManager
from .text import MAX_TEXT, speakable
from .vocabulary import build_prompt
from .worker import WorkerClient, WorkerError

LOG = logging.getLogger("imurun.voice")


def stream_commit_after() -> float:
    from . import stream

    return stream.COMMIT_AFTER_S


def stream_commit_guard() -> float:
    from . import stream

    return stream.COMMIT_GUARD_S


STT_MODELS = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")
MAX_AUDIO_BYTES = 32 * 1024 * 1024
MAX_AUDIO_SECONDS = 300

# The settings section (merged over whatever is stored). The first four keys
# are the shell's existing browser-voice settings and keep their meaning.
DEFAULTS = {
    "lang": "", "voiceURI": "", "rate": 1, "sounds": True,
    "engine": "auto",               # shell: auto (server when available) | server | browser
    "sttEngine": "faster-whisper",  # faster-whisper | off
    "sttModel": "small",            # the CPU model: tiny | base | small | medium | large-v3 | large-v3-turbo
    "sttModelGpu": "large-v3-turbo",  # the model when the GPU has room ("" = same as sttModel)
    "sttModelByLanguage": {},       # per language override: {"bn": "large-v3-turbo"} (REQUESTS F1)
    "sttBeamSize": 5,               # 1 = greedy: faster, but looped on Bengali in tests
    "device": "auto",               # auto (CUDA when it fits, else CPU) | cuda | cpu
    "gpuIdleSeconds": 120,          # drop the GPU model after this idle time (VRAM back to FISH); 0 = keep
    "vocabulary": [],               # the user's words for context biasing ("Phi-3", "Rahul", ...)
    "corrections": [],              # [{"heard": "five 3", "fixed": "Phi-3"}] from the "fix this" button
    "contextBiasing": True,         # glossary prompt: corrections + vocabulary + conversation + product names
    "cleanup": True,                # deterministic dictation clean-up (fillers, self-corrections, punctuation)
    "cleanupModel": False,          # optional FISH pass over the cleaned text (only when loaded and idle)
    "cleanupModelMs": 1000,         # its budget
    "streamEndpointSeconds": 1.2,   # trailing silence that marks the end of a streamed utterance
    "ttsEngine": "auto",            # auto | piper | system | online
    "voices": {},                   # per language: {"hi": "piper:hi_IN-rohan-medium", ...}
    "autoDetectLanguage": True,
    "languages": list(CORE),        # candidates for auto-detection
    "replyInSameLanguage": True,
    "speakReplies": False,
    "inputDevice": "",              # MediaDeviceInfo.deviceId chosen in the shell ("" = default)
    "onlineFallback": False,        # edge-tts: SENDS TEXT TO MICROSOFT. Never on by default.
    "autoInstall": True,            # download the model / voice on first use
    "threads": 2,
    "idleUnloadMinutes": 10,
}


class VoiceUnavailable(RuntimeError):
    def __init__(self, error: str, message: str, **extra) -> None:
        super().__init__(message)
        self.error = error
        self.message = message
        self.extra = extra

    def payload(self) -> dict:
        return {"error": self.error, "reason": self.message, "message": self.message, **self.extra}


class VoiceInputError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def has_module(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


@dataclass
class Speech:
    audio: bytes
    content_type: str
    engine: str
    voice: str
    language: str
    duration: float = 0.0
    fallback: str = ""
    timings: dict = field(default_factory=dict)

    def headers(self) -> dict:
        out = {"X-Voice-Engine": self.engine, "X-Voice-Id": self.voice, "X-Voice-Language": self.language,
               "X-Voice-Offline": "false" if self.engine == "online" else "true"}
        if self.duration:
            out["X-Voice-Duration"] = f"{self.duration:.3f}"
        if self.fallback:
            out["X-Voice-Fallback"] = self.fallback[:200]
        # HTTP header values are latin-1: keep them ASCII so a voice or path
        # name with other characters can never break the response.
        return {k: str(v).encode("ascii", "replace").decode("ascii") for k, v in out.items()}


class VoiceService:
    def __init__(self, *, data_root: Path | str | None = None, settings=None,
                 publish: Callable[[str, dict], object] | None = None, worker=None,
                 system: SystemTTS | None = None, python: str | None = None) -> None:
        self.paths = VoicePaths.at(data_root)
        self._settings = settings
        self._publish = publish
        self.catalogue = Catalogue(self.paths)
        self.system = system or SystemTTS(self.paths.tmp)
        cfg = self.config()
        self.worker = worker or WorkerClient(cwd=self.paths.tmp, threads=int(cfg["threads"]),
                                             idle_seconds=float(cfg["idleUnloadMinutes"]) * 60.0, python=python)
        self.vram_headroom: Callable[[], int] | None = None   # bytes; set by routes._govern
        self._jobs: dict[str, download.Job] = {}
        self._jobs_lock = threading.Lock()
        self._model_getter: Callable[[], object] | None = None   # the FISH model, for the clean-up pass
        self.streams = StreamManager(self._stream_pass, self._emit)

    # ------------------------------------------------------------ config

    def config(self) -> dict:
        values = dict(DEFAULTS)
        if self._settings is not None:
            try:
                stored = self._settings.read("voice")
                if isinstance(stored, dict):
                    values.update(stored)
            except Exception:  # noqa: BLE001 - a corrupt section falls back to defaults
                pass
        values.update(getattr(self, "overrides", {}) or {})  # CLI / tests only
        try:
            values["threads"] = max(1, min(16, int(values.get("threads") or 2)))
        except (TypeError, ValueError):
            values["threads"] = 2
        if values.get("sttModel") not in STT_MODELS:
            values["sttModel"] = "small"
        if values.get("sttModelGpu") not in STT_MODELS:
            values["sttModelGpu"] = values["sttModel"]
        if str(values.get("device") or "auto").lower() not in ("auto", "cuda", "cpu"):
            values["device"] = "auto"
        values["device"] = str(values.get("device") or "auto").lower()
        values["vocabulary"] = [str(v).strip()[:60] for v in (values.get("vocabulary") or [])
                                if isinstance(v, (str, int, float)) and str(v).strip()][:500] \
            if isinstance(values.get("vocabulary"), list) else []
        values["corrections"] = [{"heard": str(c.get("heard") or "")[:120], "fixed": str(c.get("fixed") or "")[:120]}
                                 for c in (values.get("corrections") or []) if isinstance(c, dict)
                                 and str(c.get("heard") or "").strip() and str(c.get("fixed") or "").strip()][-200:] \
            if isinstance(values.get("corrections"), list) else []
        for key, low, high, fallback in (("gpuIdleSeconds", 0, 86400, 120), ("cleanupModelMs", 100, 10000, 1000),
                                         ("streamEndpointSeconds", 0.3, 10.0, 1.2)):
            try:
                values[key] = max(low, min(high, float(values.get(key))))
            except (TypeError, ValueError):
                values[key] = fallback
        by_lang = values.get("sttModelByLanguage")
        values["sttModelByLanguage"] = {
            normalize(k): str(v) for k, v in (by_lang.items() if isinstance(by_lang, dict) else ())
            if normalize(k) and str(v) in STT_MODELS and str(v) != values["sttModel"]}
        if not isinstance(values.get("voices"), dict):
            values["voices"] = {}
        if not isinstance(values.get("languages"), list):
            values["languages"] = list(CORE)
        # Applying the worker limits here means a settings change takes
        # effect on the next request without a restart.
        worker = getattr(self, "worker", None)
        if worker is not None and hasattr(worker, "threads"):
            if worker.threads != values["threads"]:
                worker.threads = values["threads"]
                worker.unload()
            try:
                worker.idle_seconds = float(values.get("idleUnloadMinutes") or 0) * 60.0
            except (TypeError, ValueError):
                pass
            if hasattr(worker, "gpu_idle_seconds"):
                worker.gpu_idle_seconds = float(values["gpuIdleSeconds"])
        return values

    def _emit(self, event: str, data: dict) -> None:
        if self._publish is None:
            return
        try:
            self._publish(event, data)
        except Exception:  # noqa: BLE001 - the event bus must not break a request
            pass

    # ------------------------------------------------------------ availability

    @staticmethod
    def packages() -> dict:
        return {name_: has_module(name_) for name_ in
                ("faster_whisper", "ctranslate2", "av", "numpy", "piper", "onnxruntime", "edge_tts")}

    def stt_reason(self, cfg: dict | None = None) -> str:
        cfg = cfg or self.config()
        if str(cfg.get("sttEngine")).lower() in ("off", "none", ""):
            return "speech recognition is turned off in settings (voice.sttEngine)"
        if not has_module("faster_whisper"):
            return "faster-whisper is not installed (pip install -r requirements-voice.txt)"
        return ""

    def tts_piper_reason(self) -> str:
        if not (has_module("piper") and has_module("onnxruntime")):
            return "piper-tts is not installed (pip install -r requirements-voice.txt)"
        return ""

    def whisper_installed(self, size: str) -> bool:
        directory = self.paths.whisper_dir(size)
        return all((directory / item).is_file() for item in download.WHISPER_REQUIRED)

    def summary(self) -> str:
        """One line for /api/status: "ready", "partial (...)" or "unavailable (...)"."""
        cfg = self.config()
        stt = self.stt_reason(cfg)
        tts_ok = not self.tts_piper_reason() or self.system.available
        if not stt and tts_ok:
            return "ready"
        if stt and not tts_ok:
            return f"unavailable ({stt}; no TTS engine)"
        return f"partial ({stt or 'no TTS engine'})"

    def status(self) -> dict:
        cfg = self.config()
        stt_reason = self.stt_reason(cfg)
        size = cfg["sttModel"]
        piper_reason = self.tts_piper_reason()
        installed_voices = [key for key in self.catalogue.voices(allow_network=False) if self.catalogue.installed(key)]
        voices = []
        for key in installed_voices:
            entry = self.catalogue.get(key) or {}
            lang = (entry.get("language") or {}).get("family", "")
            voices.append({"id": f"piper:{key}", "name": entry.get("name", key), "lang": lang,
                           "locale": ((entry.get("language") or {}).get("code") or "").replace("_", "-"),
                           "engine": "piper", "quality": entry.get("quality", ""), "offline": True})
        for item in self.system.voices():
            voices.append({"id": f"system:{item.id}", "name": item.name, "lang": item.language,
                           "locale": item.locale, "engine": "system", "quality": "system", "offline": True})
        tts_engines = {
            "piper": {"available": not piper_reason, "reason": piper_reason, "installed_voices": len(installed_voices)},
            "system": {"available": self.system.available, "backend": self.system.backend,
                       "reason": self.system.reason(), "voices": len(self.system.voices())},
            "online": {"available": bool(cfg.get("onlineFallback")) and online.installed(),
                       "enabled": bool(cfg.get("onlineFallback")), "installed": online.installed(),
                       "privacy": online.PRIVACY_NOTE},
        }
        tts_available = (not piper_reason and bool(installed_voices)) or self.system.available or \
            tts_engines["online"]["available"]
        return {
            "summary": self.summary(),
            "stt": {
                "available": not stt_reason, "reason": stt_reason, "engine": f"faster-whisper-{size}",
                "model": size, "model_installed": self.whisper_installed(size),
                "auto_install": bool(cfg.get("autoInstall")),
                "languages": sorted(WHISPER_LANGUAGES), "auto_detect": bool(cfg.get("autoDetectLanguage")),
                "candidates": [normalize(x) for x in cfg.get("languages") or [] if normalize(x)],
                "formats": self.formats(),
            },
            "tts": {"available": tts_available, "engine": self._tts_engine_label(cfg, piper_reason),
                    "engines": tts_engines, "voices": voices, "format": "audio/wav"},
            "worker": self.worker.status() if hasattr(self.worker, "status") else {},
            "gpu": self.gpu_info(),
            "streams": self.streams.views(),
            "jobs": self.jobs(),
            "settings": {k: cfg[k] for k in ("replyInSameLanguage", "speakReplies", "autoDetectLanguage",
                                              "onlineFallback", "ttsEngine", "sttModel", "sttModelGpu",
                                              "sttModelByLanguage", "threads", "device", "gpuIdleSeconds",
                                              "idleUnloadMinutes", "inputDevice", "engine", "cleanup",
                                              "cleanupModel", "contextBiasing")},
            "vocabulary": {"words": len(cfg["vocabulary"]), "corrections": len(cfg["corrections"])},
            "paths": {"root": str(self.paths.root)},
        }

    @staticmethod
    def formats() -> dict:
        pyav = has_module("av")
        ffmpeg = bool(ffmpeg_path())
        compressed = pyav or ffmpeg
        return {"wav": True, "webm": compressed, "ogg": compressed, "mp3": compressed, "flac": compressed,
                "mp4": compressed, "decoder": "pyav" if pyav else ("ffmpeg" if ffmpeg else "wav-only")}

    @staticmethod
    def _tts_engine_label(cfg: dict, piper_reason: str) -> str:
        choice = str(cfg.get("ttsEngine") or "auto")
        if choice != "auto":
            return choice
        return "piper" if not piper_reason else "system"

    # ------------------------------------------------------------ installs

    def jobs(self) -> list[dict]:
        with self._jobs_lock:
            return [job.view() for job in self._jobs.values()]

    def _active_job(self, kind: str, name_: str) -> download.Job | None:
        with self._jobs_lock:
            for job in self._jobs.values():
                if job.kind == kind and job.name == name_ and job.state in ("queued", "downloading"):
                    return job
        return None

    def install(self, kind: str, name_: str, *, wait: bool = False) -> download.Job:
        kind = (kind or "").lower()
        if kind in ("whisper", "stt"):
            kind = "whisper"
            if name_ not in STT_MODELS:
                raise VoiceInputError(f"unknown whisper model {name_!r}; one of {', '.join(STT_MODELS)}")
        elif kind in ("piper", "tts"):
            kind = "piper"
            if self.catalogue.get(name_) is None:
                self.catalogue.voices()  # maybe the full catalogue knows it
                if self.catalogue.get(name_) is None:
                    raise VoiceInputError(f"unknown Piper voice {name_!r}")
        else:
            raise VoiceInputError("engine must be 'whisper' or 'piper'")
        existing = self._active_job(kind, name_)
        if existing is not None:
            job = existing
        else:
            job = download.Job(id=uuid.uuid4().hex[:12], kind=kind, name=name_)
            with self._jobs_lock:
                self._jobs[job.id] = job
                # keep the list short
                for old in [j for j in self._jobs.values() if j.state in ("done", "error")][:-10]:
                    self._jobs.pop(old.id, None)
            thread = threading.Thread(target=self._run_job, args=(job,), daemon=True, name=f"voice-dl-{name_}")
            thread.start()
            job._thread = thread  # type: ignore[attr-defined]
        if wait:
            thread = getattr(job, "_thread", None)
            if thread is not None:
                thread.join()
        return job

    def _run_job(self, job: download.Job) -> None:
        publish = lambda view: self._emit("voice.model.download", view)  # noqa: E731
        try:
            if job.kind == "whisper":
                files = self._whisper_files(job.name)
            else:
                files = self.catalogue.files(job.name)
            download.download_files(job, files, publish)
        except Exception as exc:  # noqa: BLE001 - becomes job state + event
            job.state, job.error = "error", str(exc)
            LOG.warning("voice download %s/%s failed: %s", job.kind, job.name, exc)
            publish(job.view())

    def _whisper_files(self, size: str) -> list[tuple[str, Path, int]]:
        repo = download.WHISPER_REPOS[size]
        target = self.paths.whisper_dir(size)
        try:
            info = download.fetch_json(f"{download.HF}/api/models/{repo}?blobs=true", timeout=30.0)
            siblings = {item["rfilename"]: int(item.get("size") or 0) for item in info.get("siblings", [])}
        except Exception:  # noqa: BLE001 - API down: try the known file names
            siblings = {name_: 0 for name_ in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")}
        files = [(download.hf_url(repo, rel), target / rel, size_) for rel, size_ in siblings.items()
                 if rel in download.WHISPER_FILES]
        if not any(rel == "model.bin" for rel in siblings):
            raise download.DownloadError(f"{repo} has no model.bin")
        return files

    # ------------------------------------------------------------ STT

    def gpu_info(self) -> dict:
        """What the worker reported about CUDA when it started ({} before)."""
        info = getattr(self.worker, "info", None)
        cuda = info.get("cuda") if isinstance(info, dict) else None
        return dict(cuda) if isinstance(cuda, dict) else {}

    def _plan(self, cfg: dict, language: str) -> dict:
        """Which model(s) and device to ask the worker for.

        GPU possible (worker reported CUDA, or not known yet): ``sttModelGpu``
        (default large-v3-turbo) with ``sttModel`` as the CPU fallback the
        worker switches to when the card is full. CPU only: ``sttModel``. A
        forced language with an ``sttModelByLanguage`` entry uses that model
        on either device. A missing GPU model is downloaded (when the worker
        confirmed CUDA) while the CPU model serves - on the GPU if it fits.
        """
        by_lang = cfg.get("sttModelByLanguage") or {}
        special = by_lang.get(language) if language else ""
        cpu_model = special or cfg["sttModel"]
        gpu_model = special or cfg.get("sttModelGpu") or cpu_model
        preference = cfg.get("device") or "auto"
        cuda = self.gpu_info().get("cuda")          # True / False / None (worker not started yet)
        if preference == "cpu" or cuda is False:
            return {"model": cpu_model, "cpu_model": "", "device": "cpu", "pending": None}
        device = "cuda" if preference == "cuda" else "auto"
        if gpu_model == cpu_model:
            return {"model": cpu_model, "cpu_model": "", "device": device, "pending": None}
        if self.whisper_installed(gpu_model):
            return {"model": gpu_model, "cpu_model": cpu_model if self.whisper_installed(cpu_model) else "",
                    "device": device, "pending": None}
        pending = None
        if cuda is True and cfg.get("autoInstall"):
            job = self.install("whisper", gpu_model)
            pending = {"model": gpu_model, "error": "model_downloading", "job": job.view(),
                       "message": f"the GPU speech model '{gpu_model}' is downloading; '{cpu_model}' is used meanwhile"}
        return {"model": cpu_model, "cpu_model": "", "device": device, "pending": pending}

    def _vram_headroom_mb(self) -> int:
        """The resource governor's VRAM headroom for this request (0 = no governor)."""
        try:
            return int((self.vram_headroom() if self.vram_headroom else 0) // (1024 * 1024))
        except Exception:  # noqa: BLE001 - advisory
            return 0

    def prompt_for(self, cfg: dict, language: str, context=None) -> str:
        if not cfg.get("contextBiasing", True):
            return ""
        return build_prompt(language=language, vocabulary=cfg.get("vocabulary"), context=context or [],
                            corrections=cfg.get("corrections"))

    def _validate_audio(self, audio: bytes, content_type: str, *, limit_seconds: bool = True) -> None:
        if not audio:
            raise VoiceInputError("empty audio body")
        if len(audio) > MAX_AUDIO_BYTES:
            raise VoiceInputError(f"audio larger than {MAX_AUDIO_BYTES // (1024 * 1024)} MB", 413)
        kind = sniff(audio, content_type)
        if not kind:
            raise VoiceInputError("unrecognised audio format (send WAV, WebM/Opus, OGG, MP3, FLAC or M4A)", 415)
        if kind != "wav" and not self.formats()[kind]:
            raise VoiceUnavailable("decoder_unavailable",
                                   f"{kind} audio needs PyAV (pip install av) or ffmpeg on PATH; WAV works without either")
        if kind == "wav" and limit_seconds:
            try:
                if wav_info(audio).duration > MAX_AUDIO_SECONDS:
                    raise VoiceInputError(f"audio longer than {MAX_AUDIO_SECONDS} s", 413)
            except AudioError as exc:
                raise VoiceInputError(str(exc)) from exc

    def transcribe(self, audio: bytes, content_type: str = "", lang: str = "", *, context=None,
                   cleanup: bool | None = None, polish: bool | None = None, offset: float = 0.0,
                   language_lock: str = "") -> dict:
        cfg = self.config()
        reason = self.stt_reason(cfg)
        if reason:
            raise VoiceUnavailable("stt_unavailable", reason)
        self._validate_audio(audio, content_type)
        hint = normalize(lang) or normalize(cfg.get("lang"))
        auto = bool(cfg.get("autoDetectLanguage")) and not language_lock
        language = language_lock or ("" if auto else hint)
        by_lang = cfg.get("sttModelByLanguage") or {}
        plan = self._plan(cfg, language)
        size = plan["model"]
        self._ensure_whisper(size, cfg)
        candidates = [normalize(x) for x in cfg.get("languages") or [] if normalize(x) in WHISPER_LANGUAGES]
        if auto and hint and hint in WHISPER_LANGUAGES and hint not in candidates:
            candidates.append(hint)
        prompt = self.prompt_for(cfg, language, context)
        beam = int(cfg.get("sttBeamSize") or 5)
        started = time.perf_counter()

        def request(model: str, cpu_model: str, device: str, forced: str, cands: list) -> dict:
            try:
                reply_, _ = self.worker.request({
                    "op": "transcribe", "model_dir": str(self.paths.whisper_dir(model)),
                    "cpu_model_dir": str(self.paths.whisper_dir(cpu_model)) if cpu_model else "",
                    "language": forced, "beam_size": beam, "vad": True, "content_type": content_type,
                    "candidates": cands, "device": device, "prompt": prompt, "offset": float(offset or 0.0),
                    "vram_headroom_mb": self._vram_headroom_mb(),
                }, audio)
            except WorkerError as exc:
                if exc.kind == "audio":
                    raise VoiceInputError(str(exc), 415) from exc
                raise VoiceUnavailable("stt_failed", str(exc)) from exc
            reply_.pop("ok", None)
            return reply_

        reply = request(size, plan["cpu_model"], plan["device"], language, candidates if auto else [])
        used = reply.get("model") or size
        code = normalize(reply.get("language"))
        wanted = by_lang.get(code) if auto else ""
        if wanted and wanted != used:
            # The detected language has its own (usually larger) model: run it
            # with the language fixed. A missing model is reported, not fatal.
            try:
                self._ensure_whisper(wanted, cfg)
            except VoiceUnavailable as exc:
                reply["model_pending"] = {"model": wanted, "error": exc.error, "message": str(exc)}
            else:
                try:
                    second = request(wanted, "", plan["device"] if plan["device"] != "cpu" else "cpu", code, [])
                except VoiceUnavailable as exc:
                    reply["model_pending"] = {"model": wanted, "error": "stt_failed", "message": str(exc)}
                else:
                    reply = second
                    used = reply.get("model") or wanted
        if plan.get("pending") and "model_pending" not in reply:
            reply["model_pending"] = plan["pending"]
        reply["model"] = used
        reply["language"] = code
        reply["lang"] = code                      # the shell's field name (VOICE.md)
        reply["language_name"] = name(code)
        reply["duration_seconds"] = reply.get("duration", 0.0)
        reply["engine"] = f"faster-whisper-{used}"
        reply["prompted"] = bool(prompt)
        reply["answer_language"] = code if cfg.get("replyInSameLanguage") else ""
        self._finish_text(reply, cfg, cleanup=cleanup, polish=polish)
        reply["elapsed"] = round(time.perf_counter() - started, 3)
        return reply

    def _finish_text(self, reply: dict, cfg: dict, *, cleanup: bool | None, polish: bool | None) -> None:
        """``text`` = cleaned dictation, ``text_raw`` = what Whisper wrote."""
        raw = str(reply.get("text") or "")
        reply["text_raw"] = raw
        do_clean = cfg.get("cleanup", True) if cleanup is None else bool(cleanup)
        info = {"applied": False, "changes": [], "model": {"used": False, "reason": "off"}}
        text = raw
        if do_clean and raw:
            result = cleanup_mod.clean(raw, reply.get("language") or "en", corrections=cfg.get("corrections"))
            text = result.text
            info.update(applied=True, changes=result.changes)
        do_polish = cfg.get("cleanupModel", False) if polish is None else bool(polish)
        if do_polish and text and do_clean:
            from .polish import polish as run_polish

            model = None
            try:
                model = self._model_getter() if self._model_getter else None
            except Exception:  # noqa: BLE001
                model = None
            outcome = run_polish(model, text, reply.get("language") or "en",
                                 budget_ms=int(cfg.get("cleanupModelMs") or 1000),
                                 vocabulary=[*(c["fixed"] for c in cfg.get("corrections") or []),
                                             *(cfg.get("vocabulary") or [])])
            info["model"] = {k: outcome[k] for k in ("used", "reason", "ms")}
            if outcome["used"]:
                text = outcome["text"]
        reply["text"] = text
        reply["cleanup"] = info

    # ------------------------------------------------------------ streaming

    def stream_chunk(self, sid: str, seq: int, chunk: bytes, content_type: str = "", lang: str = "", *,
                     final: bool = False, context=None) -> dict:
        cfg = self.config()
        reason = self.stt_reason(cfg)
        if reason:
            raise VoiceUnavailable("stt_unavailable", reason)
        sid = "".join(ch for ch in str(sid or "") if ch.isalnum() or ch in "-_")[:64]
        if not sid:
            raise VoiceInputError("stream id required (?id=...)")
        session = self.streams.get(sid)
        if session is None:
            if not chunk:
                raise VoiceInputError("the first chunk of a stream must carry audio")
            if not sniff(chunk, content_type):
                raise VoiceInputError("unrecognised audio format (send WebM/Opus, OGG or WAV chunks)", 415)
            session = self.streams.open(sid, content_type, lang, context)
        if chunk:
            try:
                self.streams.add(session, int(seq), chunk)
            except ValueError as exc:
                raise VoiceInputError(str(exc), 409) from exc
            except OverflowError as exc:
                raise VoiceInputError(str(exc), 413) from exc
        if final:
            result = self.streams.finish(session)
            self._emit("voice.final", {"id": sid, "text": result.get("text", ""),
                                       "text_raw": result.get("text_raw", ""), "language": result.get("language", "")})
            return {**result, "id": sid, "final": True}
        if (session.partial or {}).get("device") != "cpu":
            # On the CPU a pass takes 4-6 s: after the first one shows that,
            # no more partials - they would only delay the final pass.
            self.streams.kick(session)
        partial = dict(session.partial or {})
        return {"id": sid, "seq": session.seq, "accepted": True, "final": False, "partial": partial or None}

    def _stream_pass(self, session, final: bool) -> dict:
        cfg = self.config()
        with session.lock:
            data = bytes(session.data)
            offset = session.committed_offset
            locked = session.language
        reply = self.transcribe(data, session.content_type, session.lang, context=session.context,
                                cleanup=True, polish=None if final else False, offset=offset,
                                language_lock=locked)
        session.passes += 1
        segments = reply.get("segments") or []
        duration = float(reply.get("duration") or 0.0)
        if not locked and duration >= 2.0 and reply.get("text"):
            session.language = normalize(reply.get("language"))
        stable = session.stable_text()
        if final:
            raw = " ".join(t for t in (stable, reply.get("text_raw", "")) if t).strip()
            reply["text_raw"] = raw
            result = cleanup_mod.clean(raw, reply.get("language") or "en", corrections=cfg.get("corrections")) \
                if cfg.get("cleanup", True) else None
            if result is not None:
                reply["text"] = result.text
                reply.setdefault("cleanup", {})["changes"] = result.changes
            else:
                reply["text"] = raw
            reply["stream"] = {"passes": session.passes, "committed_s": round(session.committed_offset, 2)}
            return reply
        if duration > stream_commit_after() and len(segments) > 1:
            cut = duration - stream_commit_guard()
            done = [seg for seg in segments if seg["end"] <= cut]
            if done:
                with session.lock:
                    session.committed_text.append(" ".join(seg["text"] for seg in done).strip())
                    session.committed_offset = offset + float(done[-1]["end"])
                stable = session.stable_text()
        last_end = float(segments[-1]["end"]) if segments else 0.0
        endpoint = bool(segments) and (duration - last_end) >= float(cfg.get("streamEndpointSeconds") or 1.2)
        tail = reply.get("text_raw", "")
        if segments and stable and session.committed_offset > offset:
            tail = " ".join(seg["text"] for seg in segments if offset + seg["end"] > session.committed_offset)
        live = " ".join(t for t in (stable, tail) if t).strip()
        cleaned = cleanup_mod.clean(live, reply.get("language") or "en", corrections=cfg.get("corrections")).text \
            if cfg.get("cleanup", True) else live
        return {"id": session.id, "seq": session.seq, "text": cleaned, "text_raw": live, "stable": stable,
                "language": reply.get("language", ""), "endpoint": endpoint, "audio_s": round(offset + duration, 2),
                "elapsed": reply.get("elapsed"), "device": reply.get("device", ""), "model": reply.get("model", ""),
                "partial": True}

    # ------------------------------------------------------------ vocabulary

    def vocabulary(self) -> dict:
        cfg = self.config()
        from .vocabulary import PRODUCT_TERMS

        return {"vocabulary": cfg["vocabulary"], "corrections": cfg["corrections"],
                "product_terms": list(PRODUCT_TERMS), "context_biasing": bool(cfg.get("contextBiasing", True)),
                "prompt_preview": self.prompt_for(cfg, "")}

    def _write(self, values: dict) -> None:
        if self._settings is None:
            raise VoiceUnavailable("settings_unavailable", "no settings store")
        self._settings.write("voice", values)

    def update_vocabulary(self, add=None, remove=None, replace=None) -> dict:
        cfg = self.config()
        words = list(cfg["vocabulary"]) if replace is None else []
        for item in (replace or []) + list(add or []):
            term = " ".join(str(item or "").split())[:60]
            if term and term.casefold() not in {w.casefold() for w in words}:
                words.append(term)
        drop = {str(x).casefold() for x in (remove or [])}
        words = [w for w in words if w.casefold() not in drop][:500]
        self._write({"vocabulary": words})
        return self.vocabulary()

    def add_correction(self, heard: str, fixed: str) -> dict:
        heard, fixed = " ".join(str(heard or "").split())[:120], " ".join(str(fixed or "").split())[:120]
        if not heard or not fixed:
            raise VoiceInputError("send {heard, fixed}: what was transcribed and what it should be")
        if heard.casefold() == fixed.casefold():
            raise VoiceInputError("heard and fixed are the same")
        cfg = self.config()
        items = [c for c in cfg["corrections"] if c["heard"].casefold() != heard.casefold()]
        items.append({"heard": heard, "fixed": fixed})
        words = list(cfg["vocabulary"])
        # The fixed spelling also biases recognition next time (the prompt).
        for term in [fixed] if len(fixed.split()) <= 4 else []:
            if term.casefold() not in {w.casefold() for w in words}:
                words.append(term)
        self._write({"corrections": items[-200:], "vocabulary": words[:500]})
        return self.vocabulary()

    def remove_correction(self, heard: str) -> dict:
        cfg = self.config()
        items = [c for c in cfg["corrections"] if c["heard"].casefold() != str(heard or "").casefold()]
        self._write({"corrections": items})
        return self.vocabulary()

    def _ensure_whisper(self, size: str, cfg: dict) -> None:
        if self.whisper_installed(size):
            return
        job = None
        if cfg.get("autoInstall"):
            job = self.install("whisper", size)
        raise VoiceUnavailable(
            "model_downloading" if job else "model_not_installed",
            f"the speech model '{size}' (~{download.WHISPER_SIZES_MB.get(size, 0)} MB) is "
            + ("downloading; watch voice.model.download events and retry" if job else
               "not installed; POST /api/voice/voices/install {engine: 'whisper', name: '%s'}" % size),
            job=job.view() if job else None)

    # ------------------------------------------------------------ TTS

    def resolve_voice(self, lang: str, voice: str, cfg: dict) -> list[tuple[str, str]]:
        """Ordered (engine, voice id) candidates for a language."""
        code = normalize(lang) or "en"
        chosen = voice or str((cfg.get("voices") or {}).get(code) or "")
        order: list[tuple[str, str]] = []
        if chosen:
            engine, _, ident = chosen.partition(":")
            if not ident:  # a bare Piper key
                engine, ident = "piper", chosen
            order.append((engine, ident))
        preference = str(cfg.get("ttsEngine") or "auto")
        piper_voice = DEFAULT_VOICE.get(code)
        if not piper_voice:
            matches = self.catalogue.for_language(code)
            installed = [v["key"] for v in matches if self.catalogue.installed(v["key"])]
            piper_voice = (installed or [v["key"] for v in matches] or [""])[0]
        system_voice = (self.system.for_language(code) or [None])[0]
        tiers = {"piper": ("piper", piper_voice) if piper_voice else None,
                 "system": ("system", system_voice.id) if system_voice else None,
                 "online": ("online", online.DEFAULT_VOICES.get(code, "")) if cfg.get("onlineFallback") else None}
        ranking = {"auto": ("piper", "system", "online"), "piper": ("piper", "system", "online"),
                   "system": ("system", "piper", "online"), "online": ("online", "piper", "system")}
        for tier in ranking.get(preference, ranking["auto"]):
            item = tiers.get(tier)
            if item and item not in order:
                order.append(item)
        return order

    def speak(self, text: str, lang: str = "", voice: str = "", rate: float = 1.0, *, raw: bool = False) -> Speech:
        cfg = self.config()
        spoken = str(text or "") if raw else speakable(text)
        if not spoken.strip():
            raise VoiceInputError("nothing to say")
        if len(spoken) > MAX_TEXT:
            raise VoiceInputError(f"text longer than {MAX_TEXT} characters: send one sentence per request")
        try:
            rate = max(0.5, min(2.0, float(rate or cfg.get("rate") or 1.0)))
        except (TypeError, ValueError):
            rate = 1.0
        code = voice_language(lang or cfg.get("lang") or "", spoken)
        failures: list[str] = []
        downloading = None
        for engine, ident in self.resolve_voice(code, voice, cfg):
            try:
                if engine == "piper":
                    if self.tts_piper_reason():
                        failures.append(f"piper: {self.tts_piper_reason()}")
                        continue
                    key, _, speaker = ident.partition("#")
                    if not self.catalogue.installed(key):
                        if cfg.get("autoInstall") and self.catalogue.get(key) is not None:
                            downloading = self.install("piper", key)
                            failures.append(f"piper: voice {key} is downloading")
                        else:
                            failures.append(f"piper: voice {key} not installed")
                        continue
                    started = time.perf_counter()
                    reply, audio = self.worker.request({
                        "op": "speak", "model": str(self.paths.piper_model(key)),
                        "config": str(self.paths.piper_config(key)),
                        "speaker": int(speaker) if speaker.isdigit() else None, "rate": rate,
                    }, spoken.encode("utf-8"))
                    timings = dict(reply.get("timings") or {})
                    timings["elapsed"] = round(time.perf_counter() - started, 3)
                    return Speech(audio, "audio/wav", "piper", f"piper:{ident}", code,
                                  float(reply.get("duration") or 0.0), "; ".join(failures), timings)
                if engine == "system":
                    started = time.perf_counter()
                    audio, used = self.system.speak(spoken, code, ident, rate)
                    try:
                        duration = wav_info(audio).duration
                    except AudioError:
                        duration = 0.0
                    return Speech(audio, "audio/wav", "system", f"system:{used}", code, duration,
                                  "; ".join(failures), {"elapsed": round(time.perf_counter() - started, 3)})
                if engine == "online":
                    if not cfg.get("onlineFallback"):
                        continue
                    started = time.perf_counter()
                    audio, used = online.speak(spoken, code, ident, rate)
                    return Speech(audio, "audio/mpeg", "online", f"online:{used}", code, 0.0,
                                  "; ".join(failures), {"elapsed": round(time.perf_counter() - started, 3)})
            except (WorkerError, RuntimeError, OSError) as exc:
                failures.append(f"{engine}: {exc}")
        label = f"{name(code)} ({native_name(code)})" if code in LANGUAGES else code
        if downloading is not None:
            raise VoiceUnavailable("voice_downloading", f"the {label} voice is downloading; retry when "
                                   "voice.model.download reports done", job=downloading.view(), tried=failures)
        raise VoiceUnavailable("no_voice", f"no voice can speak {label} on this machine"
                               + (f" ({'; '.join(failures)})" if failures else ""), tried=failures)

    # ------------------------------------------------------------ matrix

    def voices(self, *, refresh: bool = False) -> dict:
        """{language: [{engine, voice, name, installed, quality, offline, ...}]} + metadata."""
        cfg = self.config()
        # First call fetches the full Piper catalogue once (then the disk cache
        # is used); offline it silently stays on the built-in list.
        self.catalogue.voices(refresh=refresh)
        piper_ok = not self.tts_piper_reason()
        languages = set(CORE) | set(self.catalogue.languages()) | {v.language for v in self.system.voices() if v.language}
        if cfg.get("onlineFallback"):
            languages |= set(online.DEFAULT_VOICES)
        matrix: dict[str, dict] = {}
        for code in sorted(languages, key=lambda c: (c not in CORE, CORE.index(c) if c in CORE else 0, c)):
            entries = []
            for entry in self.catalogue.for_language(code):
                key = entry["key"]
                size = sum(int(m.get("size_bytes") or 0) for p, m in (entry.get("files") or {}).items()
                           if p.endswith(".onnx"))
                entries.append({"engine": "piper", "voice": f"piper:{key}", "name": entry.get("name", key),
                                "locale": (entry.get("language") or {}).get("code", "").replace("_", "-"),
                                "quality": entry.get("quality", ""), "speakers": entry.get("num_speakers", 1),
                                "installed": self.catalogue.installed(key), "engine_available": piper_ok,
                                "offline": True, "download_mb": round(size / 1e6, 1),
                                "default": DEFAULT_VOICE.get(code) == key})
            for item in self.system.for_language(code):
                entries.append({**item.view(), "voice": f"system:{item.id}", "engine_available": True})
            if cfg.get("onlineFallback") and code in online.DEFAULT_VOICES:
                entries.append({"engine": "online", "voice": f"online:{online.DEFAULT_VOICES[code]}",
                                "name": online.DEFAULT_VOICES[code], "installed": online.installed(),
                                "engine_available": online.installed(), "offline": False, "quality": "neural",
                                "privacy": online.PRIVACY_NOTE})
            matrix[code] = {"name": name(code), "native": native_name(code),
                            "stt": code in WHISPER_LANGUAGES, "voices": entries,
                            "selected": str((cfg.get("voices") or {}).get(code) or "")}
        return {"languages": matrix, "catalogue": self.catalogue.source,
                "stt_model": cfg["sttModel"], "stt_model_installed": self.whisper_installed(cfg["sttModel"]),
                "stt_models": [{"name": m, "download_mb": download.WHISPER_SIZES_MB.get(m),
                                "installed": self.whisper_installed(m)} for m in STT_MODELS]}

    def close(self) -> None:
        try:
            self.worker.close()
        except Exception:  # noqa: BLE001
            pass
