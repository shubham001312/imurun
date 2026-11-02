#!/usr/bin/env python3
"""IMURUN one-command installer (Windows / macOS / Linux).

    python install.py              # create/repair .venv and install everything needed
    python install.py --here       # install into the interpreter running this script
    python install.py --no-window  # skip the optional native-window package (pywebview)
    python install.py --dev        # also install requirements-dev.txt
    python install.py --voice      # also install requirements-voice.txt (server STT/TTS)
    python install.py --check      # only report what is missing, change nothing
    python install.py --verbose    # stream raw pip output instead of the spinner

Standard library only: this file must run before any dependency is installed.
``start.py`` imports the helpers below, so keep them free of side effects.

The FISH engine (a C++ shared library) is NOT built here; this script only
detects it (and the build tools) and prints build instructions.
``download_engine`` fetches a prebuilt library from the GitHub release when
asked (``imurun --get-engine``), checked against the release's SHA256SUMS.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# In an installed wheel this file lives in site-packages/imurun/ next to the
# package's __init__.py; nothing may be written there (no .venv, no logs).
INSTALLED = (ROOT / "__init__.py").is_file() and (ROOT / "cli.py").is_file()
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
REQUIREMENTS_DEV = ROOT / "requirements-dev.txt"
REQUIREMENTS_VOICE = ROOT / "requirements-voice.txt"
LOG_DIR = Path.home() / ".imurun" / "logs"

# import name -> pip distribution name. Keep in sync with requirements.txt.
REQUIRED_MODULES: dict[str, str] = {
    "aiohttp": "aiohttp",
    "httpx": "httpx",
    "huggingface_hub": "huggingface_hub",
    "yaml": "pyyaml",
}
WINDOW_MODULE = ("webview", "pywebview")
WINDOW_REQUIREMENT = "pywebview>=5.0"

MIN_PY = (3, 11)
# Newest minor version we expect binary wheels for (aiohttp has C extensions).
MAX_TESTED_PY = (3, 14)

IS_WINDOWS = os.name == "nt"

# Block-letter word mark. "#" becomes a full block on Unicode terminals.
LOGO_WORD = "IMUTECHZ"
LOGO_RED = {"I", "T"}
LOGO_FONT = {
    "I": ["###", " # ", " # ", " # ", "###"],
    "M": ["#   #", "## ##", "# # #", "#   #", "#   #"],
    "U": ["#   #", "#   #", "#   #", "#   #", " ### "],
    "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
    "E": ["####", "#   ", "### ", "#   ", "####"],
    "C": [" ####", "#    ", "#    ", "#    ", " ####"],
    "H": ["#   #", "#   #", "#####", "#   #", "#   #"],
    "Z": ["#####", "   # ", "  #  ", " #   ", "#####"],
}


# --------------------------------------------------------------------------- #
# Terminal UI: colours, symbols, spinner, boxes
# --------------------------------------------------------------------------- #
def _enable_windows_vt() -> bool:
    if not IS_WINDOWS:
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # VT processing
    except Exception:  # noqa: BLE001
        return False


class UI:
    def __init__(self) -> None:
        out = sys.stdout
        self.tty = bool(getattr(out, "isatty", lambda: False)())
        self.color = self.tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb" and _enable_windows_vt()
        enc = (getattr(out, "encoding", "") or "").lower().replace("-", "")
        self.unicode = enc.startswith("utf")
        if self.unicode:
            self.sym = {"ok": "\u2714", "fail": "\u2716", "warn": "\u25b2", "info": "\u2022",
                        "arrow": "\u203a", "dot": "\u00b7"}
            self.frames = ["\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834",
                           "\u2826", "\u2827", "\u2807", "\u280f"]
            self.box_chars = ("\u256d", "\u256e", "\u2570", "\u256f", "\u2500", "\u2502")
        else:
            self.sym = {"ok": "OK", "fail": "X", "warn": "!", "info": "*", "arrow": ">", "dot": "-"}
            self.frames = ["|", "/", "-", "\\"]
            self.box_chars = ("+", "+", "+", "+", "-", "|")

    # colours ---------------------------------------------------------------
    def c(self, text: str, *codes: str) -> str:
        if not self.color:
            return text
        table = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
                 "blue": "34", "magenta": "35", "cyan": "36", "grey": "90"}
        seq = ";".join(table[c] for c in codes if c in table)
        return f"\x1b[{seq}m{text}\x1b[0m" if seq else text

    @staticmethod
    def width() -> int:
        return max(40, min(shutil.get_terminal_size((80, 24)).columns, 100))

    # lines -----------------------------------------------------------------
    def line(self, text: str = "") -> None:
        print(text, flush=True)

    def ok(self, text: str) -> None:
        self.line(f"  {self.c(self.sym['ok'], 'green', 'bold')} {text}")

    def fail(self, text: str) -> None:
        self.line(f"  {self.c(self.sym['fail'], 'red', 'bold')} {text}")

    def warn(self, text: str) -> None:
        self.line(f"  {self.c(self.sym['warn'], 'yellow', 'bold')} {text}")

    def info(self, text: str) -> None:
        self.line(f"  {self.c(self.sym['info'], 'cyan')} {text}")

    def hint(self, text: str) -> None:
        self.line(f"      {self.c(text, 'grey')}")

    def step(self, index: int, total: int, title: str) -> None:
        self.line()
        self.line(f"  {self.c(f'[{index}/{total}]', 'magenta', 'bold')} {self.c(title, 'bold')}")

    def box(self, title: str, lines: list[str], colour: str = "cyan") -> None:
        tl, tr, bl, br, h, v = self.box_chars
        inner = min(self.width() - 6, max([len(title) + 2] + [len(x) + 2 for x in lines]))
        self.line("  " + self.c(tl + h * inner + tr, colour))
        self.line("  " + self.c(v, colour) + " " + self.c(title, "bold") + " " * (inner - len(title) - 1) + self.c(v, colour))
        for text in lines:
            text = text[: inner - 2]
            self.line("  " + self.c(v, colour) + " " + text + " " * (inner - len(text) - 1) + self.c(v, colour))
        self.line("  " + self.c(bl + h * inner + br, colour))

    def logo(self) -> None:
        """The ImuTechz word mark, large, with the I and the T in red."""
        fill = "█" if self.unicode else "#"
        rows = []
        for r in range(5):
            parts = []
            for letter in LOGO_WORD:
                glyph = LOGO_FONT[letter][r].replace("#", fill)
                colour = ("red", "bold") if letter in LOGO_RED else ("bold",)
                parts.append(self.c(glyph, *colour))
            rows.append("  " + " ".join(parts))
        self.line()
        for row in rows:
            self.line(row)
            if self.tty:
                time.sleep(0.05)  # a short top-to-bottom reveal

    def banner(self, subtitle: str) -> None:
        self.logo()
        tag = f"IMURUN {self.sym['dot']} local AI chat {self.sym['dot']} FISH engine"
        self.line()
        self.line("  " + self.c(tag, "cyan", "bold"))
        self.line("  " + self.c(subtitle, "grey"))
        width = sum(len(LOGO_FONT[ch][0]) for ch in LOGO_WORD) + len(LOGO_WORD) - 1
        self.line("  " + self.c(self.box_chars[4] * width, "grey"))

    def ask(self, question: str, default: bool = True) -> bool:
        if not sys.stdin or not sys.stdin.isatty():
            return default
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            answer = input(f"  {self.c('?', 'cyan', 'bold')} {question} {self.c(suffix, 'grey')} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            self.line()
            return False
        if not answer:
            return default
        return answer in ("y", "yes")


ui = UI()


class Spinner:
    """An animated status line; degrades to plain lines when not a terminal."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.detail = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = time.monotonic()
        self._lock = threading.Lock()
        self._last_plain = ""

    def __enter__(self) -> "Spinner":
        self._start = time.monotonic()
        if ui.tty:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        else:
            ui.line(f"  {ui.sym['arrow']} {self.text} ...")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._halt()
        if exc_type is not None and exc_type is not SystemExit:
            ui.fail(f"{self.text} {ui.c('(interrupted)', 'grey')}")

    def update(self, detail: str) -> None:
        with self._lock:
            self.detail = detail
        if not ui.tty and detail and detail != self._last_plain:
            self._last_plain = detail

    def _render(self, frame: str) -> str:
        elapsed = time.monotonic() - self._start
        with self._lock:
            detail = self.detail
        text = f"  {ui.c(frame, 'cyan', 'bold')} {self.text}"
        plain_len = 4 + len(self.text)
        if detail:
            room = ui.width() - plain_len - 12
            if room > 8:
                d = detail if len(detail) <= room else detail[: room - 1] + "\u2026" if ui.unicode else detail[: room - 3] + "..."
                text += " " + ui.c(f"{ui.sym['dot']} {d}", "grey")
        text += " " + ui.c(f"{elapsed:4.0f}s", "grey")
        return text

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            frame = ui.frames[i % len(ui.frames)]
            sys.stdout.write("\r\x1b[2K" + self._render(frame) if ui.color else "\r" + self._render(frame)[: ui.width() - 1].ljust(ui.width() - 1))
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.08)

    def _halt(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join()
            self._thread = None
            sys.stdout.write("\r\x1b[2K" if ui.color else "\r" + " " * (ui.width() - 1) + "\r")
            sys.stdout.flush()

    def done(self, text: str | None = None, ok: bool = True) -> None:
        self._halt()
        elapsed = time.monotonic() - self._start
        msg = f"{text or self.text} {ui.c(f'({elapsed:.1f}s)', 'grey')}"
        (ui.ok if ok else ui.fail)(msg)


def say(msg: str = "") -> None:
    ui.line(msg)


def warn(msg: str) -> None:
    ui.warn(msg)


def _run_quiet(cmd: list[str], timeout: float = 120) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None


# --------------------------------------------------------------------------- #
# Virtual environment helpers
# --------------------------------------------------------------------------- #
def venv_python(venv: Path = VENV) -> Path:
    if IS_WINDOWS:
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def in_venv(venv: Path = VENV) -> bool:
    """True when the running interpreter is this repo's .venv."""
    try:
        return Path(sys.prefix).resolve() == venv.resolve()
    except OSError:
        return False


def _read_pyvenv_cfg(venv: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                out[key.strip().lower()] = value.strip()
    except OSError:
        pass
    return out


def venv_problem(venv: Path = VENV) -> str | None:
    """Why ``venv`` is unusable, or None when it is healthy (or absent).

    A venv copied from another machine still contains ``Scripts/python.exe``
    on Windows, but that launcher only redirects to ``home=`` from
    ``pyvenv.cfg``; if that base install is gone the venv is dead. So we check
    the ``home`` path as well as the binary, and finally try to run it.
    """
    if not venv.exists():
        return None
    cfg = _read_pyvenv_cfg(venv)
    if not cfg:
        return f"{venv / 'pyvenv.cfg'} is missing or unreadable"
    home = cfg.get("home")
    if home and not Path(home).is_dir():
        return f"its base Python ({home}) no longer exists"
    exe = cfg.get("executable")
    if exe and not Path(exe).exists():
        return f"its base Python executable ({exe}) no longer exists"
    py = venv_python(venv)
    if not py.exists():
        return f"{py} does not exist"
    proc = _run_quiet([str(py), "-c", "import sys"], timeout=60)
    if proc is None or proc.returncode != 0:
        detail = "" if proc is None else (proc.stderr or proc.stdout).strip()[:200]
        return f"{py} fails to start {detail}".strip()
    return None


def venv_healthy(venv: Path = VENV) -> bool:
    return venv.exists() and venv_problem(venv) is None


def remove_broken_venv(venv: Path = VENV) -> bool:
    """Delete ``venv`` only if it is confirmed broken. Returns True if removed."""
    problem = venv_problem(venv)
    if problem is None:
        return False
    # Safety: only ever delete something that really is a virtual environment.
    if not ((venv / "pyvenv.cfg").exists() or (venv / "Scripts").exists() or (venv / "bin").exists()):
        warn(f"{venv} does not look like a virtual environment; not deleting it.")
        return False
    ui.warn(f"Stale .venv found: {problem}")
    with Spinner("Removing the stale .venv (it will be recreated)") as sp:
        shutil.rmtree(venv, ignore_errors=True)
        if venv.exists():
            # Windows may hold a lock on a file; move it aside instead.
            aside = venv.with_name(".venv.broken")
            shutil.rmtree(aside, ignore_errors=True)
            try:
                venv.rename(aside)
            except OSError as exc:
                sp.done("Could not remove the stale .venv", ok=False)
                raise SystemExit(f"Cannot remove broken {venv}: {exc}. Delete it manually and retry.")
        sp.done("Removed the stale .venv")
    return True


def _version_of(python: str | Path) -> tuple[int, int] | None:
    proc = _run_quiet([str(python), "-c", "import sys; print('%d %d' % sys.version_info[:2])"], timeout=60)
    if proc is None or proc.returncode != 0:
        return None
    try:
        major, minor = (int(x) for x in proc.stdout.split())
    except ValueError:
        return None
    return major, minor


def _good_version(v: tuple[int, int] | None) -> bool:
    return v is not None and MIN_PY <= v <= MAX_TESTED_PY


def find_base_python(explicit: str | None = None) -> str:
    """Pick an interpreter to build the venv from.

    Preference: --python, the running interpreter if its version is in the
    supported range, then other installed versions (newest supported first),
    then the running interpreter anyway.
    """
    if explicit:
        return explicit
    current = (sys.version_info.major, sys.version_info.minor)
    if _good_version(current) and not in_venv():
        return sys.executable

    candidates: list[str] = []
    minors = range(MAX_TESTED_PY[1], MIN_PY[1] - 1, -1)
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        for m in minors:
            if local:
                candidates.append(str(Path(local) / "Programs" / "Python" / f"Python3{m}" / "python.exe"))
            candidates.append(rf"C:\Program Files\Python3{m}\python.exe")
    else:
        for m in minors:
            found = shutil.which(f"python3.{m}")
            if found:
                candidates.append(found)
        found = shutil.which("python3")
        if found:
            candidates.append(found)
    for exe in candidates:
        if Path(exe).exists() and _good_version(_version_of(exe)):
            return exe
    if IS_WINDOWS and shutil.which("py"):
        for m in minors:
            proc = _run_quiet(["py", f"-3.{m}", "-c", "import sys; print(sys.executable)"], timeout=60)
            if proc is not None and proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
    if not _good_version(current):
        warn(
            f"Python {current[0]}.{current[1]} is outside the tested range "
            f"{MIN_PY[0]}.{MIN_PY[1]}-{MAX_TESTED_PY[0]}.{MAX_TESTED_PY[1]}; "
            "some packages may need to compile from source."
        )
        if IS_WINDOWS:
            ui.hint("Install a supported one with:  winget install -e --id Python.Python.3.12")
    return getattr(sys, "_base_executable", None) or sys.executable


def create_venv(base_python: str, venv: Path = VENV) -> Path:
    remove_broken_venv(venv)
    if venv_healthy(venv):
        ui.ok(f"Using existing .venv {ui.c(str(venv_python(venv)), 'grey')}")
        return venv_python(venv)
    version = _version_of(base_python)
    label = f"Python {version[0]}.{version[1]}" if version else base_python
    with Spinner(f"Creating .venv with {label}") as sp:
        proc = _run_quiet([base_python, "-m", "venv", str(venv)], timeout=600)
        if proc is None or proc.returncode != 0:
            sp.done("Could not create .venv", ok=False)
            if proc is not None and (proc.stderr or proc.stdout):
                ui.hint((proc.stderr or proc.stdout).strip().splitlines()[-1])
            if sys.platform.startswith("linux"):
                ui.hint("Debian/Ubuntu: sudo apt install python3-venv   then run this again")
            raise SystemExit(1)
        sp.done(f"Created .venv with {label}")
    return venv_python(venv)


# --------------------------------------------------------------------------- #
# Package helpers
# --------------------------------------------------------------------------- #
def missing_modules(python: str | Path | None = None, modules: dict[str, str] | None = None) -> list[str]:
    """Return the pip names of required modules that cannot be imported."""
    modules = modules or REQUIRED_MODULES
    if python is None or Path(python) == Path(sys.executable):
        import importlib
        import importlib.util

        importlib.invalidate_caches()
        missing = []
        for mod, dist in modules.items():
            try:
                if importlib.util.find_spec(mod) is None:
                    missing.append(dist)
            except (ImportError, ValueError):
                missing.append(dist)
        return missing
    code = (
        "import importlib.util\n"
        f"mods = {dict(modules)!r}\n"
        "print(' '.join(d for m, d in mods.items() if importlib.util.find_spec(m) is None))\n"
    )
    proc = _run_quiet([str(python), "-c", code], timeout=120)
    if proc is None or proc.returncode != 0:
        return list(modules.values())
    return proc.stdout.split()


def installed_versions(python: str | Path, dists: list[str]) -> dict[str, str]:
    code = (
        "import importlib.metadata as m, json, sys\n"
        "out = {}\n"
        "for d in sys.argv[1:]:\n"
        "    try: out[d] = m.version(d)\n"
        "    except Exception: out[d] = ''\n"
        "print(json.dumps(out))\n"
    )
    proc = _run_quiet([str(python), "-c", code, *dists], timeout=60)
    if proc is None or proc.returncode != 0:
        return {}
    import json

    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {}


def _ensure_pip(python: str) -> None:
    proc = _run_quiet([python, "-m", "pip", "--version"], timeout=60)
    if proc is not None and proc.returncode == 0:
        return
    with Spinner("pip is missing - bootstrapping it with ensurepip") as sp:
        proc = _run_quiet([python, "-m", "ensurepip", "--upgrade"], timeout=600)
        if proc is None or proc.returncode != 0:
            sp.done("Could not bootstrap pip", ok=False)
            if sys.platform.startswith("linux"):
                ui.hint("Debian/Ubuntu: sudo apt install python3-pip python3-venv")
            raise SystemExit(1)
        sp.done("pip is ready")


def network_ok(host: str = "pypi.org", port: int = 443, timeout: float = 5.0) -> bool:
    if os.environ.get("PIP_INDEX_URL") or os.environ.get("PIP_NO_INDEX") or os.environ.get("HTTPS_PROXY"):
        return True  # custom index / proxy: let pip decide
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_LOG_PATH: Path | None = None


def _log_path() -> Path:
    global _LOG_PATH
    if _LOG_PATH is None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            _LOG_PATH = LOG_DIR / "install.log"
        except OSError:
            import tempfile  # noqa: PLC0415 - never write into an installed package

            _LOG_PATH = (Path(tempfile.gettempdir()) if INSTALLED else ROOT) / "imurun-install.log"
    return _LOG_PATH


def _pip_hints(tail: str) -> list[str]:
    low = tail.lower()
    hints = []
    if any(s in low for s in ("could not resolve", "connectionerror", "connection refused",
                              "temporary failure in name resolution", "max retries exceeded",
                              "ssl", "proxy")):
        hints.append("Network problem: check your internet / proxy (set HTTPS_PROXY) and retry.")
    if any(s in low for s in ("microsoft visual c++", "error: command 'gcc'", "failed building wheel",
                              "no matching distribution", "requires-python")):
        hints.append("No pre-built wheel for this Python: use Python 3.12 (winget install -e --id Python.Python.3.12,")
        hints.append("brew install python@3.12, or your distro package) and run the installer again.")
    if "permission" in low or "access is denied" in low:
        hints.append("Permission problem: use the .venv (python install.py) instead of --here,")
        hints.append("or close any running IMURUN window that may be locking files.")
    return hints


def run_pip(python: str | Path, pip_args: list[str], label: str, *, verbose: bool = False) -> bool:
    """Run ``python -m pip <pip_args>`` behind an animated status line."""
    python = str(python)
    cmd = [python, "-m", "pip", *pip_args, "--disable-pip-version-check", "--no-input"]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PIP_NO_COLOR="1")
    log_path = _log_path()
    tail: list[str] = []
    stats = {"downloaded": 0, "cached": 0, "satisfied": 0}
    installed: list[str] = []

    with open(log_path, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
        if verbose:
            ui.info(label)
            proc = subprocess.run(cmd, env=env)
            return proc.returncode == 0
        with Spinner(label) as sp:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                        text=True, encoding="utf-8", errors="replace", bufsize=1)
            except OSError as exc:
                sp.done(f"{label}: cannot run pip ({exc})", ok=False)
                return False
            assert proc.stdout is not None
            for raw in proc.stdout:
                log.write(raw)
                line = raw.strip()
                if not line:
                    continue
                tail.append(line)
                del tail[:-25]
                m = re.match(r"Collecting (\S+)", line)
                if m:
                    sp.update(f"resolving {m.group(1)}")
                    continue
                m = re.match(r"Downloading (\S+)(?: \((.+)\))?", line)
                if m:
                    stats["downloaded"] += 1
                    name = m.group(1).rsplit("/", 1)[-1].split("-")[0]
                    size = f" ({m.group(2)})" if m.group(2) else ""
                    sp.update(f"downloading {name}{size} {ui.sym['dot']} {stats['downloaded']} fetched")
                    continue
                m = re.match(r"Using cached (\S+)", line)
                if m:
                    stats["cached"] += 1
                    sp.update(f"cached {m.group(1).split('-')[0]}")
                    continue
                if line.startswith("Requirement already satisfied"):
                    stats["satisfied"] += 1
                    sp.update(f"{stats['satisfied']} already installed")
                    continue
                m = re.match(r"Installing collected packages: (.*)", line)
                if m:
                    n = len([x for x in m.group(1).split(",") if x.strip()])
                    sp.update(f"installing {n} package{'s' if n != 1 else ''}")
                    continue
                m = re.match(r"Successfully installed (.*)", line)
                if m:
                    installed = m.group(1).split()
            rc = proc.wait()
            if rc == 0:
                if installed:
                    sp.done(f"{label} {ui.c(f'- {len(installed)} new', 'grey')}")
                else:
                    sp.done(f"{label} {ui.c('- already up to date', 'grey')}")
            else:
                sp.done(f"{label} failed (exit {rc})", ok=False)
    if rc != 0:
        ui.line()
        for line in tail[-12:]:
            ui.hint(line)
        for h in _pip_hints("\n".join(tail)):
            ui.warn(h)
        ui.hint(f"Full log: {log_path}")
        return False
    return True


def pip_install(python: str | Path, *, window: bool = False, dev: bool = False,
                upgrade_pip: bool = True, verbose: bool = False, voice: bool = False) -> None:
    python = str(python)
    _ensure_pip(python)
    if not network_ok():
        ui.warn("pypi.org is not reachable - downloads will probably fail.")
        ui.hint("Check your internet connection or proxy settings (HTTPS_PROXY).")
    if upgrade_pip:
        run_pip(python, ["install", "--upgrade", "pip"], "Updating pip", verbose=verbose)
    args = ["install", "-r", str(REQUIREMENTS)]
    if dev and REQUIREMENTS_DEV.exists():
        args += ["-r", str(REQUIREMENTS_DEV)]
    if voice and REQUIREMENTS_VOICE.exists():
        args += ["-r", str(REQUIREMENTS_VOICE)]
    if not run_pip(python, args, "Installing IMURUN packages", verbose=verbose):
        raise SystemExit(1)
    versions = installed_versions(python, list(REQUIRED_MODULES.values()))
    for dist in REQUIRED_MODULES.values():
        v = versions.get(dist, "")
        if v:
            ui.line(f"      {ui.c(ui.sym['ok'], 'green')} {dist} {ui.c(v, 'grey')}")
        else:
            ui.line(f"      {ui.c(ui.sym['fail'], 'red')} {dist} {ui.c('missing', 'red')}")
    if window:
        if run_pip(python, ["install", WINDOW_REQUIREMENT], "Installing native window support (pywebview)",
                   verbose=verbose):
            pass
        else:
            ui.warn("pywebview is optional - IMURUN will open in your browser instead.")


def window_supported_by_default() -> bool:
    """pywebview needs system GUI libraries on Linux, so it is opt-in there."""
    return sys.platform in ("win32", "darwin")


# --------------------------------------------------------------------------- #
# Engine detection
# --------------------------------------------------------------------------- #
def engine_library_names() -> list[str]:
    if sys.platform == "win32":
        return ["fish.dll"]
    if sys.platform == "darwin":
        return ["libfish.dylib"]
    return ["libfish.so", "fish.so"]


def data_root() -> Path:
    """The user data root (``~/.imurun`` unless IMURUN_DATA_ROOT says otherwise)."""
    env = os.environ.get("IMURUN_DATA_ROOT")
    return Path(env).expanduser() if env else Path.home() / ".imurun"


def user_engine_dir() -> Path:
    """Where ``download_engine`` puts the library (outside site-packages)."""
    return data_root() / "bin"


def find_engine_library() -> Path | None:
    env = os.environ.get("FISH_LIBRARY")
    if env:
        p = Path(env)
        return p if p.exists() else None
    for base in (ROOT / "bin", ROOT / "model" / "fish" / "build", user_engine_dir()):
        for name in engine_library_names():
            p = base / name
            if p.exists():
                return p
    return None


# --------------------------------------------------------------------------- #
# Prebuilt engine from the GitHub release
# --------------------------------------------------------------------------- #
RELEASES_URL = "https://github.com/shubham001312/imurun/releases"
CHECKSUMS_ASSET = "SHA256SUMS.txt"


def engine_asset_name() -> str | None:
    """Release asset for this OS/CPU, or None when CI does not build one."""
    machine = platform.machine().lower()
    x64 = machine in ("x86_64", "amd64")
    arm64 = machine in ("arm64", "aarch64")
    if sys.platform == "win32" and x64:
        return "fish-windows-x64.dll"
    if sys.platform == "darwin" and arm64:
        return "libfish-macos-arm64.dylib"
    if sys.platform.startswith("linux") and x64:
        return "libfish-linux-x64.so"
    return None


def app_version() -> str:
    """The IMURUN version (pyproject.toml in a checkout, wheel metadata when installed)."""
    try:
        if not INSTALLED and str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from imurun import __version__  # noqa: PLC0415

        return __version__
    except Exception:  # noqa: BLE001
        return "0+unknown"


def download_engine(version: str | None = None, dest_dir: Path | None = None,
                    timeout: float = 60.0) -> Path:
    """Download this platform's engine from GitHub release ``v<version>``.

    The file is verified against the release's SHA256SUMS.txt before it is
    moved into place; every failure raises RuntimeError with a readable message.
    """
    import hashlib  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    asset = engine_asset_name()
    if asset is None:
        raise RuntimeError(
            f"no prebuilt FISH engine is published for {platform.system()} {platform.machine()}; "
            "build it from source (docs/engine/ARCHITECTURE.md, or `python install.py --check`)")
    version = (version or app_version()).lstrip("v")
    base = f"{RELEASES_URL}/download/v{version}"
    dest_dir = dest_dir or user_engine_dir()
    target = dest_dir / engine_library_names()[0]

    def fetch(name: str) -> bytes:
        req = urllib.request.Request(f"{base}/{name}", headers={"User-Agent": f"imurun/{version}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"could not download {base}/{name}: {exc}") from exc

    sums = fetch(CHECKSUMS_ASSET).decode("utf-8", "replace")
    expected = None
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset:
            expected = parts[0].lower()
    if not expected:
        raise RuntimeError(f"{asset} is not listed in {base}/{CHECKSUMS_ASSET}")
    data = fetch(asset)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(f"checksum mismatch for {asset}: expected {expected}, got {actual}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(data)
    os.replace(partial, target)
    return target


def build_tools() -> list[tuple[str, bool, str]]:
    """(tool, present, how-to-install) for the engine build toolchain.

    MSVC is not supported: the kernels use GCC target attributes, so Windows
    builds with MinGW-w64 GCC (WinLibs or MSYS2 UCRT64).
    """
    if sys.platform == "win32":
        return [
            ("cmake", bool(shutil.which("cmake")), "winget install -e --id Kitware.CMake"),
            ("ninja", bool(shutil.which("ninja")), "winget install -e --id Ninja-build.Ninja"),
            ("MinGW-w64 g++", bool(shutil.which("g++")),
             "winget install -e --id BrechtSanders.WinLibs.POSIX.UCRT  (then open a new terminal)"),
        ]
    if sys.platform == "darwin":
        return [
            ("cmake", bool(shutil.which("cmake")), "brew install cmake"),
            ("ninja", bool(shutil.which("ninja")), "brew install ninja"),
            ("clang++", bool(shutil.which("clang++")), "xcode-select --install"),
        ]
    return [
        ("cmake", bool(shutil.which("cmake")), "sudo apt install cmake   (dnf/pacman: cmake)"),
        ("ninja", bool(shutil.which("ninja")), "sudo apt install ninja-build"),
        ("C++ compiler", bool(shutil.which("g++") or shutil.which("clang++")),
         "sudo apt install build-essential"),
    ]


def engine_build_instructions() -> str:
    lib = engine_library_names()[0]
    lines = [
        "Build the FISH engine with CMake + Ninja:",
        "    cd model/fish",
        "    cmake -G Ninja -B build -DCMAKE_BUILD_TYPE=Release",
        "    ninja -C build",
        f"then copy build/{lib} to bin/{lib} (or set FISH_LIBRARY to its full path).",
    ]
    if sys.platform == "win32":
        lines.append("Windows: use MinGW-w64 GCC (g++ on PATH); MSVC is not supported.")
    if INSTALLED:
        lines = [
            "This installed copy has no FISH engine for this platform.",
            "Download the prebuilt one:  imurun --get-engine",
            f"  (or fetch it from {RELEASES_URL} and set FISH_LIBRARY to its path),",
            "or build it from source: "
            "https://github.com/shubham001312/imurun/blob/main/docs/engine/ARCHITECTURE.md",
        ]
    lines.append("IMURUN still starts without the engine; chat is unavailable until it exists.")
    return "\n".join(lines)


def report_engine() -> bool:
    lib = find_engine_library()
    if lib:
        ui.ok(f"FISH engine library found {ui.c(str(lib), 'grey')}")
        return True
    ui.warn("FISH engine library not found (" + " / ".join(engine_library_names()) + ")")
    for name, present, how in build_tools():
        if present:
            ui.line(f"      {ui.c(ui.sym['ok'], 'green')} {name}")
        else:
            ui.line(f"      {ui.c(ui.sym['fail'], 'red')} {name}  {ui.c(how, 'grey')}")
    for line in engine_build_instructions().splitlines():
        ui.hint(line)
    return False


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def os_label() -> str:
    system = {"Darwin": "macOS"}.get(platform.system(), platform.system())
    release = platform.mac_ver()[0] if system == "macOS" else platform.release()
    return f"{system} {release} {platform.machine()}".strip()


def _check(args: argparse.Namespace, target: Path | None) -> int:
    ui.step(1, 3, "Python environment")
    problem = venv_problem()
    if problem:
        ui.fail(f".venv is broken: {problem}")
        ui.hint("python install.py   will recreate it")
    elif VENV.exists():
        ui.ok(f".venv {ui.c(str(venv_python()), 'grey')}")
    else:
        ui.warn(".venv not created yet")
    ui.step(2, 3, "Packages")
    check_py = target or Path(sys.executable)
    miss = missing_modules(check_py)
    versions = installed_versions(check_py, list(REQUIRED_MODULES.values()))
    for dist in REQUIRED_MODULES.values():
        if dist in miss:
            ui.fail(f"{dist} {ui.c('missing', 'red')}")
        else:
            ui.ok(f"{dist} {ui.c(versions.get(dist, ''), 'grey')}")
    if missing_modules(check_py, {WINDOW_MODULE[0]: WINDOW_MODULE[1]}):
        ui.info(f"pywebview not installed {ui.c('(optional: native window)', 'grey')}")
    else:
        ui.ok(f"pywebview {ui.c('(native window)', 'grey')}")
    ui.step(3, 3, "FISH engine")
    report_engine()
    ui.line()
    return 0 if not miss else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install IMURUN's Python dependencies.")
    parser.add_argument("--here", action="store_true",
                        help="install into the current interpreter instead of .venv")
    parser.add_argument("--python", default=None, help="base interpreter used to create .venv")
    win = parser.add_mutually_exclusive_group()
    win.add_argument("--window", action="store_true", help="install pywebview (native window)")
    win.add_argument("--no-window", action="store_true", help="do not install pywebview")
    parser.add_argument("--dev", action="store_true", help="also install requirements-dev.txt")
    parser.add_argument("--voice", action="store_true",
                        help="also install requirements-voice.txt (faster-whisper, piper-tts)")
    parser.add_argument("--check", action="store_true", help="only report status; install nothing")
    parser.add_argument("--verbose", action="store_true", help="show raw pip output")
    parser.add_argument("--quiet-if-ok", action="store_true",
                        help="exit silently with 0 when everything is already installed")
    args = parser.parse_args(argv)

    if sys.version_info < MIN_PY:
        ui.fail(f"IMURUN needs Python {MIN_PY[0]}.{MIN_PY[1]}+; this is {sys.version.split()[0]}.")
        return 1

    if args.here or in_venv():
        target: Path | None = Path(sys.executable)
    else:
        target = venv_python() if venv_healthy() else None

    if args.quiet_if_ok and target is not None and not missing_modules(target):
        return 0

    ui.banner(f"setup {ui.sym['dot']} {os_label()} {ui.sym['dot']} Python {sys.version.split()[0]}")

    if args.check:
        return _check(args, target)

    window = args.window or (not args.no_window and window_supported_by_default())
    total = 4
    started = time.monotonic()

    ui.step(1, total, "System check")
    ui.ok(f"{os_label()}")
    ui.ok(f"Python {sys.version.split()[0]} {ui.c(sys.executable, 'grey')}")
    if network_ok():
        ui.ok("Internet connection to pypi.org")
    else:
        ui.warn("Cannot reach pypi.org - package downloads may fail")

    ui.step(2, total, "Python environment")
    if target is None:
        target = create_venv(find_base_python(args.python))
    else:
        ui.ok(f"Using {ui.c(str(target), 'grey')}")

    ui.step(3, total, "Packages")
    try:
        pip_install(target, window=window, dev=args.dev, verbose=args.verbose, voice=args.voice)
    except SystemExit:
        ui.line()
        ui.fail("Setup did not finish. Fix the problem above and run the installer again.")
        return 1
    miss = missing_modules(target)
    if miss:
        ui.fail(f"Still missing after install: {', '.join(miss)}")
        return 1

    ui.step(4, total, "FISH engine")
    report_engine()

    ui.line()
    if sys.platform == "win32":
        run = "run.bat"
        alt = r".venv\Scripts\python start.py"
    elif sys.platform == "darwin":
        run = "./run.command   (or double-click it in Finder)"
        alt = ".venv/bin/python start.py"
    else:
        run = "./run.sh"
        alt = ".venv/bin/python start.py"
    ui.box(
        f"{ui.sym['ok']} IMURUN is ready  ({time.monotonic() - started:.0f}s)",
        [
            "Start it:",
            f"    {run}",
            f"    {alt}",
            "Options:  --browser   open in the browser",
            "          --no-open   server only",
            "          --port N    use another port",
        ],
        colour="green",
    )
    ui.line()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        ui.fail("Cancelled.")
        raise SystemExit(130)
