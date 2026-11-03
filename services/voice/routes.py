"""HTTP handlers for ``/api/voice/*`` (behind the bearer token, like every /api route).

``services/server/api.py`` forwards any path under ``/api/voice/`` here with
the live request handler, because voice needs what the JSON-only helpers
there do not do: audio request bodies larger than the 1 MB JSON cap, and
binary (WAV/MP3) responses with engine headers.

GET  /api/voice/status                 engines, model/voice install state, worker, jobs
GET  /api/voice/voices[?refresh=1]     per-language matrix {language -> voices[]}
GET  /api/voice/jobs                   download jobs
POST /api/voice/transcribe[?lang=hi&cleanup=0|1&polish=0|1&context=...]
                                       body: audio (WAV/WebM/OGG/MP3/FLAC/M4A) or multipart
                                       (fields: audio file, lang, context = JSON list of the
                                       last messages or plain text) -> {text, text_raw, cleanup, ...}
POST /api/voice/stream?id=S&seq=N[&final=1&lang=..]  body: the next audio chunk (MediaRecorder
                                       timeslice); partials -> SSE voice.partial + the reply;
                                       final=1 -> the full transcription (+ SSE voice.final)
POST /api/voice/stream/cancel?id=S     drop a stream session
GET  /api/voice/vocabulary             {vocabulary, corrections, product_terms, prompt_preview}
POST /api/voice/vocabulary             {add: [...], remove: [...]} or {vocabulary: [...]} (replace)
POST /api/voice/correction             {heard, fixed} (the "fix this" button) | {remove: heard}
POST /api/voice/speak                  {text, language|lang, voice|voice_id, rate, raw} -> audio
POST /api/voice/voices/install         {engine: whisper|piper, name}
POST /api/voice/unload                 stop the worker now (frees the model RAM)

Errors are JSON: 400/413/415 for bad input, 503 ``{error, reason}`` when an
engine, model or voice is not available. Never a traceback.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit

LOG = logging.getLogger("imurun.voice")
_LOCK = threading.Lock()
PREFIX = "/api/voice/"
MAX_JSON = 256 * 1024


def _govern(server: Any, service) -> None:
    """Voice threads come out of the shared CPU budget (docs/resources/README.md)
    and the worker's CPU counts as IMURUN's own load, not the user's."""
    governor = getattr(server, "_governor", None)
    worker = getattr(service, "worker", None)
    if governor is None or worker is None or not hasattr(worker, "thread_grant"):
        return
    service.vram_headroom = lambda: governor.budget().vram_headroom
    worker.thread_grant = governor.voice_threads
    worker.thread_release = governor.release_voice

    def pids():
        proc = getattr(worker, "_proc", None)
        return [proc.pid] if proc is not None and proc.poll() is None else []

    governor.add_pids(pids)


def get_service(server: Any):
    service = getattr(server, "_voice", None)
    if service is not None:
        return service
    with _LOCK:
        service = getattr(server, "_voice", None)
        if service is None:
            from .service import VoiceService

            events = getattr(server, "_events", None)
            service = VoiceService(data_root=getattr(server, "_home", None),
                                   settings=getattr(server, "_settings", None),
                                   publish=getattr(events, "publish", None))
            server._voice = service
            _govern(server, service)
    if getattr(service, "_model_getter", None) is None and hasattr(service, "_model_getter"):
        # The clean-up model pass uses the FISH model only if it is loaded.
        service._model_getter = lambda: getattr(server, "_model", None)
    return service


def status_line(server: Any) -> str:
    """For /api/status: never raises, never spawns the worker."""
    try:
        return get_service(server).summary()
    except Exception as exc:  # noqa: BLE001
        return f"unavailable ({type(exc).__name__}: {exc})"


def dispatch(server: Any, handler: Any, path: str) -> None:
    """Answer one /api/voice/* request on ``handler`` (already authorised)."""
    from .service import MAX_AUDIO_BYTES, VoiceInputError, VoiceUnavailable

    method = handler.command
    route = path[len(PREFIX):].strip("/")
    query = parse_qs(urlsplit(handler.path).query)
    try:
        service = get_service(server)
        if method == "GET" and route == "status":
            handler._reply(200, service.status())
        elif method == "GET" and route == "voices":
            refresh = (query.get("refresh") or [""])[0] in ("1", "true")
            handler._reply(200, service.voices(refresh=refresh))
        elif method == "GET" and route == "jobs":
            handler._reply(200, {"jobs": service.jobs()})
        elif method == "POST" and route == "transcribe":
            body = _read(handler, MAX_AUDIO_BYTES)
            if body is None:
                return
            content_type = handler.headers.get("Content-Type", "")
            lang = (query.get("lang") or query.get("language") or [""])[0]
            context = _context((query.get("context") or [""])[0])
            if content_type.lower().startswith("multipart/form-data"):
                from .audio import AudioError, parse_multipart

                try:
                    body, content_type, fields = parse_multipart(body, content_type)
                except AudioError as exc:
                    raise VoiceInputError(str(exc)) from exc
                lang = lang or fields.get("lang") or fields.get("language") or ""
                context = context or _context(fields.get("context") or "")
            handler._reply(200, service.transcribe(body, content_type, lang, context=context,
                                                   cleanup=_flag(query, "cleanup"), polish=_flag(query, "polish")))
        elif method == "POST" and route == "stream":
            body = _read(handler, MAX_AUDIO_BYTES)
            if body is None:
                return
            sid = (query.get("id") or [""])[0]
            try:
                seq = int((query.get("seq") or ["0"])[0])
            except ValueError as exc:
                raise VoiceInputError("seq must be an integer") from exc
            lang = (query.get("lang") or query.get("language") or [""])[0]
            handler._reply(200, service.stream_chunk(sid, seq, body, handler.headers.get("Content-Type", ""), lang,
                                                     final=bool(_flag(query, "final")),
                                                     context=_context((query.get("context") or [""])[0])))
        elif method == "POST" and route == "stream/cancel":
            _read(handler, MAX_JSON)
            handler._reply(200, {"ok": service.streams.cancel((query.get("id") or [""])[0])})
        elif method == "GET" and route == "vocabulary":
            handler._reply(200, service.vocabulary())
        elif method == "POST" and route == "vocabulary":
            payload = _json(handler)
            if payload is None:
                return
            replace = payload.get("vocabulary") if isinstance(payload.get("vocabulary"), list) else None
            add = payload.get("add") if isinstance(payload.get("add"), list) else []
            remove = payload.get("remove") if isinstance(payload.get("remove"), list) else []
            handler._reply(200, service.update_vocabulary(add=add, remove=remove, replace=replace))
        elif method == "POST" and route == "correction":
            payload = _json(handler)
            if payload is None:
                return
            if payload.get("remove"):
                handler._reply(200, service.remove_correction(str(payload["remove"])))
            else:
                handler._reply(200, service.add_correction(str(payload.get("heard") or ""),
                                                           str(payload.get("fixed") or "")))
        elif method == "POST" and route == "speak":
            payload = _json(handler)
            if payload is None:
                return
            speech = service.speak(str(payload.get("text") or ""),
                                   str(payload.get("language") or payload.get("lang") or ""),
                                   str(payload.get("voice") or payload.get("voice_id") or ""),
                                   payload.get("rate") or 0, raw=bool(payload.get("raw")))
            headers = {"Content-Type": speech.content_type, "Cache-Control": "no-store",
                       "X-Content-Type-Options": "nosniff", **speech.headers()}
            handler._raw(200, headers, speech.audio)
        elif method == "POST" and route == "voices/install":
            payload = _json(handler)
            if payload is None:
                return
            name = str(payload.get("name") or payload.get("voice") or payload.get("model") or "")
            engine = str(payload.get("engine") or "")
            if name.startswith("piper:"):
                engine, name = "piper", name[len("piper:"):]
            job = service.install(engine, name.partition("#")[0])
            handler._reply(202, {"job": job.view()})
        elif method == "POST" and route == "unload":
            service.worker.unload()
            handler._reply(200, {"ok": True, "worker": service.worker.status()})
        else:
            handler._reply(404, {"error": "not found"})
    except VoiceInputError as exc:
        handler._reply(exc.status, {"error": "bad_request", "reason": str(exc), "message": str(exc)})
    except VoiceUnavailable as exc:
        handler._reply(503, exc.payload())
    except Exception as exc:  # noqa: BLE001 - the contract: a reason, never a traceback
        LOG.exception("voice route %s failed", path)
        handler._reply(503, {"error": "voice_error", "reason": f"{type(exc).__name__}: {exc}",
                             "message": "the voice layer failed; see the server log"})


def _flag(query: dict, key: str) -> bool | None:
    value = (query.get(key) or [""])[0].lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def _context(raw: str) -> list:
    """The caller's recent messages: a JSON list of strings / {content}, or plain text."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw[:1] in "[{":
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            out = []
            for item in data[-8:]:
                if isinstance(item, dict):
                    item = item.get("content") or item.get("text") or ""
                if isinstance(item, str) and item.strip():
                    out.append(item[:4000])
            return out
    return [raw[:4000]]


def _read(handler: Any, limit: int) -> bytes | None:
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        handler._reply(411, {"error": "length_required", "reason": "send Content-Length (no chunked bodies)"})
        handler.close_connection = True
        return None
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        handler._reply(400, {"error": "bad_request", "reason": "invalid content length"})
        return None
    if length > limit:
        if length <= limit + 65536:
            handler._drain(length)
        else:
            handler.close_connection = True
        handler._reply(413, {"error": "too_large", "reason": f"body larger than {limit // (1024 * 1024)} MB"})
        return None
    return handler.rfile.read(length) if length else b""


def _json(handler: Any) -> dict | None:
    raw = _read(handler, MAX_JSON)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        handler._reply(400, {"error": "bad_request", "reason": "invalid json"})
        return None
    if not isinstance(payload, dict):
        handler._reply(400, {"error": "bad_request", "reason": "a json object is required"})
        return None
    return payload
