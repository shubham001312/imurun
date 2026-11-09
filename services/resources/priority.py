"""Run IMURUN below normal priority while it computes.

The engine runs inside the server process, so while a reply is generated
every engine thread competes with the user's own programs at the same
priority. Lowering the *process* priority class for the duration of a
generation (and of an in-process voice or tuning step) lets the OS scheduler
hand the CPU to the user's foreground app first, so a busy IMURUN no longer
freezes the desktop; an idle machine still gives IMURUN every cycle it asks
for, so throughput on a calm machine is unchanged.

* Windows: ``SetPriorityClass(GetCurrentProcess(), BELOW_NORMAL)`` when the
  first job starts, the saved class is restored when the last one ends.
* POSIX: ``os.nice`` / ``setpriority`` +10. An unprivileged process cannot
  raise its priority back, so on POSIX the lowering is sticky for the rest
  of the run (reported as ``mode: "nice"``, ``sticky: true``).

Voice workers and trainers are child processes that already start below
normal (services/voice/worker.py, services/tuning/backend.py).
"""
from __future__ import annotations

import os
import sys
import threading

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
NORMAL_PRIORITY_CLASS = 0x00000020
NICE_STEP = 10


class WindowsBackend:
    mode = "below_normal"
    sticky = False

    def __init__(self, kernel32=None) -> None:
        if kernel32 is None:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.GetPriorityClass.argtypes = [ctypes.c_void_p]
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self._k = kernel32

    def lower(self):
        handle = self._k.GetCurrentProcess()
        saved = int(self._k.GetPriorityClass(handle) or NORMAL_PRIORITY_CLASS)
        if not self._k.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS):
            raise OSError("SetPriorityClass(BELOW_NORMAL) failed")
        return saved

    def restore(self, saved) -> None:
        handle = self._k.GetCurrentProcess()
        if not self._k.SetPriorityClass(handle, int(saved or NORMAL_PRIORITY_CLASS)):
            raise OSError("SetPriorityClass(restore) failed")


class PosixBackend:
    mode = "nice"
    sticky = True

    def __init__(self) -> None:
        self._done = False

    def lower(self):
        if not self._done:
            os.nice(NICE_STEP)
            self._done = True
        return None

    def restore(self, saved) -> None:
        # Raising priority back needs CAP_SYS_NICE; stay lowered.
        return None


def default_backend():
    try:
        if sys.platform == "win32":
            return WindowsBackend()
        if hasattr(os, "nice"):
            return PosixBackend()
    except Exception:  # noqa: BLE001
        return None
    return None


class PriorityGuard:
    """Reference-counted "lower while any job runs" switch. Holders are job
    names (chat / voice / tuning); acquiring twice under one name counts once.
    Never raises: a failed call is recorded in ``error``."""

    def __init__(self, backend="default", enabled: bool = True, on_change=None) -> None:
        self._backend = default_backend() if backend == "default" else backend
        self._enabled = bool(enabled)
        self._holders: set[str] = set()
        self._lowered = False
        self._saved = None
        self._error = ""
        self._lock = threading.Lock()
        self._on_change = on_change

    @property
    def lowered(self) -> bool:
        return self._lowered

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            changed = self._sync_locked()
        self._notify(changed)

    def acquire(self, name: str) -> None:
        with self._lock:
            self._holders.add(name)
            changed = self._sync_locked()
        self._notify(changed)

    def release(self, name: str) -> None:
        with self._lock:
            self._holders.discard(name)
            changed = self._sync_locked()
        self._notify(changed)

    def _sync_locked(self) -> bool:
        want = self._enabled and bool(self._holders) and self._backend is not None
        if want and not self._lowered:
            try:
                self._saved = self._backend.lower()
                self._lowered, self._error = True, ""
                return True
            except Exception as exc:  # noqa: BLE001 - priority is best effort
                self._error = f"lower priority: {exc}"
                return False
        if not want and self._lowered and not getattr(self._backend, "sticky", False):
            try:
                self._backend.restore(self._saved)
                self._error = ""
            except Exception as exc:  # noqa: BLE001
                self._error = f"restore priority: {exc}"
            self._lowered = False
            return True
        return False

    def _notify(self, changed: bool) -> None:
        if changed and self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001
                pass

    def view(self) -> dict:
        with self._lock:
            if self._backend is None:
                mode = "unsupported"
            elif not self._enabled:
                mode = "off"
            else:
                mode = getattr(self._backend, "mode", "unknown")
            return {"enabled": self._enabled, "lowered": self._lowered, "mode": mode,
                    "sticky": bool(getattr(self._backend, "sticky", False)),
                    "holders": sorted(self._holders), "error": self._error}
