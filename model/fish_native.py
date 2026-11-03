"""FISH native backend — loads fish.dll / libfish.so / libfish.dylib in-process.

The engine is a shared library with a small C ABI (see model/fish/include/fish/c_api.h).
Running it inside the Python process means:

  * no separate executable to sign, launch, or firewall,
  * the weights live in one address space (no copies across a pipe),
  * streaming is a plain callback instead of an SSE hop.

The HTTP client in ``fish_client`` remains for container deployments where the
engine runs as its own service; this module is the default on a desktop.

Thread-safety contract
----------------------
``_gen_lock`` serialises everything that touches the engine context while a
generation may be running (generate, select, close, unload). ``_lock`` guards
the Python-side state. Never destroy the context without holding ``_gen_lock``.
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import AsyncGenerator, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

FISH_TOKEN_CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)

# The ABI this module was written against. Older libraries still load; the
# newer fields are simply reported as zero.
EXPECTED_ABI = 4


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
        # ABI 2
        ("truncated", ctypes.c_int),
        ("cached_tokens", ctypes.c_int),
        ("n_threads", ctypes.c_int),
        ("streaming", ctypes.c_int),
        ("prefill_tokens_per_second", ctypes.c_double),
        ("gpu_layers", ctypes.c_int),
        ("ram_layers", ctypes.c_int),
        ("disk_layers", ctypes.c_int),
        ("gpu_bytes", ctypes.c_ulonglong),
        ("gpu_name", ctypes.c_char * 64),
        ("draft_proposed", ctypes.c_int),
        ("draft_accepted", ctypes.c_int),
        ("draft_active", ctypes.c_int),
        ("gpu_mix", ctypes.c_char * 160),
        # ABI 4
        ("placement", ctypes.c_char * 320),
        ("load_profile", ctypes.c_char * 400),
        ("vram_total", ctypes.c_ulonglong),
        ("vram_free", ctypes.c_ulonglong),
        ("vram_headroom", ctypes.c_ulonglong),
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
        # ABI 2
        ("n_experts", ctypes.c_int),
        ("n_experts_per_tok", ctypes.c_int),
        ("n_ctx_train", ctypes.c_int),
        ("chat_format", ctypes.c_char * 16),
        ("tokenizer", ctypes.c_char * 8),
    ]


class FishAdapterInfo(ctypes.Structure):
    """ABI 3: the LoRA adapter merged into the loaded weights (c_api.h)."""
    _fields_ = [
        ("active", ctypes.c_int),
        ("pending", ctypes.c_int),
        ("name", ctypes.c_char * 128),
        ("rank", ctypes.c_int),
        ("rank_min", ctypes.c_int),
        ("alpha", ctypes.c_float),
        ("scale", ctypes.c_float),
        ("effective_scale", ctypes.c_float),
        ("tensors", ctypes.c_int),
        ("merged_bytes", ctypes.c_ulonglong),
        ("merge_seconds", ctypes.c_double),
        ("retention", ctypes.c_double),
        ("residual", ctypes.c_double),
        ("blocks", ctypes.c_ulonglong),
        ("blocks_refit", ctypes.c_ulonglong),
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
    # Same order as install.find_engine_library: the checkout's bin/, a fresh
    # build, then the user's data root where `imurun --get-engine` saves it.
    user_bin = Path(os.environ.get("IMURUN_DATA_ROOT") or (Path.home() / ".imurun")).expanduser() / "bin"
    for name in names:
        for base in (REPO_ROOT / "bin", REPO_ROOT / "model" / "fish" / "build", user_bin):
            candidate = base / name
            if candidate.exists():
                return candidate
    return None


def default_model_path() -> Optional[Path]:
    """The model to serve: FISH_MODEL only.

    No model is picked implicitly from the models directory: loading a
    multi-gigabyte file is a decision the user makes in the Models tab.
    """
    env = os.environ.get("FISH_MODEL")
    if env:
        p = Path(env)
        return p if p.exists() else None
    return None


def _peak_rss_mb() -> float:
    """Resident set of this process, measured here because the DLL will not."""
    try:
        if sys.platform == "win32":
            import ctypes.wintypes as wt

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            psapi = ctypes.WinDLL("psapi")
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return pmc.PeakWorkingSetSize / (1024 * 1024)
        else:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return peak / 1024 if sys.platform != "darwin" else peak / (1024 * 1024)
    except Exception:  # noqa: BLE001 - a missing counter is not an error
        pass
    return 0.0


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
        self._last_used = 0.0
        self._abi = 0
        # Repeat penalty used when neither the call nor the config sets one;
        # chosen per model at load (see _repeat_default_for_loaded).
        self._default_repeat = 1.1
        self._library_path = default_library_path()
        cfg_model = getattr(config, "model_path", None)
        self._model_path = Path(cfg_model) if cfg_model else default_model_path()
        # LoRA adapter merged into the weights at every load (ABI 3), or None.
        cfg_adapter = getattr(config, "adapter_path", None)
        self._adapter_path: Optional[Path] = Path(cfg_adapter) if cfg_adapter else None
        self._adapter_scale = float(getattr(config, "adapter_scale", 1.0) or 1.0)
        # Resource governor (services/resources): engine knobs for each load
        # and the thread target of each generation. None = engine defaults.
        self.resource_hook = None
        self._threads_applied: Optional[int] = None

    # ------------------------------------------------------------------ state

    @property
    def configured(self) -> bool:
        """True when both the library and a model file are present."""
        return self._library_path is not None and self._model_path is not None

    @property
    def available(self) -> bool:
        """True when the engine is loaded and ready to generate."""
        return self._loaded and self._ctx is not None and self._lib is not None

    @property
    def loaded(self) -> bool:
        return self.available

    @property
    def library_path(self) -> Optional[Path]:
        return self._library_path

    @property
    def model_path(self) -> Optional[Path]:
        return self._model_path

    @property
    def error(self) -> Optional[str]:
        return self._error

    def selected_name(self) -> str:
        """The model file name the engine is currently serving."""
        return self._model_path.name if self._model_path else ""

    # ------------------------------------------------------------------ binding

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
        # ABI 2 functions (absent in older libraries)
        if hasattr(lib, "fish_specs_json"):
            lib.fish_specs_json.argtypes = [ctypes.c_void_p]
            lib.fish_specs_json.restype = ctypes.c_char_p
        if hasattr(lib, "fish_abi_version"):
            lib.fish_abi_version.restype = ctypes.c_int
        if hasattr(lib, "fish_supported_archs"):
            lib.fish_supported_archs.restype = ctypes.c_char_p
        if hasattr(lib, "fish_set_draft"):
            lib.fish_set_draft.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            lib.fish_set_draft.restype = ctypes.c_int
            lib.fish_clear_draft.argtypes = [ctypes.c_void_p]
        if hasattr(lib, "fish_set_adapter"):
            lib.fish_set_adapter.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_float]
            lib.fish_set_adapter.restype = ctypes.c_int
            lib.fish_clear_adapter.argtypes = [ctypes.c_void_p]
            lib.fish_clear_adapter.restype = ctypes.c_int
            lib.fish_adapter_info_of.argtypes = [ctypes.c_void_p, ctypes.POINTER(FishAdapterInfo)]
            lib.fish_adapter_info_of.restype = ctypes.c_int
        if hasattr(lib, "fish_count_chat_tokens"):
            lib.fish_count_chat_tokens.argtypes = [ctypes.c_void_p,
                                                   ctypes.POINTER(ctypes.c_char_p),
                                                   ctypes.POINTER(ctypes.c_char_p), ctypes.c_int]
            lib.fish_count_chat_tokens.restype = ctypes.c_int
        if hasattr(lib, "fish_set_threads"):  # requested: docs/engine/REQUESTS.md R1
            lib.fish_set_threads.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.fish_set_threads.restype = ctypes.c_int
        if hasattr(lib, "fish_generate_chat"):
            lib.fish_generate_chat.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_char_p),
                                               ctypes.POINTER(ctypes.c_char_p),
                                               ctypes.c_int,
                                               ctypes.c_int, ctypes.c_float, ctypes.c_int,
                                               ctypes.c_float, ctypes.c_float,
                                               FISH_TOKEN_CB, ctypes.c_void_p]
            lib.fish_generate_chat.restype = ctypes.c_int

    # ------------------------------------------------------------------ lifecycle

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
                lib = self._lib or ctypes.CDLL(str(self._library_path))
                if self._lib is None:
                    self._bind(lib)
                self._apply_load_budget()
                ctx = lib.fish_create()
                if not ctx:
                    self._error = "fish_create failed"
                    return False
                if self._adapter_path is not None:
                    # Remembered by the engine and merged during fish_load.
                    if not hasattr(lib, "fish_set_adapter"):
                        self._error = ("this FISH build cannot apply LoRA adapters "
                                       "(needs ABI 3); rebuild the engine")
                        lib.fish_destroy(ctx)
                        return False
                    lib.fish_set_adapter(ctx, str(self._adapter_path).encode("utf-8"),
                                         float(self._adapter_scale))
                # n_ctx = 0 lets the specs engine pick a context from the
                # measured machine rather than a hardcoded guess.
                n_ctx = int(getattr(self.config, "context_size", 0) or 0)
                rc = lib.fish_load(ctx, str(self._model_path).encode("utf-8"), n_ctx)
                if rc != 0:
                    err = lib.fish_last_error(ctx) or b"unknown error"
                    self._error = err.decode("utf-8", "replace")
                    lib.fish_destroy(ctx)
                    return False
                self._lib, self._ctx, self._loaded = lib, ctx, True
                self._abi = int(lib.fish_abi_version()) if hasattr(lib, "fish_abi_version") else 1
                self._error = None
                self._last_used = time.time()
                self._default_repeat = self._repeat_default_for_loaded()
                self._attach_draft()
                self._note_engine_threads()
                logger.info(
                    "FISH %s (abi %d) loaded %s with %s kernels",
                    lib.fish_version().decode(), self._abi,
                    self._model_path.name, lib.fish_kernel_name().decode(),
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

    # Engine knobs read at load: FISH_THREADS (thread-pool size, fixed for the
    # process once the pool exists), FISH_RAM_BUDGET_MB (layer placement),
    # FISH_VRAM_HEADROOM_MB (VRAM kept free). Explicit FishConfig values win
    # over the governor; a value exported in the shell before start wins over both.
    _CONFIG_KNOBS = (("threads", "FISH_THREADS"), ("ram_budget_mb", "FISH_RAM_BUDGET_MB"),
                     ("vram_headroom_mb", "FISH_VRAM_HEADROOM_MB"))
    _SHELL_KNOBS = frozenset(k for _a, k in _CONFIG_KNOBS if os.environ.get(k))

    def _apply_load_budget(self) -> None:
        env: dict = {}
        hook = self.resource_hook
        if hook is not None:
            try:
                env.update(hook.load_env() or {})
            except Exception:  # noqa: BLE001 - a broken governor keeps engine defaults
                logger.debug("resource governor load_env failed", exc_info=True)
        for attr, key in self._CONFIG_KNOBS:
            try:
                value = int(getattr(self.config, attr, 0) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                env[key] = str(value)
        for key, value in env.items():
            if key not in self._SHELL_KNOBS:
                os.environ[key] = str(value)

    def _note_engine_threads(self) -> None:
        hook = self.resource_hook
        if hook is None or not hasattr(hook, "note_engine"):
            return
        try:
            s = FishStats()
            self._lib.fish_get_stats(self._ctx, ctypes.byref(s))
            hook.note_engine(pool_threads=int(s.n_threads) or None,
                             can_retarget=hasattr(self._lib, "fish_set_threads"))
        except Exception:  # noqa: BLE001
            pass

    def _begin_generation(self) -> None:
        """Caller holds ``_gen_lock``. Apply the governor's thread target when
        the engine can change it per generation (docs/engine/REQUESTS.md R1)."""
        hook = self.resource_hook
        if hook is None:
            return
        try:
            n = int(hook.before_generate())
        except Exception:  # noqa: BLE001
            return
        if n > 0 and hasattr(self._lib, "fish_set_threads") and n != self._threads_applied:
            try:
                if self._lib.fish_set_threads(self._ctx, n) == 0:
                    self._threads_applied = n
                    hook.note_engine(applied=n)
            except Exception:  # noqa: BLE001
                pass

    def _end_generation(self) -> None:
        hook = self.resource_hook
        if hook is not None:
            try:
                hook.after_generate()
            except Exception:  # noqa: BLE001
                pass

    # Below this many parameters the repeat penalty defaults to off. Measured
    # on Llama-3.2-1B Q4_K_M (2026-11-01, 4 runs x 5 sums, temp 0.35): penalty
    # 1.1 got 16/20 sums right, 1.0 got 20/20, and 1.0 produced no repetition
    # loops in 8 long answers. The penalty also counts the prompt's own tokens,
    # so it pushes a small model away from the very numbers it was asked about.
    SMALL_MODEL_PARAMS = 2.5e9

    def _repeat_default_for_loaded(self) -> float:
        """1.0 for a small model, 1.1 otherwise. Caller holds ``_lock``."""
        try:
            info = FishModelInfo()
            if self._lib.fish_model_info_of(self._ctx, ctypes.byref(info)) != 0:
                return 1.1
            d, layers = int(info.n_embd), int(info.n_layers)
            experts = max(1, int(info.n_experts))
            params = layers * (4 * d * d + 3 * d * int(info.n_ff) * experts) + int(info.vocab) * d
            return 1.0 if 0 < params < self.SMALL_MODEL_PARAMS else 1.1
        except Exception:  # noqa: BLE001 - an unreadable shape keeps the old default
            return 1.1

    def _attach_draft(self) -> None:
        """Speculative decoding: pair the loaded model with a small draft model
        of the same vocabulary (FISH_DRAFT_MODEL or config.draft_model_path).
        A draft that does not match is reported and ignored, never fatal."""
        path = os.environ.get("FISH_DRAFT_MODEL") or getattr(self.config, "draft_model_path", None)
        if not path or not hasattr(self._lib, "fish_set_draft"):
            return
        draft = Path(path)
        if not draft.is_file() or draft.resolve() == Path(self._model_path).resolve():
            return
        k = int(os.environ.get("FISH_DRAFT_K") or getattr(self.config, "draft_k", 0) or 0)
        rc = self._lib.fish_set_draft(self._ctx, str(draft).encode("utf-8"), k)
        if rc == 0:
            logger.info("FISH draft model attached: %s (k=%s)", draft.name, k or "default")
        else:
            err = (self._lib.fish_last_error(self._ctx) or b"").decode("utf-8", "replace")
            logger.warning("FISH draft model not attached (%s): %s", rc, err)

    def set_draft(self, path: Path | str | None, k: int = 0) -> bool:
        """Attach (or with None detach) a draft model at runtime."""
        with self._gen_lock:
            if not self.load():
                return False
            if path is None:
                self._lib.fish_clear_draft(self._ctx)
                return True
            rc = self._lib.fish_set_draft(self._ctx, str(path).encode("utf-8"), int(k))
            if rc != 0:
                self._error = (self._lib.fish_last_error(self._ctx) or b"").decode("utf-8", "replace")
            return rc == 0

    @property
    def adapter_path(self) -> Optional[Path]:
        return self._adapter_path

    def set_adapter(self, path: Path | str | None, scale: float = 1.0) -> bool:
        """Merge a LoRA adapter GGUF into the loaded model (None removes it).

        The engine reloads the same model file with the adapter merged at
        load (no merged GGUF on disk). On rejection the engine reloads the
        plain model and ``error`` says why; the adapter setting is dropped.
        """
        target = Path(path) if path is not None else None
        if target is not None and not target.is_file():
            self._error = f"Adapter file not found: {target}"
            return False
        with self._gen_lock:
            with self._lock:
                if not self._loaded:
                    self._adapter_path, self._adapter_scale = target, float(scale)
                    return self.load() if target is not None else True
                if not hasattr(self._lib, "fish_set_adapter"):
                    self._error = "this FISH build cannot apply LoRA adapters (needs ABI 3)"
                    return False
                if target is None:
                    rc = self._lib.fish_clear_adapter(self._ctx)
                else:
                    rc = self._lib.fish_set_adapter(self._ctx, str(target).encode("utf-8"), float(scale))
                if rc == 0:
                    self._adapter_path, self._adapter_scale = target, float(scale)
                    self._error = None
                else:
                    self._error = (self._lib.fish_last_error(self._ctx) or b"").decode("utf-8", "replace")
                    self._adapter_path, self._adapter_scale = None, 1.0
                    if rc == -3:   # nothing is loaded any more
                        self._destroy_locked()
                        return False
                # A reload drops the draft model; attach it again.
                self._default_repeat = self._repeat_default_for_loaded()
                self._attach_draft()
                self._last_used = time.time()
                return rc == 0

    def adapter_info(self) -> Optional[dict]:
        """The merged adapter as a dict, or None (no adapter / old engine)."""
        if not self.available or not hasattr(self._lib, "fish_adapter_info_of"):
            return None
        a = FishAdapterInfo()
        if self._lib.fish_adapter_info_of(self._ctx, ctypes.byref(a)) != 0:
            return None
        if not a.active and not a.pending:
            return None
        return {
            "active": bool(a.active),
            "pending": bool(a.pending),
            "name": (a.name or b"").decode("utf-8", "replace"),
            "rank": a.rank,
            "alpha": a.alpha,
            "scale": a.scale,
            "effective_scale": a.effective_scale,
            "tensors": a.tensors,
            "merged_bytes": a.merged_bytes,
            "merge_seconds": a.merge_seconds,
            "retention": a.retention,
            "residual": a.residual,
            "blocks": a.blocks,
            "blocks_refit": a.blocks_refit,
        }

    def _destroy_locked(self) -> None:
        """Free the context. Caller holds both locks."""
        if self._ctx and self._lib:
            self._lib.fish_destroy(self._ctx)
        self._ctx = None
        self._loaded = False

    def close(self) -> None:
        self.cancel()
        with self._gen_lock:
            with self._lock:
                self._destroy_locked()
                self._lib = None

    # --------------------------------------------------------------- keepalive

    @property
    def last_used(self) -> float:
        return self._last_used

    @property
    def idle_seconds(self) -> float:
        if not self._last_used:
            return 0.0
        return max(0.0, time.time() - self._last_used)

    def unload_if_idle(self, timeout_seconds: float) -> bool:
        """Unload the weights after inactivity so an idle machine gets its RAM back.

        The model is *not* unloaded between messages; it stays resident while
        the user is active. Only a full ``timeout_seconds`` of silence frees it,
        and the next turn transparently reloads it.
        """
        if timeout_seconds <= 0 or not self._loaded:
            return False
        # Serialise against generation so the context is never destroyed mid-decode.
        with self._gen_lock:
            with self._lock:
                if not self._loaded:
                    return False
                if self._last_used and (time.time() - self._last_used) < timeout_seconds:
                    return False
                self._destroy_locked()
                logger.info("FISH unloaded after %.0fs idle", timeout_seconds)
                return True

    # ------------------------------------------------------------- selection

    def select(self, path: Path | str, adapter: Path | str | None = None,
               scale: float = 1.0) -> bool:
        """Switch to a different model, optionally with a LoRA adapter merged
        at load (``adapter``: the adapter GGUF, ``scale``: its strength).

        Waits for any generation in flight (never frees weights under a
        running decode), then unloads and loads the new file. On failure the
        previous model (and its adapter) is restored so the engine never ends
        up empty.
        """
        target = Path(path)
        if not target.is_file():
            self._error = f"Model file not found: {target}"
            return False
        adapter_path = Path(adapter) if adapter is not None else None
        if adapter_path is not None and not adapter_path.is_file():
            self._error = f"Adapter file not found: {adapter_path}"
            return False
        with self._gen_lock:
            with self._lock:
                previous = self._model_path
                prev_adapter, prev_scale = self._adapter_path, self._adapter_scale
                self._destroy_locked()
                self._model_path = target
                self._adapter_path, self._adapter_scale = adapter_path, float(scale)
                if self.load():
                    return True
                failure = self._error
                if previous is not None and previous.is_file():
                    self._model_path = previous
                    self._adapter_path, self._adapter_scale = prev_adapter, prev_scale
                    if not self.load() and prev_adapter is not None:
                        self._adapter_path = None
                        self.load()
                self._error = failure
                return False

    # ------------------------------------------------------------------ info

    def health(self) -> bool:
        """Can this client serve? Does not load the weights (that is a user
        decision made in the Models tab, and polling status must not undo an
        idle unload)."""
        return self.configured

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
            "context_train": info.n_ctx_train,
            "vocab": info.vocab,
            "tied_output": bool(info.tied_output),
            "experts": info.n_experts,
            "experts_per_token": info.n_experts_per_tok,
            "quant": info.quant.decode("utf-8", "replace"),
            "chat_format": (info.chat_format or b"").decode("utf-8", "replace")
                           or (self._lib.fish_chat_format(self._ctx) or b"").decode(),
            "tokenizer": (info.tokenizer or b"").decode("utf-8", "replace"),
            "kernel": (self._lib.fish_kernel_name() or b"").decode(),
            "abi": self._abi,
            "adapter": self.adapter_info(),
        }

    def context_limit(self) -> int:
        """Tokens of context the loaded model uses; the configured value or
        4096 when nothing is loaded."""
        if self.available:
            info = FishModelInfo()
            if self._lib.fish_model_info_of(self._ctx, ctypes.byref(info)) == 0 and info.n_ctx > 0:
                return int(info.n_ctx)
        return int(getattr(self.config, "context_size", 0) or 4096)

    def specs(self) -> dict:
        if not self.load():
            return {"error": self._error}
        if not hasattr(self._lib, "fish_specs_json"):
            return {"error": "specs not supported by this engine build"}
        raw = self._lib.fish_specs_json(self._ctx)
        try:
            return json.loads(raw.decode("utf-8") if raw else "{}")
        except (ValueError, AttributeError):
            return {"error": "invalid specs JSON"}

    def stats(self) -> dict:
        if not self.available:
            return {"error": "model not loaded"}
        s = FishStats()
        self._lib.fish_get_stats(self._ctx, ctypes.byref(s))
        return {
            "load_seconds": s.load_seconds,
            "prefill_seconds": s.prefill_seconds,
            "decode_seconds": s.decode_seconds,
            "tokens_per_second": s.tokens_per_second,
            "prefill_tokens_per_second": s.prefill_tokens_per_second,
            "prompt_tokens": s.prompt_tokens,
            "cached_tokens": s.cached_tokens,
            "generated_tokens": s.generated_tokens,
            "context_used": s.context_used,
            "context_size": s.context_size,
            "truncated": bool(s.truncated),
            "weights_bytes": s.weights_bytes,
            "kv_bytes": s.kv_bytes,
            "threads": s.n_threads,
            "streaming": bool(s.streaming),
            "placement": {
                "gpu_layers": s.gpu_layers,
                "ram_layers": s.ram_layers,
                "disk_layers": s.disk_layers,
                "gpu_bytes": s.gpu_bytes,
                "gpu": (s.gpu_name or b"").decode("utf-8", "replace"),
                "mix": (s.gpu_mix or b"").decode("utf-8", "replace"),
                # ABI 4: the residency planner's decision (or why it did not
                # run), per-phase load seconds and live VRAM (any vendor).
                "planner": (s.placement or b"").decode("utf-8", "replace"),
                "load_profile": (s.load_profile or b"").decode("utf-8", "replace"),
                "vram_total_bytes": s.vram_total,
                "vram_free_bytes": s.vram_free,
                "vram_headroom_bytes": s.vram_headroom,
            },
            "speculative": {
                "active": bool(s.draft_active),
                "proposed": s.draft_proposed,
                "accepted": s.draft_accepted,
                "acceptance": (s.draft_accepted / s.draft_proposed) if s.draft_proposed else None,
            },
            "peak_rss_mb": s.peak_rss_mb or _peak_rss_mb(),
        }

    # ------------------------------------------------------------- prompting

    @staticmethod
    def _message_arrays(messages: Iterable[dict]):
        msgs = list(messages)
        n = len(msgs)
        roles = (ctypes.c_char_p * n)(*[str(m.get("role", "user")).encode("utf-8") for m in msgs])
        contents = (ctypes.c_char_p * n)(*[str(m.get("content", "")).encode("utf-8") for m in msgs])
        return roles, contents, n

    def count_tokens(self, messages: Iterable[dict]) -> int:
        """Tokens the conversation occupies after the model's template is
        applied. Falls back to a 4-chars-per-token estimate on old engines."""
        msgs = list(messages)
        if self.available and hasattr(self._lib, "fish_count_chat_tokens"):
            roles, contents, n = self._message_arrays(msgs)
            got = self._lib.fish_count_chat_tokens(self._ctx, roles, contents, n)
            if got >= 0:
                return int(got)
        return max(1, sum(len(str(m.get("content", ""))) for m in msgs) // 4)

    def chat_prompt(self, messages: Iterable[dict]) -> str:
        """Render messages with the model's own chat template (text form)."""
        if not self.load():
            raise RuntimeError(self._error or "FISH not loaded")
        roles, contents, n = self._message_arrays(messages)
        cap = 4096
        for _ in range(8):
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
        """Resolve sampling parameters: explicit value, else config, else default.

        ``x or default`` is wrong here: temperature 0 (greedy) is a real choice.
        """
        cfg = self.config

        def pick(name, default):
            value = getattr(params, name, None) if params is not None else None
            if value is None:
                value = getattr(cfg, name, None) if cfg is not None else None
            return default if value is None else value

        return {
            "temperature": float(pick("temperature", 0.7)),
            "top_p": float(pick("top_p", 0.9)),
            "max_tokens": int(pick("max_tokens", 2048)),
            "top_k": int(pick("top_k", 40)),
            "repeat_penalty": float(pick("repeat_penalty", self._default_repeat)),
        }

    def _raise_last_error(self, default: str) -> None:
        err = self._lib.fish_last_error(self._ctx) if self._ctx else None
        raise RuntimeError((err or default.encode()).decode("utf-8", "replace") if isinstance(err, bytes) else default)

    def _generate(self, prompt: str, params, callback) -> int:
        """Generate from a raw, already-rendered prompt (special tokens honoured)."""
        p = self._params(params)
        with self._gen_lock:
            if not self.load():
                raise RuntimeError(self._error or "FISH not loaded")
            self._last_used = time.time()
            self._begin_generation()
            try:
                rc = self._lib.fish_generate(
                    self._ctx, prompt.encode("utf-8"), 0,
                    p["max_tokens"], p["temperature"], p["top_k"], p["top_p"],
                    p["repeat_penalty"], callback, None,
                )
            finally:
                self._end_generation()
            self._last_used = time.time()
            if rc < 0:
                self._raise_last_error("generation failed")
        return rc

    def _generate_chat(self, messages: list[dict], params, callback) -> int:
        """Generate from a conversation using the model's template, applied
        structurally inside the engine (user text can never inject control
        tokens, and BOS is added exactly once)."""
        p = self._params(params)
        with self._gen_lock:
            if not self.load():
                raise RuntimeError(self._error or "FISH not loaded")
            self._last_used = time.time()
            self._begin_generation()
            try:
                if hasattr(self._lib, "fish_generate_chat"):
                    roles, contents, n = self._message_arrays(messages)
                    rc = self._lib.fish_generate_chat(
                        self._ctx, roles, contents, n,
                        p["max_tokens"], p["temperature"], p["top_k"], p["top_p"],
                        p["repeat_penalty"], callback, None,
                    )
                else:  # older engine: render text, template already carries BOS
                    prompt = self.chat_prompt(messages)
                    rc = self._lib.fish_generate(
                        self._ctx, prompt.encode("utf-8"), 0,
                        p["max_tokens"], p["temperature"], p["top_k"], p["top_p"],
                        p["repeat_penalty"], callback, None,
                    )
            finally:
                self._end_generation()
            self._last_used = time.time()
            if rc < 0:
                self._raise_last_error("generation failed")
        return rc

    def generate_full(self, prompt: str, params=None) -> str:
        chunks: list[str] = []

        def _cb(text, _token_id, _user):
            if text:
                chunks.append(text.decode("utf-8", "replace"))

        self._generate(prompt, params, FISH_TOKEN_CB(_cb))
        return "".join(chunks)

    def generate_chat_full(self, messages: list[dict], params=None) -> str:
        chunks: list[str] = []

        def _cb(text, _token_id, _user):
            if text:
                chunks.append(text.decode("utf-8", "replace"))

        self._generate_chat(list(messages), params, FISH_TOKEN_CB(_cb))
        return "".join(chunks)

    def cancel(self) -> None:
        if self._lib and self._ctx:
            self._lib.fish_cancel(self._ctx)

    async def _stream(self, work: Callable[[FISH_TOKEN_CB], int]) -> AsyncGenerator[str, None]:
        """Run `work` on a worker thread and yield chunks in order."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        done = object()

        def _cb(text, _token_id, _user):
            if text:
                loop.call_soon_threadsafe(queue.put_nowait, text.decode("utf-8", "replace"))

        callback = FISH_TOKEN_CB(_cb)

        def _work():
            try:
                work(callback)
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

    async def generate_stream(self, messages: Iterable[dict], params=None) -> AsyncGenerator[str, None]:
        """Stream decoded text for a conversation without blocking the event loop."""
        msgs = list(messages)
        async for chunk in self._stream(lambda cb: self._generate_chat(msgs, params, cb)):
            yield chunk

    async def generate_prompt_stream(self, prompt: str, params=None) -> AsyncGenerator[str, None]:
        """Stream decoded text for a raw prompt."""
        async for chunk in self._stream(lambda cb: self._generate(prompt, params, cb)):
            yield chunk
