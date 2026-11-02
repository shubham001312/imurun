"""The voice worker: a child process that owns the heavy engines.

Why a process and not threads in the server:

* priority - the worker lowers its OWN process priority (Windows
  ``SetPriorityClass(BELOW_NORMAL)``, POSIX ``os.nice(10)``), which covers the
  CTranslate2 / onnxruntime threads too. Lowering a thread in the server
  would not reach the engines' internal thread pools;
* memory - "idle unload" kills the process, so the OS really gets the RAM
  back (a Python process rarely returns freed model memory);
* isolation - a native crash in an engine cannot take down the server.

Protocol (stdin/stdout, binary): each frame is ``<II`` (header length,
payload length), a UTF-8 JSON header, then the payload bytes. One request in
flight at a time (the client holds a lock). Requests::

    {"op": "ping"}
    {"op": "transcribe", "model_dir", "threads", "language", "beam_size", "vad",
     "content_type"} + audio bytes
    {"op": "speak", "model", "config", "threads", "speaker", "rate"} + UTF-8 text
    {"op": "unload"}

Replies: ``{"ok": true, ...}`` (+ WAV bytes for speak) or
``{"ok": false, "error": "...", "kind": "audio" | "engine"}``.

Run: ``python -m services.voice.worker`` (the client does this with the same
interpreter as the server). ``IMURUN_VOICE_FAKE=1`` swaps in fake engines.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

LOG = logging.getLogger("imurun.voice.worker")
_HEAD = struct.Struct("<II")
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000


def write_frame(stream, header: dict, payload: bytes = b"") -> None:
    blob = json.dumps(header, ensure_ascii=False).encode("utf-8")
    stream.write(_HEAD.pack(len(blob), len(payload)) + blob + payload)
    stream.flush()


def read_frame(stream) -> tuple[dict, bytes] | None:
    head = _read_exact(stream, _HEAD.size)
    if head is None:
        return None
    size_header, size_payload = _HEAD.unpack(head)
    blob = _read_exact(stream, size_header)
    payload = _read_exact(stream, size_payload) if size_payload else b""
    if blob is None or payload is None:
        return None
    return json.loads(blob.decode("utf-8")), payload


def _read_exact(stream, size: int) -> bytes | None:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            return None
        data += chunk
    return bytes(data)


def lower_priority() -> str:
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            # HANDLE is pointer-sized: with ctypes' default int types the
            # pseudo-handle -1 was truncated and the call failed with
            # ERROR_INVALID_HANDLE (BUGS-FIXED #2).
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            if kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS):
                return "below_normal"
            return "unchanged"
        os.nice(10)
        return "nice+10"
    except Exception:  # noqa: BLE001 - priority is best effort
        return "unchanged"


# ------------------------------------------------------------------ worker

def main() -> int:
    channel_in = sys.stdin.buffer
    channel_out = sys.stdout.buffer
    sys.stdout = sys.stderr  # stray prints from libraries must not corrupt the protocol
    priority = lower_priority()
    from .audio import AudioError
    from . import engines

    fake = os.getenv("IMURUN_VOICE_FAKE", "").strip() == "1"
    stt = engines.FakeSTT() if fake else engines.WhisperSTT()
    tts = engines.FakeTTS() if fake else engines.PiperTTS(os.getcwd())
    while True:
        frame = read_frame(channel_in)
        if frame is None:
            return 0
        header, payload = frame
        op = header.get("op")
        try:
            if op == "ping":
                write_frame(channel_out, {"ok": True, "pid": os.getpid(), "priority": priority, "fake": fake,
                                          "whisper": fake or engines.has_module("faster_whisper"),
                                          "piper": fake or engines.has_module("piper")})
            elif op == "transcribe":
                result = stt.transcribe(payload, str(header.get("content_type") or ""),
                                        model_dir=str(header.get("model_dir") or ""),
                                        threads=int(header.get("threads") or 2),
                                        language=str(header.get("language") or ""),
                                        beam_size=int(header.get("beam_size") or 1),
                                        vad=bool(header.get("vad", True)),
                                        candidates=list(header.get("candidates") or []))
                write_frame(channel_out, {"ok": True, **result})
            elif op == "speak":
                audio, meta = tts.speak(payload.decode("utf-8"), model=str(header.get("model") or ""),
                                        config=str(header.get("config") or ""),
                                        threads=int(header.get("threads") or 2),
                                        speaker=header.get("speaker"),
                                        rate=float(header.get("rate") or 1.0))
                write_frame(channel_out, {"ok": True, **meta}, audio)
            elif op == "unload":
                stt.unload()
                tts.unload()
                write_frame(channel_out, {"ok": True})
            else:
                write_frame(channel_out, {"ok": False, "error": f"unknown op {op!r}", "kind": "engine"})
        except AudioError as exc:
            write_frame(channel_out, {"ok": False, "error": str(exc), "kind": "audio"})
        except Exception as exc:  # noqa: BLE001 - every failure becomes a reply
            write_frame(channel_out, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "kind": "engine"})


# ------------------------------------------------------------------ client

class WorkerError(RuntimeError):
    def __init__(self, message: str, kind: str = "engine") -> None:
        super().__init__(message)
        self.kind = kind


class WorkerClient:
    """Spawns the worker on demand, serialises requests, unloads when idle."""

    def __init__(self, *, cwd: Path, threads: int = 2, idle_seconds: float = 600.0,
                 python: str | None = None, env: dict | None = None) -> None:
        self._cwd = Path(cwd)
        self.threads = max(1, int(threads))
        self.idle_seconds = float(idle_seconds)
        self._python = python or sys.executable
        self._env = dict(env or {})
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stderr: collections.deque[str] = collections.deque(maxlen=40)
        self._last_used = 0.0
        self.started = 0
        self.info: dict = {}
        self._closed = False
        self._reaper = threading.Thread(target=self._reap, daemon=True, name="imurun-voice-idle")
        self._reaper.start()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _spawn(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        env.update(self._env)
        env["PYTHONPATH"] = str(repo) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["OMP_NUM_THREADS"] = str(self.threads)
        env["HF_HUB_OFFLINE"] = "1"   # the worker never downloads; the server does
        env["PYTHONIOENCODING"] = "utf-8"
        self._cwd.mkdir(parents=True, exist_ok=True)
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            [self._python, "-m", "services.voice.worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(self._cwd), env=env, **kwargs,
        )
        self.started += 1
        threading.Thread(target=self._drain, args=(self._proc,), daemon=True).start()
        header, _ = self._exchange({"op": "ping"}, b"", timeout=60.0)
        self.info = header

    def _drain(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self._stderr.append(line)
                LOG.debug("voice worker: %s", line)

    def _exchange(self, header: dict, payload: bytes, timeout: float) -> tuple[dict, bytes]:
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        result: list = []

        def run() -> None:
            try:
                write_frame(proc.stdin, header, payload)
                result.append(read_frame(proc.stdout))
            except (OSError, ValueError) as exc:
                result.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            self._kill()
            raise WorkerError(f"voice worker timed out after {timeout:.0f}s")
        outcome = result[0] if result else None
        if outcome is None or isinstance(outcome, Exception):
            tail = " | ".join(list(self._stderr)[-3:])
            self._kill()
            raise WorkerError("voice worker exited" + (f": {tail}" if tail else ""))
        return outcome

    def request(self, header: dict, payload: bytes = b"", timeout: float = 300.0) -> tuple[dict, bytes]:
        with self._lock:
            if self._closed:
                raise WorkerError("voice worker is shut down")
            if not self.running:
                self._spawn()
            header = dict(header)
            header.setdefault("threads", self.threads)
            reply, data = self._exchange(header, payload, timeout)
            self._last_used = time.monotonic()
        if not reply.get("ok"):
            raise WorkerError(str(reply.get("error") or "voice worker error"), str(reply.get("kind") or "engine"))
        return reply, data

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def unload(self) -> None:
        with self._lock:
            self._kill()

    def close(self) -> None:
        self._closed = True
        self.unload()

    def _reap(self) -> None:
        while not self._closed:
            time.sleep(5.0)
            if self.idle_seconds <= 0 or not self.running:
                continue
            if time.monotonic() - self._last_used > self.idle_seconds and self._lock.acquire(blocking=False):
                try:
                    if self.running and time.monotonic() - self._last_used > self.idle_seconds:
                        LOG.info("voice worker idle for %.0fs: unloading", self.idle_seconds)
                        self._kill()
                finally:
                    self._lock.release()

    def status(self) -> dict:
        return {"running": self.running, "pid": self._proc.pid if self.running and self._proc else None,
                "threads": self.threads, "idle_unload_s": self.idle_seconds, "starts": self.started,
                "priority": self.info.get("priority", "")}


if __name__ == "__main__":
    sys.exit(main())
