"""Streaming dictation: chunked uploads, partial transcripts, final text.

The shell records with ``MediaRecorder`` and a timeslice (~1.5 s) and posts
every chunk to ``POST /api/voice/stream?id=<session>&seq=<n>``; the last one
carries ``final=1``. WebM chunks are only decodable together (the first one
holds the header), so a session keeps the whole byte stream and each partial
re-decodes it from the last committed point. That re-decode IS the overlap:
words cut at a chunk boundary come out right on the next pass, and the
final pass replaces every partial.

* Partials run in a background thread, at most one per session; a chunk
  that arrives while one runs just extends the buffer (the next pass sees
  it). Each pass publishes ``voice.partial`` on the event bus and the
  latest one is also returned in every chunk reply (no SSE needed).
* Long dictation: past ``COMMIT_AFTER_S`` of uncommitted audio, segments
  ending more than ``COMMIT_GUARD_S`` before the end are committed (their
  text is fixed, their audio skipped from then on), so a pass never decodes
  more than ~30 s.
* Endpointing: when the last speech segment ended ``endpoint_s`` (default
  1.2 s) before the end of the audio, the partial says ``endpoint: true``;
  the shell may stop recording (hands-free mode).
* The language detected on the first pass with >= 2 s of audio is kept for
  the rest of the session, so partials cannot flip scripts mid-sentence.

Sessions expire ``IDLE_S`` after their last chunk.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

COMMIT_AFTER_S = 20.0
COMMIT_GUARD_S = 4.0
IDLE_S = 60.0
MAX_SESSIONS = 4
MAX_BYTES = 32 * 1024 * 1024


@dataclass
class StreamSession:
    id: str
    content_type: str = ""
    data: bytearray = field(default_factory=bytearray)
    seq: int = -1
    lang: str = ""
    language: str = ""            # locked after the first confident pass
    context: list = field(default_factory=list)
    committed_text: list = field(default_factory=list)
    committed_offset: float = 0.0
    partial: dict = field(default_factory=dict)
    passes: int = 0
    busy: bool = False
    dirty: bool = False
    created: float = field(default_factory=time.monotonic)
    updated: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    idle: threading.Condition = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.idle = threading.Condition(self.lock)

    def stable_text(self) -> str:
        return " ".join(t for t in self.committed_text if t).strip()


class StreamManager:
    """Owns the sessions; ``run_pass(session, final)`` is the service's transcription."""

    def __init__(self, run_pass, publish) -> None:
        self._run_pass = run_pass
        self._publish = publish
        self._sessions: dict[str, StreamSession] = {}
        self._lock = threading.Lock()

    def _expire(self) -> None:
        now = time.monotonic()
        for key, item in list(self._sessions.items()):
            if now - item.updated > IDLE_S and not item.busy:
                self._sessions.pop(key, None)

    def get(self, sid: str) -> StreamSession | None:
        with self._lock:
            return self._sessions.get(sid)

    def open(self, sid: str, content_type: str, lang: str, context) -> StreamSession:
        with self._lock:
            self._expire()
            session = self._sessions.get(sid)
            if session is None:
                if len(self._sessions) >= MAX_SESSIONS:
                    oldest = min(self._sessions.values(), key=lambda s: s.updated)
                    self._sessions.pop(oldest.id, None)
                session = StreamSession(id=sid, content_type=content_type, lang=lang,
                                        context=list(context or []))
                self._sessions[sid] = session
            return session

    def cancel(self, sid: str) -> bool:
        with self._lock:
            return self._sessions.pop(sid, None) is not None

    def add(self, session: StreamSession, seq: int, chunk: bytes) -> bool:
        """Append a chunk in order; a repeated sequence number is ignored."""
        with session.lock:
            if seq <= session.seq:
                return False
            if seq != session.seq + 1:
                raise ValueError(f"chunk {seq} out of order (expected {session.seq + 1})")
            if len(session.data) + len(chunk) > MAX_BYTES:
                raise OverflowError("stream longer than 32 MB")
            session.data += chunk
            session.seq = seq
            session.updated = time.monotonic()
            session.dirty = True
            return True

    def kick(self, session: StreamSession) -> None:
        with session.lock:
            if session.busy or not session.dirty:
                return
            session.busy = True
            session.dirty = False
        threading.Thread(target=self._partial_loop, args=(session,), daemon=True,
                         name=f"voice-stream-{session.id[:8]}").start()

    def _partial_loop(self, session: StreamSession) -> None:
        try:
            while True:
                try:
                    view = self._run_pass(session, False)
                except Exception as exc:  # noqa: BLE001 - a partial may fail (truncated chunk)
                    view = {"id": session.id, "error": str(exc)[:200], "partial": True}
                if view and view.get("text") is not None:
                    session.partial = view
                    self._publish("voice.partial", view)
                with session.lock:
                    if not session.dirty:
                        session.busy = False
                        session.idle.notify_all()
                        return
                    session.dirty = False
        except BaseException:
            with session.lock:
                session.busy = False
                session.idle.notify_all()
            raise

    def finish(self, session: StreamSession, timeout: float = 120.0) -> dict:
        with session.lock:
            deadline = time.monotonic() + timeout
            while session.busy and time.monotonic() < deadline:
                session.idle.wait(0.5)
            session.busy = True
        try:
            result = self._run_pass(session, True)
        finally:
            with session.lock:
                session.busy = False
            self.cancel(session.id)
        return result

    def views(self) -> list[dict]:
        with self._lock:
            return [{"id": s.id, "seq": s.seq, "bytes": len(s.data), "passes": s.passes, "busy": s.busy,
                     "committed_s": round(s.committed_offset, 2), "language": s.language}
                    for s in self._sessions.values()]
