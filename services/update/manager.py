"""Update check, "ask again" policy, and apply for IMURUN.

Standard library only: ``imurun --version`` and ``imurun update`` use this
before (or without) the server's dependencies.

* ``check()``    PyPI JSON + GitHub latest release, 3 s timeout each, cached
                 6 h in ``<data root>/update-state.json``.
* policy         ``prompt`` is true when a newer version exists and the user
                 has not dismissed it, or dismissed it 3+ launches ago, or a
                 version newer than the dismissed one appeared.
* ``apply()``    updates whatever this copy is (checkout: ``git pull --ff-only``
                 then ``install.py --auto``; pip: ``pip install -U imurun``;
                 pipx: ``pipx upgrade imurun``; uv: ``uv tool upgrade imurun``;
                 npm launcher venv: pip; pip pins ``imurun==<latest>``), then
                 asks the launcher to restart (``EXIT_RESTART`` = 75, see
                 ``request_restart`` and start.py).

Nothing is ever applied without an explicit user action.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

PYPI_URL = "https://pypi.org/pypi/imurun/json"
GITHUB_URL = "https://api.github.com/repos/shubham001312/imurun/releases/latest"
RELEASES_URL = "https://github.com/shubham001312/imurun/releases"
TIMEOUT = 3.0
CACHE_SECONDS = 6 * 3600
ASK_AGAIN_AFTER = 3          # launches after "Not now" before the prompt returns
RESTART_DELAY = 2.0          # seconds between restart_pending and the restart
EXIT_RESTART = 75            # server exit code that means "start me again"
STATE_FILE = "update-state.json"
OFF_VALUES = ("0", "false", "no", "off")
STATES = ("idle", "checking", "updating", "restart_pending", "error")

# Set when an applied update wants the process restarted; start.py polls it.
RESTART = threading.Event()


def enabled() -> bool:
    """The automatic per-launch check (IMURUN_UPDATE_CHECK=0 turns it off).

    Test runs (pytest) and CI never check automatically, so they never touch
    the network or the user's state file.
    """
    if os.environ.get("IMURUN_UPDATE_CHECK", "1").strip().lower() in OFF_VALUES:
        return False
    return not (os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("CI"))


def request_restart(delay: float = RESTART_DELAY) -> None:
    """Ask start.py to stop the server and start the new version.

    start.py's wait loop sees ``RESTART``, stops the server (which closes the
    model), then either exits with ``EXIT_RESTART`` when a launcher supervises
    it (``IMURUN_SUPERVISED=1``: the ``imurun`` shim, the npm launcher) or
    starts a fresh interpreter itself and passes its exit code on.
    """
    if delay <= 0:
        RESTART.set()
        return
    timer = threading.Timer(delay, RESTART.set)
    timer.daemon = True
    timer.start()


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #
_PRE = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "rc": 3, "c": 3}


def parse_version(text: str | None) -> tuple | None:
    """A sortable key for a PEP 440-ish version ('v0.4.1', '0.5.0rc1'); None if unparsable."""
    if not text:
        return None
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)(?:[.-]?(dev|alpha|beta|rc|a|b|c)\.?(\d*))?", str(text), re.I)
    if not m:
        return None
    release = [int(p) for p in m.group(1).split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    if m.group(2):
        pre = (_PRE[m.group(2).lower()], int(m.group(3) or 0))
    else:
        pre = (9, 0)  # a final release sorts after its pre-releases
    return (tuple(release), pre)


def is_newer(candidate: str | None, than: str | None) -> bool:
    a, b = parse_version(candidate), parse_version(than)
    if a is None:
        return False
    if b is None:
        return True
    return a > b


def clean_version(text: str | None) -> str | None:
    if not text:
        return None
    text = str(text).strip()
    return text[1:] if text[:1] in ("v", "V") else text


# --------------------------------------------------------------------------- #
# Layout: what "apply" updates
# --------------------------------------------------------------------------- #
def detect_source(*, installed: bool, prefix: str | None = None, env: dict | None = None,
                  data_root: Path | None = None) -> str:
    """checkout | pip | pipx | uv | npm."""
    if not installed:
        return "checkout"
    env = os.environ if env is None else env
    prefix_path = os.path.normcase(os.path.abspath(prefix or sys.prefix))
    if env.get("IMURUN_LAUNCHER") == "npm":
        return "npm"
    npm_venv = env.get("IMURUN_VENV") or (str(Path(data_root) / "venv") if data_root else None)
    if npm_venv and os.path.normcase(os.path.abspath(npm_venv)) == prefix_path:
        return "npm"
    parts = re.split(r"[\\/]+", prefix_path)
    if "pipx" in parts or any(p.endswith("pipx") for p in parts):
        return "pipx"
    # `uv tool install imurun`: ~/.local/share/uv/tools/imurun (Windows:
    # %APPDATA%\uv\data\tools\imurun). Those venvs have no pip of their own.
    if "uv" in parts and "tools" in parts:
        return "uv"
    return "pip"


def _git(root: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        return subprocess.run([git, "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def apply_plan(source: str, *, root: Path, python: str | None = None,
               version: str | None = None) -> list[list[str]]:
    """The commands ``apply`` runs for this layout, in order.

    pip / npm pin ``imurun==<version>`` when the checked version is known:
    a bare ``pip install -U imurun`` exits 0 without changing anything when
    GitHub already has the release but the PyPI upload has not finished (or
    when the latest is a pre-release), and the app would then "restart into"
    the same old version. With the pin pip fails loudly instead.
    """
    py = python or sys.executable
    if source == "checkout":
        return [["git", "-C", str(root), "pull", "--ff-only"],
                [py, str(root / "install.py"), "--auto"]]
    if source == "pipx":
        return [["pipx", "upgrade", "imurun"]]
    if source == "uv":
        return [["uv", "tool", "upgrade", "imurun"]]
    # pip and the npm launcher's venv (~/.imurun/venv): pip in the running interpreter.
    spec = f"imurun=={version}" if version and parse_version(version) else "imurun"
    return [[py, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", spec]]


def describe_plan(plan: list[list[str]]) -> str:
    out = []
    for cmd in plan:
        name = Path(cmd[0]).name.lower()
        if name.startswith("python"):
            cmd = ["python", *[Path(c).name if c.endswith(".py") else c for c in cmd[1:]]]
        elif name == "git":
            cmd = ["git", *cmd[3:]]  # drop "-C <root>"
        out.append(" ".join(cmd))
    return ", then ".join(out)


def apply_blocked(source: str, root: Path) -> str | None:
    """Why ``apply`` would refuse right now (None = it can run)."""
    if source == "checkout":
        if not (root / ".git").exists():
            return "this folder is not a git checkout; download the new release instead"
        res = _git(root, "status", "--porcelain", "--untracked-files=no")
        if res is None:
            return "git is not installed or not on PATH"
        if res.returncode != 0:
            return "git status failed: " + (res.stderr or "").strip()[:200]
        if res.stdout.strip():
            return "the checkout has local changes (git status); commit or stash them, then update"
        return None
    if source == "pipx" and not shutil.which("pipx"):
        return "pipx is not on PATH; run: pipx upgrade imurun"
    if source == "uv" and not shutil.which("uv"):
        return "uv is not on PATH; run: uv tool upgrade imurun"
    return None


def release_engine_lock(paths: list[Path]) -> list[str]:
    """Windows: let git/pip replace an engine library this process has loaded.

    A loaded DLL cannot be deleted or overwritten but it can be renamed, so the
    loaded file moves to ``<name>.old-<pid>`` and an unlocked copy takes its
    place (same bytes: git sees no change). start.py removes stale ``.old-*``
    copies on the next start. No-op elsewhere.
    """
    done: list[str] = []
    if os.name != "nt":
        return done
    for path in paths:
        try:
            if not path.is_file():
                continue
            old = path.with_name(f"{path.name}.old-{os.getpid()}")
            os.replace(path, old)
            shutil.copy2(old, path)
            done.append(str(path))
        except OSError:
            continue
    return done


def remove_stale_engine_copies(dirs: list[Path]) -> int:
    count = 0
    for d in dirs:
        try:
            for p in d.glob("*.old-*"):
                try:
                    p.unlink()
                    count += 1
                except OSError:
                    pass  # still loaded by a process that has not exited yet
        except OSError:
            pass
    return count


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #
def http_json(url: str, timeout: float = TIMEOUT) -> dict:
    import urllib.request  # noqa: PLC0415

    req = urllib.request.Request(url, headers={
        "User-Agent": "imurun-update-check",
        "Accept": "application/json" if "pypi" in url else "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URLs
        return json.loads(resp.read(2_000_000).decode("utf-8"))


def is_prerelease(text: str | None) -> bool:
    key = parse_version(text)
    return key is not None and key[1][0] != 9


def fetch_latest(fetch: Callable[[str, float], dict] = http_json, timeout: float = TIMEOUT,
                 *, allow_pre: bool = False) -> dict:
    """``{latest, releaseNotesUrl, errors}`` from PyPI and GitHub (the newer wins).

    Pre-releases (``0.4.0rc1``) are offered only to someone already running a
    pre-release (``allow_pre``): a final-release user is never pushed onto one.
    """
    found: list[tuple[str, str | None]] = []
    errors: list[str] = []
    try:
        data = fetch(PYPI_URL, timeout)
        v = clean_version((data.get("info") or {}).get("version"))
        if v and parse_version(v) and (allow_pre or not is_prerelease(v)):
            found.append((v, None))
    except Exception as exc:  # noqa: BLE001 - offline / proxy / bad JSON are all just "unknown"
        errors.append(f"PyPI: {type(exc).__name__}: {exc}"[:200])
    try:
        data = fetch(GITHUB_URL, timeout)
        if not data.get("draft") and not data.get("prerelease"):
            v = clean_version(data.get("tag_name"))
            if v and parse_version(v) and (allow_pre or not is_prerelease(v)):
                found.append((v, data.get("html_url") or f"{RELEASES_URL}/tag/v{v}"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"GitHub: {type(exc).__name__}: {exc}"[:200])
    if not found:
        return {"latest": None, "releaseNotesUrl": None, "errors": errors}
    best = max(found, key=lambda item: parse_version(item[0]))
    url = best[1] or next((u for v, u in found if u and v == best[0]), None) \
        or f"{RELEASES_URL}/tag/v{best[0]}"
    return {"latest": best[0], "releaseNotesUrl": url, "errors": errors}


# --------------------------------------------------------------------------- #
# Policy (pure)
# --------------------------------------------------------------------------- #
def should_prompt(*, current: str, latest: str | None, dismissed: str | None,
                  launches_since_dismiss: int, snoozed: bool = False) -> bool:
    if snoozed or not is_newer(latest, current):
        return False
    if not dismissed:
        return True
    if is_newer(latest, dismissed):
        return True
    return launches_since_dismiss >= ASK_AGAIN_AFTER


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #
def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass
class UpdateManager:
    data_root: Path
    current: str
    source: str
    root: Path
    publish: Callable[[str, dict], object] | None = None
    fetch: Callable[[str, float], dict] = http_json
    runner: Callable[..., subprocess.CompletedProcess] | None = None
    clock: Callable[[], float] = time.time
    python: str = field(default_factory=lambda: sys.executable)
    engine_paths: list[Path] = field(default_factory=list)
    restart: Callable[[], None] = request_restart

    def __post_init__(self) -> None:
        self.data_root = Path(self.data_root)
        self._lock = threading.RLock()
        self._state = "idle"
        self._error: str | None = None
        self._snoozed = False
        self._persist = self._load()

    # -------------------------------------------------------------- state file
    @property
    def path(self) -> Path:
        return self.data_root / STATE_FILE

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._persist, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass  # a read-only home only loses the cache and the dismiss memory

    def _emit(self) -> None:
        if self.publish is None:
            return
        try:
            self.publish("update", self.view())
        except Exception:  # noqa: BLE001 - a dead event broker must not break updates
            pass

    def _set_state(self, state: str, error: str | None = None) -> None:
        with self._lock:
            self._state, self._error = state, error
        self._emit()

    # ------------------------------------------------------------------ policy
    def record_launch(self) -> None:
        """Called once per server start. Counts launches since "Not now"."""
        with self._lock:
            p = self._persist
            dismissed = p.get("dismissed_version")
            if dismissed and not is_newer(dismissed, self.current):
                # The user is already on (or past) the dismissed version.
                p.pop("dismissed_version", None)
                p["launches_since_dismiss"] = 0
            elif dismissed:
                p["launches_since_dismiss"] = int(p.get("launches_since_dismiss") or 0) + 1
            p["launches"] = int(p.get("launches") or 0) + 1
            self._save()

    @property
    def latest(self) -> str | None:
        return self._persist.get("latest")

    @property
    def available(self) -> bool:
        return is_newer(self.latest, self.current)

    def prompt(self) -> bool:
        p = self._persist
        return enabled() and should_prompt(
            current=self.current, latest=self.latest, dismissed=p.get("dismissed_version"),
            launches_since_dismiss=int(p.get("launches_since_dismiss") or 0), snoozed=self._snoozed,
        ) and self._state in ("idle", "error")

    def dismiss(self) -> dict:
        with self._lock:
            self._persist["dismissed_version"] = self.latest or self.current
            self._persist["launches_since_dismiss"] = 0
            self._save()
        self._emit()
        return self.view()

    def later(self) -> dict:
        with self._lock:
            self._snoozed = True
        self._emit()
        return self.view()

    # ------------------------------------------------------------------- check
    def cache_fresh(self) -> bool:
        checked = float(self._persist.get("checked_at") or 0)
        return bool(checked) and 0 <= self.clock() - checked < CACHE_SECONDS

    def _begin(self, state: str, *, force: bool = True) -> bool:
        """Take the busy transition; False when busy or (check only) the cache is fresh."""
        with self._lock:
            if self._state in ("checking", "updating", "restart_pending"):
                return False
            if state == "checking" and not force and self.cache_fresh():
                return False
            self._state, self._error = state, None
        self._emit()
        return True

    def check(self, *, force: bool = False) -> dict:
        """Query PyPI + GitHub unless the 6 h cache is fresh. Never raises."""
        if self._begin("checking", force=force):
            self._do_check()
        return self.view()

    def check_async(self, *, force: bool = False) -> threading.Thread | None:
        if not self._begin("checking", force=force):
            return None
        thread = threading.Thread(target=self._do_check, name="imurun-update-check", daemon=True)
        thread.start()
        return thread

    def _do_check(self) -> None:
        try:
            result = fetch_latest(self.fetch, allow_pre=is_prerelease(self.current))
        except Exception as exc:  # noqa: BLE001 - never kill the thread with a traceback
            result = {"latest": None, "errors": [str(exc)]}
        with self._lock:
            if result["latest"]:
                self._persist["latest"] = result["latest"]
                self._persist["release_notes_url"] = result.get("releaseNotesUrl")
                self._persist["checked_at"] = self.clock()
                self._save()
                state, error = "idle", None
            else:
                state = "error"
                error = "could not reach PyPI or GitHub (" + "; ".join(result["errors"]) + ")"
        self._set_state(state, error)

    # ------------------------------------------------------------------- apply
    def plan(self) -> list[list[str]]:
        target = self.latest if self.available else None
        return apply_plan(self.source, root=self.root, python=self.python, version=target)

    def blocked(self) -> str | None:
        return apply_blocked(self.source, self.root)

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        if self.runner is not None:
            return self.runner(cmd)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        env.pop("IMURUN_SKIP_SETUP", None)
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=1800, **kw)

    def apply(self, *, restart: bool = True) -> dict:
        """Run the update for this layout (blocking). Returns the view; never raises."""
        if self._begin("updating"):
            self._do_apply(restart)
        return self.view()

    def apply_async(self, *, restart: bool = True) -> threading.Thread | None:
        if not self._begin("updating"):
            return None
        thread = threading.Thread(target=self._do_apply, args=(restart,), name="imurun-update-apply",
                                  daemon=True)
        thread.start()
        return thread

    def _do_apply(self, restart: bool) -> None:
        reason = self.blocked()
        if reason:
            self._set_state("error", reason)
            return
        release_engine_lock(self.engine_paths)
        for cmd in self.plan():
            try:
                res = self._run(cmd)
            except (OSError, subprocess.SubprocessError) as exc:
                self._set_state("error", f"{Path(cmd[0]).name} could not run: {exc}")
                return
            if res.returncode != 0:
                tail = ((res.stderr or "") + (res.stdout or "")).strip().splitlines()[-3:]
                self._set_state("error", f"`{describe_plan([cmd])}` failed (exit {res.returncode}): "
                                + " | ".join(tail)[:400])
                return
        with self._lock:
            self._persist.pop("dismissed_version", None)
            self._persist["launches_since_dismiss"] = 0
            self._persist["applied"] = {"from": self.current, "to": self.latest, "at": self.clock()}
            self._save()
        self._blocked_memo = None
        if restart:
            self._set_state("restart_pending")
            self.restart()
        else:
            self._set_state("idle")

    # -------------------------------------------------------------------- view
    def view(self) -> dict:
        with self._lock:
            p = self._persist
            blocked = None
            if self.available and self._state in ("idle", "error"):
                blocked = self._blocked_cached()
            return {
                "current": self.current,
                "latest": self.latest,
                "available": self.available,
                "source": self.source,
                "releaseNotesUrl": p.get("release_notes_url") or (
                    f"{RELEASES_URL}/tag/v{self.latest}" if self.latest else None),
                "prompt": self.prompt(),
                "state": self._state,
                "error": self._error,
                "checkedAt": _iso(p.get("checked_at")),
                "enabled": enabled(),
                "canApply": self.available and blocked is None,
                "applyBlocked": blocked,
                "command": describe_plan(self.plan()),
                "dismissedVersion": p.get("dismissed_version"),
                "launchesSinceDismiss": int(p.get("launches_since_dismiss") or 0),
            }

    def _blocked_cached(self) -> str | None:
        # git status costs ~20-50 ms; GET /api/update may be polled.
        now = time.monotonic()
        cached = getattr(self, "_blocked_memo", None)
        if cached and now - cached[0] < 10:
            return cached[1]
        value = self.blocked()
        self._blocked_memo = (now, value)
        return value


# --------------------------------------------------------------------------- #
# Construction helpers
# --------------------------------------------------------------------------- #
def app_root() -> Path:
    """The folder holding start.py (repository root, or the installed package)."""
    return Path(__file__).resolve().parents[2]


def is_installed_layout(root: Path | None = None) -> bool:
    root = root or app_root()
    return (root / "__init__.py").is_file() and (root / "cli.py").is_file()


def current_version(root: Path | None = None) -> str:
    root = root or app_root()
    try:
        if not is_installed_layout(root):
            import tomllib  # noqa: PLC0415

            return str(tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
                       ["project"]["version"])
    except Exception:  # noqa: BLE001
        pass
    try:
        from importlib.metadata import version  # noqa: PLC0415

        return version("imurun")
    except Exception:  # noqa: BLE001
        return "0+unknown"


def default_data_root() -> Path:
    env = os.environ.get("IMURUN_DATA_ROOT")
    return Path(env).expanduser() if env else Path.home() / ".imurun"


def engine_paths(root: Path, data_root: Path) -> list[Path]:
    names = ["fish.dll"] if sys.platform == "win32" else (
        ["libfish.dylib"] if sys.platform == "darwin" else ["libfish.so", "fish.so"])
    dirs = [root / "bin", root / "model" / "fish" / "build", data_root / "bin"]
    env = os.environ.get("FISH_LIBRARY")
    out = [Path(env)] if env else []
    out += [d / n for d in dirs for n in names]
    return [p for p in out if p.is_file()]


def make_manager(data_root: Path | str | None = None, *, publish=None, **kw) -> UpdateManager:
    root = Path(kw.pop("root", None) or app_root())
    home = Path(data_root) if data_root else default_data_root()
    installed = is_installed_layout(root)
    return UpdateManager(
        data_root=home,
        current=kw.pop("current", None) or current_version(root),
        source=kw.pop("source", None) or detect_source(installed=installed, data_root=home),
        root=root,
        publish=publish,
        engine_paths=kw.pop("engine_paths", None) or engine_paths(root, home),
        **kw,
    )


def cached_notice(data_root: Path | str | None = None, current: str | None = None) -> str | None:
    """"update available: X" from the cache only (no network), for --version."""
    home = Path(data_root) if data_root else default_data_root()
    try:
        data = json.loads((home / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    latest = data.get("latest") if isinstance(data, dict) else None
    current = current or current_version()
    if is_newer(latest, current):
        return f"update available: {latest} (run: imurun update)"
    return None
