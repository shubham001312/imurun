"""FISH native backend — loads fish.dll / libfish.so / libfish.dylib in-process.

The engine is a shared library with a small C ABI (see fish/c_api.h). Running it
inside the Python process means:

  * no separate executable to sign, launch, or firewall,
  * the weights live in one address space (no copies across a pipe),
  * streaming is a plain callback instead of an SSE hop.

The HTTP client in ``fish_client`` remains for container deployments where the
engine runs as its own service; this module is the default on a desktop.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import AsyncGenerator, Iterable, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

FISH_TOKEN_CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)


class FishStats(ctypes.Structure):
    _fields_ = [
        ("load_seconds", ctypes.c_double),
        ("prefill_seconds", ctypes.c_double),
        ("decode_seconds", ctypes.c_double),
        ("tokens_per_second", ctypes.c_double),
        ("prompt_tokens", ctypes.c_int),
        ("generated_tokens", ctypes.c_int),
        ("context_used", ctypes.c_int),
        ("context_size", ctypes.c_int),
        ("weights_bytes", ctypes.c_ulonglong),
        ("kv_bytes", ctypes.c_ulonglong),
        ("peak_rss_mb", ctypes.c_double),
    ]


class FishModelInfo(ctypes.Structure):
    _fields_ = [
        ("arch", ctypes.c_char * 32),
        ("n_layers", ctypes.c_int),
        ("n_embd", ctypes.c_int),
        ("n_ff", ctypes.c_int),
        ("n_head", ctypes.c_int),
        ("n_head_kv", ctypes.c_int),
        ("head_dim", ctypes.c_int),
        ("n_ctx", ctypes.c_int),
        ("vocab", ctypes.c_int),
        ("tied_output", ctypes.c_int),
        ("quant", ctypes.c_char * 64),
    ]


def default_library_path() -> Optional[Path]:
    """Where the engine library lives, honouring FISH_LIBRARY."""
    env = os.environ.get("FISH_LIBRARY")
    if env:
        p = Path(env)
        return p if p.exists() else None
    names = {
        "win32": ["fish.dll"],
        "darwin": ["libfish.dylib"],
    }.get(sys.platform, ["libfish.so", "fish.so"])
    for name in names:
        for base in (REPO_ROOT / "bin", REPO_ROOT / "model" / "fish" / "build"):
            candidate = base / name
            if candidate.exists():
                return candidate
    return None


def default_model_path() -> Optional[Path]:
    """The model to serve, honouring FISH_MODEL."""
    env = os.environ.get("FISH_MODEL")
    if env:
        p = Path(env)
        return p if p.exists() else None
    models = REPO_ROOT / "models"
    if not models.is_dir():
        return None
    ggufs = sorted(models.glob("*.gguf"), key=lambda p: p.stat().st_size, reverse=True)
    return ggufs[0] if ggufs else None


class FishNativeClient:
    """Thin, thread-safe wrapper over the FISH C ABI."""

    def __init__(self, config=None):
        self.config = config
        self._lib: Optional[ctypes.CDLL] = None
        self._ctx: Optional[int] = None
        self._lock = threading.RLock()
        self._gen_lock = threading.Lock()
        self._loaded = False
        self._error: Optional[str] = None
        self._library_path = default_library_path()
        self._model_path = Path(getattr(config, "model_path", None) or "") if getattr(
            config, "model_path", None
        ) else default_model_path()

    # ------------------------------------------------------------------ state

    @property
    def configured(self) -> bool:
        """True when both the library and a model file are present."""
        return self._library_path is not None and self._model_path is not None

    @property
    def library_path(self) -> Optional[Path]:
        return self._library_path

    @property
    def model_path(self) -> Optional[Path]:
        return self._model_path

    def _bind(self, lib: ctypes.CDLL) -> None:
        lib.fish_version.restype = ctypes.c_char_p
        lib.fish_create.restype = ctypes.c_void_p
        lib.fish_destroy.argtypes = [ctypes.c_void_p]
        lib.fish_load.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.fish_load.restype = ctypes.c_int
        lib.fish_loaded.argtypes = [ctypes.c_void_p]
        lib.fish_loaded.restype = ctypes.c_int
        lib.fish_last_error.argtypes = [ctypes.c_void_p]
        lib.fish_last_error.restype = ctypes.c_char_p
        lib.fish_model_info_of.argtypes = [ctypes.c_void_p, ctypes.POINTER(FishModelInfo)]
        lib.fish_model_info_of.restype = ctypes.c_int
        lib.fish_tokenize.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_int32), ctypes.c_int]
        lib.fish_tokenize.restype = ctypes.c_int
        lib.fish_detokenize.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32),
                                        ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.fish_detokenize.restype = ctypes.c_int
        lib.fish_chat_prompt.argtypes = [ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_char_p),
                                         ctypes.POINTER(ctypes.c_char_p),
                                         ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.fish_chat_prompt.restype = ctypes.c_int
        lib.fish_chat_format.argtypes = [ctypes.c_void_p]
        lib.fish_chat_format.restype = ctypes.c_char_p
        lib.fish_generate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_float, ctypes.c_int,
                                      ctypes.c_float, ctypes.c_float,
                                      FISH_TOKEN_CB, ctypes.c_void_p]
        lib.fish_generate.restype = ctypes.c_int
        lib.fish_generate_to.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_float, ctypes.c_int,
                                         ctypes.c_float, ctypes.c_float,
                                         ctypes.c_char_p, ctypes.c_int]
        lib.fish_generate_to.restype = ctypes.c_int
        lib.fish_cancel.argtypes = [ctypes.c_void_p]
        lib.fish_reset.argtypes = [ctypes.c_void_p]
        lib.fish_get_stats.argtypes = [ctypes.c_void_p, ctypes.POINTER(FishStats)]
        lib.fish_kernel_name.restype = ctypes.c_char_p
        lib.fish_specs_json.argtypes = [ctypes.c_void_p]
        lib.fish_specs_json.restype = ctypes.c_char_p

    def load(self) -> bool:
        """Load the library and the model once. Idempotent and thread-safe."""
        with self._lock:
            if self._loaded:
                return True
            if not self.configured:
                self._error = (
                    "FISH library or model not found "
                    f"(library={self._library_path}, model={self._model_path})"
                )
                return False
            try:
                lib = ctypes.CDLL(str(self._library_path))
                self._bind(lib)
                ctx = lib.fish_create()
                if not ctx:
                    self._error = "fish_create failed"
                    return False
                # n_ctx = 0 lets the specs engine pick a context from the
                # measured machine rather than a hardcoded guess.
                rc = lib.fish_load(ctx, str(self._model_path).encode("utf-8"), 0)
                if rc != 0:
                    err = lib.fish_last_error(ctx) or b"unknown error"
                    self._error = err.decode("utf-8", "replace")
                    lib.fish_destroy(ctx)
                    return False
                self._lib, self._ctx, self._loaded = lib, ctx, True
                self._error = None
                logger.info(
                    "FISH %s loaded %s with %s kernels",
                    lib.fish_version().decode(),
                    self._model_path.name,
                    lib.fish_kernel_name().decode(),
                )
                return True
            except OSError as exc:
                self._error = f"cannot load {self._library_path}: {exc}"
                logger.warning(self._error)
                return False
            except Exception as exc:  # noqa: BLE001 - never break the host
                self._error = f"FISH initialisation failed: {exc}"
                logger.warning(self._error, exc_info=True)
                return False

    def close(self) -> None:
        with self._lock:
            if self._ctx and self._lib:
                self._lib.fish_destroy(self._ctx)
            self._ctx = None
            self._lib = None
            self._loaded = False

    # ------------------------------------------------------------------ info

    def health(self) -> bool:
        return self.load()

    @property
    def error(self) -> Optional[str]:
        return self._error

    def model_info(self) -> dict:
        if not self.load():
            return {"error": self._error}
        info = FishModelInfo()
        if self._lib.fish_model_info_of(self._ctx, ctypes.byref(info)) != 0:
            return {"error": "fish_model_info_of failed"}
        return {
            "arch": info.arch.decode("utf-8", "replace"),
            "layers": info.n_layers,
            "embd": info.n_embd,
            "ff": info.n_ff,
            "heads": info.n_head,
            "kv_heads": info.n_head_kv,
            "head_dim": info.head_dim,
            "context": info.n_ctx,
            "vocab": info.vocab,
            "tied_output": bool(info.tied_output),
            "quant": info.quant.decode("utf-8", "replace"),
            "chat_format": (self._lib.fish_chat_format(self._ctx) or b"").decode(),
            "kernel": (self._lib.fish_kernel_name() or b"").decode(),
        }

    def specs(self) -> dict:
        if not self.load():
            return {"error": self._error}
        raw = self._lib.fish_specs_json(self._ctx)
        try:
            return json.loads(raw.decode("utf-8") if raw else "{}")
        except (ValueError, AttributeError):
            return {"error": "invalid specs JSON"}

    def stats(self) -> dict:
        if not self.load():
            return {"error": self._error}
        s = FishStats()
        self._lib.fish_get_stats(self._ctx, ctypes.byref(s))
        return {
            "load_seconds": s.load_seconds,
            "prefill_seconds": s.prefill_seconds,
            "decode_seconds": s.decode_seconds,
            "tokens_per_second": s.tokens_per_second,
            "prompt_tokens": s.prompt_tokens,
            "generated_tokens": s.generated_tokens,
            "context_used": s.context_used,
            "context_size": s.context_size,
            "weights_bytes": s.weights_bytes,
            "kv_bytes": s.kv_bytes,
            "peak_rss_mb": s.peak_rss_mb,
        }

    # ------------------------------------------------------------- prompting

    def chat_prompt(self, messages: Iterable[dict]) -> str:
        """Render messages with the model's own chat template."""
        if not self.load():
            raise RuntimeError(self._error or "FISH not loaded")
        msgs = list(messages)
        n = len(msgs)
        roles = (ctypes.c_char_p * n)(*[str(m.get("role", "user")).encode() for m in msgs])
        contents = (ctypes.c_char_p * n)(
            *[str(m.get("content", "")).encode() for m in msgs]
        )
        cap = 4096
        for _ in range(6):
            buf = ctypes.create_string_buffer(cap)
            got = self._lib.fish_chat_prompt(self._ctx, roles, contents, n, buf, cap)
            if got < 0:
                raise RuntimeError("fish_chat_prompt failed")
            if got < cap:
                return buf.value.decode("utf-8", "replace")
            cap = got + 1
        raise RuntimeError("chat prompt grew without bound")

    # ------------------------------------------------------------ generation

    def _params(self, params) -> dict:
        cfg = self.config
        return {
            "temperature": float(getattr(params, "temperature", None)
                                 or getattr(cfg, "temperature", 0.7)),
            "top_p": float(getattr(params, "top_p", None)
                           or getattr(cfg, "top_p", 0.9)),
            "max_tokens": int(getattr(params, "max_tokens", None)
                              or getattr(cfg, "max_tokens", 2048)),
            "top_k": int(getattr(params, "top_k", 40) or 40),
            "repeat_penalty": float(getattr(params, "repeat_penalty", 1.1) or 1.1),
        }

    def _generate(self, prompt: str, params, callback) -> int:
        p = self._params(params)
        with self._gen_lock:
            if not self.load():
                raise RuntimeError(self._error or "FISH not loaded")
            rc = self._lib.fish_generate(
                self._ctx, prompt.encode("utf-8"), 1,
                p["max_tokens"], p["temperature"], p["top_k"], p["top_p"],
                p["repeat_penalty"], callback, None,
            )
        if rc < 0:
            err = self._lib.fish_last_error(self._ctx) or b"generation failed"
            raise RuntimeError(err.decode("utf-8", "replace"))
        return rc

    def generate_full(self, prompt: str, params=None) -> str:
        chunks: list[str] = []

        def _cb(text, _token_id, _user):
            if text:
                chunks.append(text.decode("utf-8", "replace"))

        self._generate(prompt, params, FISH_TOKEN_CB(_cb))
        return "".join(chunks)

    def cancel(self) -> None:
        if self._lib and self._ctx:
            self._lib.fish_cancel(self._ctx)

    async def generate_stream(
        self, messages: Iterable[dict], params=None
    ) -> AsyncGenerator[str, None]:
        """Stream decoded text without blocking the event loop.

        Generation runs on a worker thread; chunks are handed to the loop
        through call_soon_threadsafe so the consumer sees them in order.
        """
        prompt = self.chat_prompt(messages)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        done = object()

        def _cb(text, _token_id, _user):
            if text:
                loop.call_soon_threadsafe(queue.put_nowait, text.decode("utf-8", "replace"))

        callback = FISH_TOKEN_CB(_cb)

        def _work():
            try:
                self._generate(prompt, params, callback)
            except BaseException as exc:  # noqa: BLE001 - forwarded to the loop
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        task = loop.run_in_executor(None, _work)
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if not task.done():
                self.cancel()
            await task
