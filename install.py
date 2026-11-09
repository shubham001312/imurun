#!/usr/bin/env python3
"""IMURUN one-command installer (Windows / macOS / Linux). Idempotent.

    python install.py              # create/repair .venv, then install everything below into it
    python install.py --here       # install into the interpreter running this script
    python install.py --force      # redo every step (same as `imurun install --force`)
    python install.py --no-window  # skip the optional native-window package (pywebview)
    python install.py --no-voice   # skip the voice packages (~250 MB)
    python install.py --tuning     # also set up the fine-tuning venv (torch, 2-5 GB; opt-in)
    python install.py --no-build   # never compile the engine from source
    python install.py --build-tools  # Windows: winget-install MinGW-w64 + CMake + Ninja first
    python install.py --dev        # also install requirements-dev.txt
    python install.py --check      # only report what is missing, change nothing
    python install.py --doctor     # the `imurun doctor` report
    python install.py --verbose    # stream raw pip / build output instead of the spinner

Steps (only step 1 can fail the install; the rest are best-effort and reported):
  1. core packages (requirements.txt)          4. FISH engine: found / downloaded from the
  2. pywebview (Windows/macOS by default)         GitHub release / built from model/fish
  3. voice (requirements-voice.txt)            5. GPU runtime check (Vulkan loader + driver)
  (6. tuning venv, only with --tuning)
What was done is recorded per interpreter in ~/.imurun/install-state.json;
``start.py`` / ``imurun`` call ``quick_check`` on every start (a few ms, no
engine load) and run ``install.py --here --auto`` when something is missing.

Standard library only: this file must run before any dependency is installed.
``start.py`` imports the helpers below, so keep them free of side effects.
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
            log_dir = data_root() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            _LOG_PATH = log_dir / "install.log"
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


def use_system_certificates() -> str:
    """Verify HTTPS against the OS trust store (Windows / macOS Keychain / Linux CA bundle).

    Python's own bundle (certifi) does not know a corporate proxy's CA, and the
    python.org macOS build ships with no certificates at all until the user runs
    "Install Certificates.command": both fail with "unable to get local issuer
    certificate" (an unknown CA). truststore, the same library pip uses for this,
    makes every ssl context in the process (urllib, httpx, aiohttp) use the OS
    store. Verification stays on; only the source of trusted CAs changes.
    IMURUN_NO_TRUSTSTORE=1 keeps Python's bundle. Never fatal.
    """
    if os.environ.get("IMURUN_NO_TRUSTSTORE") == "1":
        return "python bundle (IMURUN_NO_TRUSTSTORE=1)"
    try:
        import truststore  # noqa: PLC0415

        truststore.inject_into_ssl()
    except Exception:  # noqa: BLE001 - not installed yet (first run) or unsupported platform
        return "python bundle"
    return "system trust store"


def data_root() -> Path:
    """The user data root (``~/.imurun`` unless IMURUN_DATA_ROOT says otherwise)."""
    env = os.environ.get("IMURUN_DATA_ROOT")
    return Path(env).expanduser() if env else Path.home() / ".imurun"


# The user data folder. Every subfolder the app writes into is created up front
# (idempotent: existing folders are left alone) so a fresh machine never fails
# on a missing directory and a user can see the layout before any data lands.
# Private folders (secrets, runtime, sessions) get mode 0700 on POSIX; on
# Windows the profile folder is already private to the user.
HOME_DIRS: tuple[tuple[str, bool], ...] = (
    ("models", False),        # downloaded GGUF files (+ models/.cache)
    ("models/.cache", False),
    ("bin", False),           # downloaded engine library (--get-engine)
    ("sessions", True),       # chats; each session's memory lives in its JSON
    ("settings", False),      # settings sections (JSON per section)
    ("secrets", True),        # connector tokens, OAuth client secrets
    ("runtime", True),        # api.json: port + token of the running server
    ("cache", False),
    ("cache/fish", False),    # engine importance + re-quantised bytes cache
    ("logs", False),
    ("skills", False),
    ("tools", False),
    ("workspace", False),     # files the agent may touch
    ("feedback", False),      # thumbs/teach signals for tuning
    ("voice", False),         # speech models and voices
    ("tuning", False),
    ("tuning/base", False),
    ("tuning/built", False),
    ("tuning/datasets", False),
    ("tuning/runs", False),
    ("tmp", False),
)


def ensure_home_layout(root: Path | None = None) -> dict:
    """Create the data folder tree under ``root`` (default ``data_root()``).

    Returns ``{"root", "created": [...], "present": [...], "failed": {name: error}}``.
    Cheap enough for every start (a stat per folder when everything exists).
    """
    base = Path(root) if root is not None else data_root()
    out: dict = {"root": str(base), "created": [], "present": [], "failed": {}}
    for name, private in HOME_DIRS:
        path = base / name
        try:
            if path.is_dir():
                out["present"].append(name)
            else:
                path.mkdir(parents=True, exist_ok=True)
                out["created"].append(name)
            if private and os.name != "nt":
                try:
                    os.chmod(path, 0o700)
                except OSError as exc:
                    out["failed"][name] = f"chmod: {exc}"
        except OSError as exc:
            out["failed"][name] = str(exc)
    return out


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
# Engine: load check, prebuilt download, source build
# --------------------------------------------------------------------------- #
# Oldest ABI the bindings accept, and the one they were written against
# (model/fish_native.py EXPECTED_ABI). Older libraries load with fewer features.
ENGINE_ABI_MIN = 1
ENGINE_ABI_EXPECTED = 3


def _file_sig(path: Path) -> dict:
    try:
        st = path.stat()
        return {"size": st.st_size, "mtime": int(st.st_mtime)}
    except OSError:
        return {}


def probe_engine(path: Path | None) -> dict:
    """Load the library with ctypes and read its ABI (no model is loaded).

    Costs a few milliseconds; the result is cached in the state file so
    ``quick_check`` never has to load the library on a normal start.
    """
    if path is None:
        return {"ok": False, "path": None, "error": "not found"}
    info: dict = {"ok": False, "path": str(path), **_file_sig(path)}
    try:
        import ctypes  # noqa: PLC0415

        lib = ctypes.CDLL(str(path))
        abi = 1
        if hasattr(lib, "fish_abi_version"):
            lib.fish_abi_version.restype = ctypes.c_int
            abi = int(lib.fish_abi_version())
        info["abi"] = abi
        if hasattr(lib, "fish_version"):
            lib.fish_version.restype = ctypes.c_char_p
            info["fish_version"] = (lib.fish_version() or b"").decode("utf-8", "replace")
        if abi < ENGINE_ABI_MIN:
            info["error"] = f"ABI {abi} is older than {ENGINE_ABI_MIN}; rebuild the engine"
        else:
            info["ok"] = True
            if abi < ENGINE_ABI_EXPECTED:
                info["warning"] = (f"ABI {abi} < {ENGINE_ABI_EXPECTED}: chat works, LoRA adapters "
                                   "need a newer engine build")
    except OSError as exc:
        info["error"] = f"cannot load: {exc}"
        if sys.platform == "darwin" and _quarantined(path):
            info["error"] += (f" - macOS quarantined this downloaded file. Use `imurun --get-engine` "
                              f"(checksum-verified, not quarantined) or: xattr -d com.apple.quarantine \"{path}\"")
    return info


def _quarantined(path: Path) -> bool:
    try:
        return "com.apple.quarantine" in os.listxattr(path)
    except (OSError, AttributeError):
        return False


def engine_source_dir() -> Path | None:
    """model/fish when the C++ sources are present (a checkout or unpacked sdist)."""
    src = ROOT / "model" / "fish"
    return src if (src / "CMakeLists.txt").is_file() else None


def _toolchain_env() -> dict[str, str]:
    """PATH plus the places winget puts WinLibs/CMake/Ninja (no new terminal needed)."""
    env = dict(os.environ)
    if sys.platform != "win32":
        return env
    extra: list[str] = []
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    links = local / "Microsoft" / "WinGet" / "Links"
    if links.is_dir():
        extra.append(str(links))
    pkgs = local / "Microsoft" / "WinGet" / "Packages"
    if pkgs.is_dir():
        for d in sorted(pkgs.glob("BrechtSanders.WinLibs*")):
            if (d / "mingw64" / "bin").is_dir():
                extra.append(str(d / "mingw64" / "bin"))
        extra.extend(str(d) for d in sorted(pkgs.glob("Ninja-build.Ninja*")))
    cmake_dir = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "CMake" / "bin"
    if cmake_dir.is_dir():
        extra.append(str(cmake_dir))
    if extra:
        env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    return env


def _which(name: str, env: dict[str, str] | None = None) -> str | None:
    return shutil.which(name, path=(env or _toolchain_env()).get("PATH"))


WINDOWS_TOOLCHAIN_IDS = ["BrechtSanders.WinLibs.POSIX.UCRT", "Kitware.CMake", "Ninja-build.Ninja"]


def install_build_tools_windows() -> bool:
    """winget install of the MinGW-w64 + CMake + Ninja toolchain (explicit opt-in)."""
    if sys.platform != "win32" or not shutil.which("winget"):
        ui.fail("winget is not available; install the build tools by hand (hints above).")
        return False
    ok = True
    for pkg in WINDOWS_TOOLCHAIN_IDS:
        ui.info(f"winget install -e --id {pkg}")
        proc = subprocess.run(["winget", "install", "-e", "--id", pkg, "--accept-package-agreements",
                               "--accept-source-agreements"])
        # 0x8A15002B: already installed.
        ok = ok and proc.returncode in (0, -1978335189, 2316632107)
    return ok


def build_engine(verbose: bool = False) -> Path | None:
    """Configure + build model/fish with CMake/Ninja and put the library in place.

    Only possible where the C++ sources exist (a checkout): the result goes to
    <repo>/bin, where the bindings look first. Returns the library path, or
    None after printing why.
    """
    src = engine_source_dir()
    if src is None:
        return None
    env = _toolchain_env()
    cxx = "clang++" if sys.platform == "darwin" else "g++"
    missing = [n for n in ("cmake", "ninja") if not _which(n, env)]
    if not (_which(cxx, env) or (sys.platform.startswith("linux") and _which("clang++", env))):
        missing.append(cxx)
    if missing:
        ui.warn("Cannot build the FISH engine here: missing " + ", ".join(missing))
        return None
    build = src / "build"
    dest_dir = user_engine_dir() if INSTALLED else ROOT / "bin"
    name = engine_library_names()[0]
    jobs = str(max(1, (os.cpu_count() or 2) - 1))
    configure = ["cmake", "-G", "Ninja", "-S", str(src), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release"]
    if sys.platform == "win32":
        configure += ["-DCMAKE_C_COMPILER=gcc", "-DCMAKE_CXX_COMPILER=g++"]
    steps = [
        ("Configuring the FISH engine (CMake)", configure),
        ("Building the FISH engine (Ninja, a few minutes)", ["ninja", "-C", str(build), "-j", jobs, "fish"]),
    ]
    log_path = _log_path()
    for label, cmd in steps:
        with open(log_path, "a", encoding="utf-8", errors="replace") as log:
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
            log.flush()
            with Spinner(label) as sp:
                try:
                    proc = subprocess.run(cmd, env=env, stdout=None if verbose else log,
                                          stderr=subprocess.STDOUT, timeout=3600)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    sp.done(f"{label}: {exc}", ok=False)
                    return None
                if proc.returncode != 0:
                    sp.done(f"{label} failed (exit {proc.returncode}); log: {log_path}", ok=False)
                    return None
                sp.done(label)
    built = build / name
    if not built.is_file():
        ui.fail(f"The build finished but {built} is missing.")
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    try:
        shutil.copy2(built, target)
    except OSError as exc:
        ui.fail(f"Could not copy the engine to {target}: {exc}")
        ui.hint("On Windows a running IMURUN locks bin/fish.dll: stop it and retry.")
        return None
    return target


def ensure_engine(*, auto: bool, allow_build: bool = True, verbose: bool = False) -> dict:
    """Find the engine; else download the prebuilt one; else build it; else explain.

    Never raises: a missing engine is not fatal (the UI starts in its
    'provisioning' state and says what to do).
    """
    lib = find_engine_library()
    source = "found"
    if lib is None and engine_asset_name() is not None:
        with Spinner("Downloading the prebuilt FISH engine") as sp:
            try:
                lib = download_engine(timeout=20)
                source = "downloaded"
                sp.done(f"Downloaded the FISH engine {ui.c(str(lib), 'grey')}")
            except RuntimeError as exc:
                sp.done(f"No prebuilt engine downloaded ({str(exc)[:160]})", ok=False)
    if lib is None and allow_build and engine_source_dir() is not None:
        asked_no = (auto and os.environ.get("IMURUN_AUTO_BUILD") != "1"
                    and not ui.ask("Build the FISH engine from source now (a few minutes)?"))
        lib = None if asked_no else build_engine(verbose=verbose)
        source = "built" if lib else source
    info = probe_engine(lib)
    info["source"] = source if lib else "missing"
    info["platform"] = f"{sys.platform}-{platform.machine().lower()}"
    if info.get("ok"):
        extra = f" ABI {info.get('abi')}" + (f", {info['fish_version']}" if info.get("fish_version") else "")
        ui.ok(f"FISH engine {ui.c(str(lib), 'grey')}{ui.c(extra, 'grey')}")
        if info.get("warning"):
            ui.warn(info["warning"])
    elif lib is not None:
        ui.fail(f"FISH engine at {lib} does not work: {info.get('error')}")
    else:
        report_engine()
        if engine_source_dir() is not None:
            if all(present for _, present, _ in build_tools()):
                ui.hint("The build tools are present: `python install.py` (without --no-build) builds it.")
            elif sys.platform == "win32":
                ui.hint("Install the toolchain with winget:  python install.py --build-tools")
    return info


# --------------------------------------------------------------------------- #
# GPU runtime check (Vulkan loader + vendor driver): file lookups only, no
# driver is loaded, so it is cheap and cannot hang.
# --------------------------------------------------------------------------- #
def _vendor_from_name(name: str) -> str | None:
    low = name.lower()
    if low.startswith("nv") or "nvidia" in low:
        return "NVIDIA"
    if "amd" in low or "radeon" in low:
        return "AMD"
    if "intel" in low or low.startswith("igvk") or "iris" in low:
        return "Intel"
    if "moltenvk" in low:
        return "Apple (MoltenVK)"
    if "lvp" in low or "lavapipe" in low:
        return "lavapipe (software)"
    return None


def gpu_report() -> dict:
    """What the engine's Vulkan backend needs (loader + a GPU driver), found or not."""
    found: dict = {"loader": None, "drivers": [], "vendors": []}
    if sys.platform == "win32":
        sysdir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        if (sysdir / "vulkan-1.dll").is_file():
            found["loader"] = str(sysdir / "vulkan-1.dll")
        try:
            import winreg  # noqa: PLC0415

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Khronos\Vulkan\Drivers") as key:
                i = 0
                while True:
                    try:
                        found["drivers"].append(winreg.EnumValue(key, i)[0])
                    except OSError:
                        break
                    i += 1
        except (ImportError, OSError):
            pass
        # Modern drivers register their ICDs under the display adapter key
        # instead; the vendor DLLs in System32 are a cheap second signal.
        for dll, vendor in (("nvapi64.dll", "NVIDIA"), ("nvoglv64.dll", "NVIDIA"),
                            ("amdvlk64.dll", "AMD"), ("atiadlxx.dll", "AMD"),
                            ("igvk64.dll", "Intel"), ("igdumdim64.dll", "Intel")):
            if (sysdir / dll).is_file() and vendor not in found["vendors"]:
                found["vendors"].append(vendor)
        hint = ("install or update the GPU driver from NVIDIA / AMD / Intel; it ships the "
                "Vulkan runtime (vulkan-1.dll)")
    elif sys.platform == "darwin":
        dirs = [Path("/usr/local/lib"), Path("/opt/homebrew/lib")]
        if os.environ.get("VULKAN_SDK"):
            dirs.insert(0, Path(os.environ["VULKAN_SDK"]) / "lib")
        for d in dirs:
            for n in ("libvulkan.1.dylib", "libvulkan.dylib", "libMoltenVK.dylib"):
                if (d / n).exists():
                    found["loader"] = found["loader"] or str(d / n)
                    if "MoltenVK" in n:
                        found["drivers"].append(str(d / n))
        hint = "brew install molten-vk vulkan-loader   (or the LunarG Vulkan SDK)"
    else:
        for d in ("/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu", "/usr/lib64",
                  "/usr/lib", "/usr/local/lib"):
            if (Path(d) / "libvulkan.so.1").exists():
                found["loader"] = str(Path(d) / "libvulkan.so.1")
                break
        for d in ("/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d", "/usr/local/share/vulkan/icd.d"):
            if Path(d).is_dir():
                found["drivers"].extend(str(f) for f in sorted(Path(d).glob("*.json")))
        if Path("/proc/driver/nvidia/version").exists():
            found["vendors"].append("NVIDIA")
        hint = ("sudo apt install libvulkan1 mesa-vulkan-drivers   (NVIDIA: its proprietary "
                "driver; Fedora: sudo dnf install vulkan-loader mesa-vulkan-drivers)")
    for drv in found["drivers"]:
        vendor = _vendor_from_name(Path(drv).name)
        if vendor and vendor not in found["vendors"]:
            found["vendors"].append(vendor)
    real = [v for v in found["vendors"] if not v.startswith("lavapipe")]
    found["usable"] = bool(found["loader"] and real)
    found["hint"] = None if found["usable"] else hint
    return found


def report_gpu(info: dict) -> None:
    if info.get("loader"):
        ui.ok(f"Vulkan loader {ui.c(str(info['loader']), 'grey')}")
    else:
        ui.warn("Vulkan loader not found - the engine will run on the CPU only")
    if info.get("vendors"):
        ui.ok("GPU driver: " + ", ".join(info["vendors"]))
    else:
        ui.warn("No GPU driver with Vulkan support found")
    if info.get("hint"):
        ui.hint("For GPU offload: " + info["hint"])
    ui.hint("GPU offload is tested on NVIDIA only (docs/engine/GPU.md); the CPU path always works.")


# --------------------------------------------------------------------------- #
# Install state (~/.imurun/install-state.json) and the fast start-up check
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# The `imurun` command: reachable from any new terminal, however it was installed
# --------------------------------------------------------------------------- #
# pip into a venv, `pip install --user` on Windows, `npx imurun` and a source
# checkout all leave a working app whose `imurun` command is not on PATH in a
# new terminal. Setup closes that gap: if `imurun` does not already run this
# interpreter, it writes a small launcher (marked with SHIM_MARK) into a user
# bin folder and puts that folder on the user's PATH. It never overwrites or
# deletes a file it did not write. IMURUN_NO_PATH=1 turns the PATH change off,
# IMURUN_BIN_DIR=<dir> moves the launcher (tests and the CI smoke install).
SHIM_MARK = "imurun-shim"
RC_BEGIN = "# >>> imurun >>>"
RC_END = "# <<< imurun <<<"


def command_dir() -> Path:
    override = os.environ.get("IMURUN_BIN_DIR")
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        # Always under the user profile, never next to a relocated data root:
        # IMURUN_DATA_ROOT=D:\\... would put a folder every local user may write
        # to (non-system drive roots grant Authenticated Users Modify) on PATH.
        return Path.home() / ".imurun" / "cmd"
    return Path.home() / ".local" / "bin"


def _safe_dir(folder: Path) -> str | None:
    """Why ``folder`` must not hold the launcher / go on PATH, or None when it is fine.

    A folder on PATH that another user can write to lets them shadow any
    command (and, on Windows, plant DLLs). POSIX: it must be owned by this
    user and not group/world-writable; anything else is refused.
    """
    if IS_WINDOWS:
        home = _norm(Path.home())
        return None if _norm(folder).startswith(home + os.sep) else f"{folder} is outside the user profile"
    probe = folder
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        st = probe.stat()
    except OSError as exc:
        return f"cannot inspect {probe}: {exc}"
    if st.st_uid != os.getuid():
        return f"{probe} is owned by another user"
    if st.st_mode & 0o002:
        return f"{probe} is writable by every user"
    if st.st_mode & 0o020 and st.st_gid != os.getgid():
        # Group-writable is fine for the user's own private group (most distros).
        return f"{probe} is writable by a shared group"
    return None


def command_path() -> Path:
    return command_dir() / ("imurun.cmd" if IS_WINDOWS else "imurun")


def _norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _on_path(folder: Path, path_value: str | None = None) -> bool:
    parts = (os.environ.get("PATH", "") if path_value is None else path_value).split(os.pathsep)
    return _norm(folder) in {_norm(os.path.expandvars(p)) for p in parts if p}


def _script_dirs() -> set[str]:
    """Folders where pip puts this interpreter's console scripts (env + user scheme)."""
    import sysconfig  # noqa: PLC0415

    dirs = {_norm(sysconfig.get_path("scripts"))}
    try:
        dirs.add(_norm(sysconfig.get_path("scripts", f"{os.name}_user")))
    except KeyError:  # pragma: no cover - interpreter without a user scheme
        pass
    if sys.platform == "darwin":
        try:
            dirs.add(_norm(sysconfig.get_path("scripts", "osx_framework_user")))
        except KeyError:
            pass
    return dirs


def _command_runs_this(path: Path) -> bool:
    """True when ``path`` (an `imurun` found on PATH) starts this interpreter's IMURUN."""
    try:
        head = path.read_bytes()[:4096].decode("utf-8", "replace")
    except OSError:
        return False
    if SHIM_MARK in head:
        return _py_token() in head
    if "IMURUN npm launcher" in head or "node_modules" in head:
        # The npm launcher runs ~/.imurun/venv (or IMURUN_VENV).
        venv = os.environ.get("IMURUN_VENV") or str(data_root() / "venv")
        return _norm(sys.prefix) == _norm(venv)
    if head.startswith("#!"):
        shebang = head[2:].splitlines()[0].strip().strip('"')
        return _norm(shebang) == _norm(sys.executable)
    # A Windows console-script .exe: pip writes it next to (or for) this interpreter.
    return _norm(path.parent) in _script_dirs()


def _only_while_activated(path: Path) -> bool:
    """A venv's own bin/Scripts is on PATH only while that venv is activated."""
    if sys.prefix == sys.base_prefix:
        return False
    return _norm(path).startswith(_norm(sys.prefix) + os.sep)


def _cmd_quote(value: str) -> str:
    """A path for a .cmd file: double-quoted, % doubled (cmd expands it even in quotes)."""
    return '"' + value.replace("%", "%%") + '"'


def _py_token() -> str:
    """How the launcher spells this interpreter (also how a launcher is recognised)."""
    import shlex  # noqa: PLC0415

    return _cmd_quote(sys.executable) if IS_WINDOWS else shlex.quote(sys.executable)


def _shim_text() -> str:
    # -P: do not put the current directory on sys.path. `python -m` normally
    # does, so typing `imurun` inside an untrusted folder would import that
    # folder's imurun/ or json.py (pip's own console scripts never do that).
    import shlex  # noqa: PLC0415

    py = _py_token()
    root = None if INSTALLED else str(ROOT)
    if IS_WINDOWS:
        lines = ["@echo off", f"rem {SHIM_MARK}: written by IMURUN setup; runs IMURUN with the Python below.",
                 "rem Safe to delete; `imurun install` writes it again.", "setlocal"]
        if root:
            # Never leave an empty PYTHONPATH entry: "" on sys.path is the cwd.
            r = root.replace("%", "%%")
            lines.append(f'if defined PYTHONPATH (set "PYTHONPATH={r};%PYTHONPATH%") '
                         f'else (set "PYTHONPATH={r}")')
        lines += [f"{py} -P -m imurun %*", "exit /b %errorlevel%", ""]
        return "\r\n".join(lines)
    lines = ["#!/bin/sh", f"# {SHIM_MARK}: written by IMURUN setup; runs IMURUN with the Python below.",
             "# Safe to delete; `imurun install` writes it again."]
    if root:
        lines += [f'PYTHONPATH={shlex.quote(root)}"${{PYTHONPATH:+:$PYTHONPATH}}"', "export PYTHONPATH"]
    lines += [f'exec {py} -P -m imurun "$@"', ""]
    return "\n".join(lines)


def _write_launcher(target: Path, text: str) -> None:
    """Atomic write that never follows a symlink planted at ``target``."""
    import tempfile  # noqa: PLC0415

    target.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        # cmd.exe reads batch files in the console (OEM) code page, not UTF-8.
        try:
            data = text.encode("oem")
        except (UnicodeEncodeError, LookupError):
            data = ("@chcp 65001 >nul\r\n" + text).encode("utf-8")
    else:
        data = text.encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".imurun-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if not IS_WINDOWS:
            os.chmod(tmp, 0o755)
        os.replace(tmp, target)  # replaces a symlink itself, never its target
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _rc_files() -> list[Path]:
    """Shell start-up files that get the PATH line (existing ones, plus the login shell's)."""
    home = Path.home()
    shell = os.path.basename(os.environ.get("SHELL", ""))
    files = [home / ".profile"]
    # A login bash (macOS Terminal, Fedora) reads ~/.bash_profile and then skips
    # ~/.profile, so an existing ~/.bash_profile needs the line too.
    if (home / ".bash_profile").exists():
        files.append(home / ".bash_profile")
    for name, sh in ((".bashrc", "bash"), (".zshrc", "zsh")):
        if (home / name).exists() or shell == sh:
            files.append(home / name)
    return files


def _add_to_user_path(folder: Path) -> list[str]:
    """Persist ``folder`` on the user's PATH for new terminals; returns what changed."""
    changed: list[str] = []
    if IS_WINDOWS:
        import winreg  # noqa: PLC0415

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                value, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                value, kind = "", winreg.REG_EXPAND_SZ
            if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
                raise OSError("the user Path in the registry is not a string value; left unchanged")
            if not _on_path(folder, value):
                new = f"{value.rstrip(';')};{folder}" if value else str(folder)
                winreg.SetValueEx(key, "Path", 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
                                  else winreg.REG_EXPAND_SZ, new)
                changed.append(r"HKCU\Environment\Path")
        if changed:
            try:  # tell Explorer (and so new terminals) that the environment changed
                import ctypes  # noqa: PLC0415

                ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, None)
            except (OSError, AttributeError):
                pass
        return changed
    import shlex  # noqa: PLC0415

    q = shlex.quote(str(folder))
    line = f'case ":$PATH:" in *:{q}:*) ;; *) export PATH={q}":$PATH" ;; esac'
    block = f"\n{RC_BEGIN}  (added by IMURUN setup: the `imurun` command)\n{line}\n{RC_END}\n"
    for rc in _rc_files():
        try:
            text = rc.read_text(encoding="utf-8") if rc.exists() else ""
        except OSError:
            continue
        if RC_BEGIN in text or str(folder) in text:
            continue
        try:
            with rc.open("a", encoding="utf-8") as fh:
                fh.write(block)
            changed.append(str(rc))
        except OSError:
            pass
    fish = Path.home() / ".config" / "fish"
    if fish.is_dir():
        conf = fish / "conf.d" / "imurun.fish"
        if not conf.exists():
            try:
                conf.parent.mkdir(parents=True, exist_ok=True)
                conf.write_text(f"# {SHIM_MARK}\nfish_add_path -g {shlex.quote(str(folder))}\n",
                                encoding="utf-8")
                changed.append(str(conf))
            except OSError:
                pass
    return changed


def path_changes_allowed() -> bool:
    return os.environ.get("IMURUN_NO_PATH") != "1" and not os.environ.get("CI")


def ensure_command() -> dict:
    """Make `imurun` start this IMURUN from a new terminal. Never fatal; returns the record."""
    target = command_path()
    found = shutil.which("imurun")
    if found and _command_runs_this(Path(found)) and not _only_while_activated(Path(found)):
        return {"ok": True, "path": found, "detail": "on PATH"}
    record: dict = {"ok": False, "path": str(target)}
    unsafe = _safe_dir(target.parent)
    if unsafe:
        record["detail"] = f"not writing a launcher: {unsafe}"
        record["hint"] = "run IMURUN with:  python -m imurun"
        return record
    if target.is_symlink() and not found:
        record["detail"] = f"{target} is a symlink to something else; left alone"
        record["hint"] = "run IMURUN with:  python -m imurun"
        return record
    try:
        existing = target.read_text(encoding="utf-8", errors="replace") if target.exists() else None
    except OSError:
        existing = None
    if existing is not None and SHIM_MARK not in existing and not _command_runs_this(target):
        record["detail"] = f"{target} belongs to another program; left alone"
        record["hint"] = "run IMURUN with:  python -m imurun"
        return record
    if existing is None or SHIM_MARK in existing:
        text = _shim_text()
        if existing != text:
            try:
                _write_launcher(target, text)
            except OSError as exc:
                record["detail"] = f"could not write {target}: {exc}"
                return record
    record["ok"] = True
    if _on_path(target.parent):
        record["detail"] = "launcher written" if not found else (
            f"launcher written; another `imurun` comes first on PATH: {found}")
        return record
    if not path_changes_allowed():
        record["detail"] = f"launcher written; {target.parent} is not on PATH (IMURUN_NO_PATH/CI)"
        return record
    try:
        changed = _add_to_user_path(target.parent)
    except OSError as exc:
        record["detail"] = f"launcher written; could not update PATH: {exc}"
        return record
    record["path_updated"] = changed
    record["detail"] = "launcher written, PATH updated: open a new terminal" if changed else (
        "launcher written; PATH already set for new terminals")
    return record


def report_command(info: dict) -> None:
    if not info.get("ok"):
        ui.warn(f"`imurun` command: {info.get('detail')}")
        if info.get("hint"):
            ui.hint(info["hint"])
        return
    ui.ok(f"`imurun` command: {info.get('detail')} {ui.c(str(info.get('path')), 'grey')}")


STATE_SCHEMA = 1


def state_path() -> Path:
    return data_root() / "install-state.json"


def _env_key() -> str:
    """One entry per interpreter: a checkout .venv and a pip install share ~/.imurun."""
    return os.path.normcase(os.path.abspath(sys.executable))


def read_state() -> dict:
    import json  # noqa: PLC0415

    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("schema") == STATE_SCHEMA else {}


def env_state() -> dict:
    return (read_state().get("envs") or {}).get(_env_key()) or {}


def write_env_state(entry: dict) -> Path | None:
    import json  # noqa: PLC0415

    data = read_state() or {}
    data["schema"] = STATE_SCHEMA
    data.setdefault("envs", {})[_env_key()] = entry
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        ui.warn(f"Could not write {path}: {exc}")
        return None
    return path


def frontend_index() -> Path:
    return ROOT / "frontend" / "design-lab" / "index.html"


def quick_check() -> list[str]:
    """Reasons the install step has to run; [] when everything is in place.

    Standard library only, no engine load, no heavy imports: one JSON read,
    four find_spec lookups and a few stat() calls (a few milliseconds).
    """
    reasons: list[str] = []
    miss = missing_modules()
    if miss:
        reasons.append("missing Python packages: " + ", ".join(miss))
    home = ensure_home_layout()
    if home["failed"]:
        reasons.append("data folder incomplete: " + ", ".join(f"{k} ({v})" for k, v in home["failed"].items()))
    if not frontend_index().is_file():
        reasons.append(f"frontend assets missing ({frontend_index()})")
    st = env_state()
    if not st:
        reasons.append("first run in this Python environment")
        return reasons
    if "command" not in st:
        reasons.append("the `imurun` command is not set up yet")
    version = app_version()
    if st.get("imurun") != version:
        reasons.append(f"IMURUN changed ({st.get('imurun')} -> {version})")
    eng = st.get("engine") or {}
    lib = find_engine_library()
    if lib is not None:
        if str(lib) != eng.get("path") or _file_sig(lib) != {k: eng.get(k) for k in ("size", "mtime")}:
            reasons.append("the engine library is new or changed (checking it)")
    elif eng.get("source") != "missing":
        reasons.append("the engine library disappeared")
    return reasons


def _dist_versions(dists: list[str]) -> dict[str, str]:
    from importlib import metadata  # noqa: PLC0415

    out = {}
    for d in dists:
        try:
            out[d] = metadata.version(d)
        except metadata.PackageNotFoundError:
            out[d] = ""
    return out


def install_voice(python: str, verbose: bool = False) -> dict:
    """Best-effort: server speech (requirements-voice.txt). Never fatal."""
    if not REQUIREMENTS_VOICE.is_file():
        return {"ok": False, "detail": "requirements-voice.txt not found"}
    if sys.version_info[:2] > MAX_TESTED_PY:
        return {"ok": False, "detail": f"skipped: no voice wheels for Python {sys.version.split()[0]}"}
    ok = run_pip(python, ["install", "-r", str(REQUIREMENTS_VOICE)],
                 "Installing voice (speech-to-text + text-to-speech, ~250 MB)", verbose=verbose)
    if not ok:
        ui.warn("Voice is optional: IMURUN works without it (/api/voice/* answers 503 with the reason).")
    return {"ok": ok, "detail": "installed" if ok else "pip failed (see install.log)"}


def install_tuning(python: str) -> dict:
    """Opt-in: the training stack goes into its own venv (python -m services.tuning.setup)."""
    ui.info("Setting up the tuning venv (torch + transformers, 2-5 GB) ...")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(ROOT), env.get("PYTHONPATH", "")) if p)
    try:
        data_root().mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([python, "-m", "services.tuning.setup"], cwd=str(data_root()), env=env)
    except OSError as exc:
        return {"ok": False, "detail": str(exc)}
    if proc.returncode != 0:
        ui.warn("Tuning setup failed; chat is unaffected. Retry with: imurun install --tuning")
    return {"ok": proc.returncode == 0, "detail": "installed" if proc.returncode == 0
            else f"setup failed (exit {proc.returncode})"}


def run_setup(*, python: str, window: bool, voice: bool, tuning: bool, force: bool,
              auto: bool, build: bool, verbose: bool) -> int:
    """Install / repair everything into ``python`` (the running interpreter) and record it.

    Only the core packages are fatal; window, voice, engine, GPU and tuning
    are reported and recorded but never stop the app from starting.
    """
    prev = {} if force else env_state()
    version_changed = prev.get("imurun") != app_version()
    total = 6 if tuning else 5
    home = ensure_home_layout()
    if home["failed"]:
        ui.fail("Data folder: could not create " + ", ".join(f"{k} ({v})" for k, v in home["failed"].items()))
        return 1
    ui.ok(f"Data folder {home['root']}: " + (f"created {len(home['created'])} folder(s)"
          if home["created"] else "complete") + f", {len(home['present']) + len(home['created'])} in place")
    command = ensure_command()
    report_command(command)
    ui.step(1, total, "Python packages")
    if force or missing_modules():
        try:
            pip_install(python, window=False, upgrade_pip=False, verbose=verbose)
        except SystemExit:
            ui.fail("Step 1 (Python packages) failed: the required packages could not be installed.")
            return 1
    still = missing_modules()
    if still:
        ui.fail(f"Step 1 (Python packages) failed: still missing {', '.join(still)}")
        return 1
    core = _dist_versions(list(REQUIRED_MODULES.values()))
    ui.ok("Core packages: " + ", ".join(f"{k} {v}" for k, v in core.items()))

    ui.step(2, total, "Native window (optional)")
    have_window = not missing_modules(None, {WINDOW_MODULE[0]: WINDOW_MODULE[1]})
    if window and not have_window and (force or version_changed or not prev.get("window_tried")):
        have_window = run_pip(python, ["install", WINDOW_REQUIREMENT], "Installing pywebview",
                              verbose=verbose)
        if not have_window:
            ui.warn("pywebview is optional - IMURUN opens in your browser instead.")
    ui.ok("pywebview installed (native window)" if have_window else "pywebview not installed (browser UI)")

    ui.step(3, total, "Voice (optional)")
    voice_state = prev.get("voice") or {}
    if not missing_modules(None, {"faster_whisper": "faster-whisper", "piper": "piper-tts", "av": "av"}):
        voice_state = {"ok": True, "detail": "installed"}
        ui.ok("Voice packages present")
    elif not voice:
        voice_state = {"ok": False, "detail": "skipped (--no-voice, IMURUN_NO_VOICE=1 or CI)"}
        ui.info("Voice skipped (--no-voice, IMURUN_NO_VOICE=1 or CI); add it later: imurun install --force")
    elif force or version_changed or not voice_state:
        voice_state = install_voice(python, verbose=verbose)
    else:
        ui.info(f"Voice not installed ({voice_state.get('detail')}); retry: imurun install --force")

    ui.step(4, total, "FISH engine")
    engine = ensure_engine(auto=auto, allow_build=build, verbose=verbose)

    ui.step(5, total, "GPU runtime")
    gpu = gpu_report()
    report_gpu(gpu)

    tuning_state = prev.get("tuning") or {"ok": False,
                                          "detail": "not installed (opt-in: imurun install --tuning)"}
    if tuning:
        ui.step(6, total, "Tuning (opt-in)")
        tuning_state = install_tuning(python)

    entry = {
        "imurun": app_version(),
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "app_root": str(ROOT),
        "layout": "installed" if INSTALLED else "checkout",
        "platform": os_label(),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "core": core,
        "window": have_window,
        "window_tried": bool(window or prev.get("window_tried")),
        "voice": voice_state,
        "tuning": tuning_state,
        "engine": engine,
        "gpu": gpu,
        "home": {"root": home["root"], "folders": len(home["present"]) + len(home["created"])},
        "command": command,
    }
    path = write_env_state(entry)
    if path:
        ui.hint(f"Recorded in {path}")
    return 0


def doctor(port: int = 8732) -> int:
    """`imurun doctor`: a short, read-only report of this machine and install."""
    ui.banner(f"doctor {ui.sym['dot']} {os_label()}")
    ui.ok(f"IMURUN {app_version()} ({'installed package' if INSTALLED else 'source checkout'})")
    ui.hint(f"app files: {ROOT}")
    py_ok = MIN_PY <= sys.version_info[:2] <= MAX_TESTED_PY
    (ui.ok if py_ok else ui.warn)(f"Python {sys.version.split()[0]} {ui.c(sys.executable, 'grey')}")
    miss = missing_modules()
    (ui.ok if not miss else ui.fail)("core packages " + ("ok" if not miss else "missing: " + ", ".join(miss)))
    have_ui = frontend_index().is_file()
    (ui.ok if have_ui else ui.fail)(f"frontend {ui.c(str(frontend_index().parent), 'grey')}")
    lib = find_engine_library()
    info = probe_engine(lib)
    if info.get("ok"):
        ui.ok(f"engine {ui.c(str(lib), 'grey')} ABI {info.get('abi')} {info.get('fish_version', '')}")
        if info.get("warning"):
            ui.warn(info["warning"])
    elif lib:
        ui.fail(f"engine {lib}: {info.get('error')}")
    else:
        ui.fail("engine not found (" + " / ".join(engine_library_names()) + ")")
        for line in engine_build_instructions().splitlines():
            ui.hint(line)
    report_gpu(gpu_report())
    ui.ok(f"HTTPS certificates: {use_system_certificates()}")
    found = shutil.which("imurun")
    if found and _command_runs_this(Path(found)):
        ui.ok(f"`imurun` command {ui.c(found, 'grey')}")
    elif found:
        ui.warn(f"`imurun` on PATH is another install: {found}")
        ui.hint("this one: python -m imurun  (or `imurun install --force` to point the launcher here)")
    else:
        shim = command_path()
        ui.warn("`imurun` is not on PATH in this terminal"
                + (f" (launcher: {shim}; open a new terminal)" if shim.exists() else ""))
    root = data_root()
    ui.ok(f"home dir {ui.c(str(root), 'grey')}")
    home = ensure_home_layout(root)
    (ui.ok if not home["failed"] else ui.fail)(
        f"data folder {ui.c(str(root), 'grey')}: {len(home['present']) + len(home['created'])} folders"
        + (f", created {len(home['created'])} now" if home["created"] else "")
        + (", FAILED: " + ", ".join(home["failed"]) if home["failed"] else ""))
    models = root / "models" if INSTALLED else ROOT / "models"
    count = len(list(models.glob("*.gguf"))) if models.is_dir() else 0
    ui.ok(f"models dir {ui.c(str(models), 'grey')} ({count} .gguf)")
    st = env_state()
    if st:
        ui.ok(f"install state {ui.c(str(state_path()), 'grey')} ({st.get('imurun')}, {st.get('installed_at')})")
        ui.hint(f"voice: {(st.get('voice') or {}).get('detail', '?')}; "
                f"tuning: {(st.get('tuning') or {}).get('detail', '?')}")
    else:
        ui.warn("no install state yet (the first `imurun` start creates it)")
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
        ui.ok(f"port {port} is free")
    except OSError:
        ui.warn(f"port {port} is in use (IMURUN already running? use --port N)")
    reasons = quick_check()
    if reasons:
        ui.info("the next start runs the install step: " + "; ".join(reasons))
    ui.line()
    return 0 if not miss and have_ui else 1


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


def _auto_policy() -> dict[str, bool]:
    """What an automatic (start-time) setup may do without asking.

    CI and IMURUN_SKIP_OPTIONAL=1 keep it to the core packages + the checks,
    so test runs never download 250 MB of voice wheels or compile the engine.
    """
    lean = bool(os.environ.get("CI")) or os.environ.get("IMURUN_SKIP_OPTIONAL") == "1"
    return {
        "voice": not lean and os.environ.get("IMURUN_NO_VOICE") != "1",
        "build": not lean and (os.environ.get("IMURUN_AUTO_BUILD") == "1"
                               or (sys.stdin is not None and sys.stdin.isatty())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install everything IMURUN needs on this machine (idempotent).")
    parser.add_argument("--here", action="store_true",
                        help="install into the current interpreter instead of .venv "
                             "(always the case for an installed copy)")
    parser.add_argument("--python", default=None, help="base interpreter used to create .venv")
    win = parser.add_mutually_exclusive_group()
    win.add_argument("--window", action="store_true", help="install pywebview (native window)")
    win.add_argument("--no-window", action="store_true", help="do not install pywebview")
    parser.add_argument("--dev", action="store_true", help="also install requirements-dev.txt")
    parser.add_argument("--voice", action="store_true", help=argparse.SUPPRESS)  # default now
    parser.add_argument("--no-voice", action="store_true", help="skip the voice packages (~250 MB)")
    parser.add_argument("--tuning", action="store_true",
                        help="also set up the fine-tuning venv (torch, 2-5 GB; opt-in)")
    parser.add_argument("--no-build", action="store_true",
                        help="never compile the FISH engine from source")
    parser.add_argument("--build-tools", action="store_true",
                        help="Windows: winget-install MinGW-w64 + CMake + Ninja, then build the engine")
    parser.add_argument("--force", action="store_true",
                        help="redo every step even if the state file says it is done")
    parser.add_argument("--auto", action="store_true",
                        help="non-interactive run started by `imurun`/start.py (no final box)")
    parser.add_argument("--check", action="store_true", help="only report status; install nothing")
    parser.add_argument("--doctor", action="store_true", help="print the `imurun doctor` report")
    parser.add_argument("--port", type=int, default=8732, help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help="show raw pip / build output")
    parser.add_argument("--quiet-if-ok", action="store_true",
                        help="exit silently with 0 when everything is already installed")
    args = parser.parse_args(argv)
    use_system_certificates()

    if sys.version_info < MIN_PY:
        ui.fail(f"IMURUN needs Python {MIN_PY[0]}.{MIN_PY[1]}+; this is {sys.version.split()[0]}.")
        return 1
    if args.doctor:
        return doctor(args.port)

    # An installed copy has no project .venv: it always installs into the
    # interpreter pip put it in (a venv, pipx, or the npm launcher's venv).
    here = args.here or INSTALLED or in_venv()
    target: Path | None = Path(sys.executable) if here else (venv_python() if venv_healthy() else None)

    if args.quiet_if_ok and here and not quick_check():
        return 0

    if not args.auto:
        ui.banner(f"setup {ui.sym['dot']} {os_label()} {ui.sym['dot']} Python {sys.version.split()[0]}")
    if args.check:
        return _check(args, target)

    if not here:
        # Source checkout started with a system Python: create/repair .venv,
        # then run this installer again from inside it, so every step (and
        # the state entry) belongs to the interpreter that will run IMURUN.
        if target is None:
            target = create_venv(find_base_python(args.python))
        else:
            ui.ok(f"Using {ui.c(str(target), 'grey')}")
        rest = [a for a in (argv if argv is not None else sys.argv[1:]) if a != "--here"]
        return subprocess.call([str(target), str(Path(__file__).resolve()), "--here", *rest])

    if args.build_tools:
        if sys.platform == "win32":
            install_build_tools_windows()
        else:
            for name, present, how in build_tools():
                ui.line(f"      {ui.c(ui.sym['ok'] if present else ui.sym['fail'], 'green' if present else 'red')}"
                        f" {name}  {'' if present else ui.c(how, 'grey')}")

    policy = _auto_policy() if args.auto else {"voice": True, "build": True}
    window = args.window or (not args.no_window and window_supported_by_default())
    started = time.monotonic()
    if args.dev and REQUIREMENTS_DEV.exists():
        run_pip(sys.executable, ["install", "-r", str(REQUIREMENTS_DEV)], "Installing dev tools",
                verbose=args.verbose)
    rc = run_setup(python=sys.executable, window=window,
                   voice=policy["voice"] and not args.no_voice, tuning=args.tuning,
                   force=args.force, auto=args.auto,
                   build=(policy["build"] or args.build_tools) and not args.no_build,
                   verbose=args.verbose)
    if rc != 0:
        ui.line()
        ui.fail("Setup did not finish. Fix the problem above and run it again "
                "(imurun install, or python install.py).")
        return rc
    if args.auto:
        ui.ok(f"Setup done ({time.monotonic() - started:.0f}s)")
        ui.line()
        return 0

    ui.line()
    if INSTALLED or not in_venv():
        run, alt = "imurun", "python -m imurun"
    elif sys.platform == "win32":
        run, alt = "run.bat", r".venv\Scripts\python start.py"
    elif sys.platform == "darwin":
        run, alt = "./run.command   (or double-click it in Finder)", ".venv/bin/python start.py"
    else:
        run, alt = "./run.sh", ".venv/bin/python start.py"
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
