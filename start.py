#!/usr/bin/env python3
"""
IMURUN - Local AI Chat with FISH Engine
Single entry point to start the server (Windows / macOS / Linux).

    python start.py                 # start; open a native window (or the browser)
    python start.py --browser       # open the default browser instead of a window
    python start.py --no-open       # server only
    python start.py --bootstrap     # create/repair .venv, install deps, then start
    python start.py --version       # print the version
    python start.py --get-engine    # download the prebuilt FISH engine for this OS
    python start.py --skip-setup    # do not run the automatic install step

Restart after an update (services/update, POST /api/update {action: "apply"}):
the update service sets services.update.RESTART; the wait loop below stops
the server (which closes the model) and then
  * exits with code 75 (EXIT_RESTART) when a launcher supervises this process
    (IMURUN_SUPERVISED=1: the `imurun` shim written by install.py and the npm
    launcher run the command again on 75, with IMURUN_AFTER_UPDATE=1), or
  * starts a fresh interpreter with the same arguments plus
    --restart-after-update itself, waits for it and passes its exit code on.
After an update the browser is not opened again (the open tab reloads itself);
a native window is closed and reopened.

Every start runs install.quick_check() (a few ms: state file, find_spec,
stat; no engine load). When something is missing (first run, new version,
missing package, changed engine) it runs `install.py --here --auto` with the
same interpreter, then continues; only a core-package failure stops it.

`pip install imurun` installs the same thing as the `imurun` command
(imurun/cli.py), with this file bundled inside the package. In that installed
layout nothing is ever written next to this file: data, logs, models and a
downloaded engine all go under ~/.imurun (or IMURUN_DATA_ROOT).

Only the standard library is imported at module level so that a missing
dependency produces a clear message instead of a traceback.
"""

import argparse
import importlib
import importlib.util
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Add project root to path
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import install  # noqa: E402 - stdlib-only helpers that live next to this file

DEFAULT_PORT = 8732
PROJECT_PACKAGES = ("services", "model")
REEXEC_ENV = "IMURUN_REEXEC"
EXIT_RESTART = 75                      # == services.update.EXIT_RESTART
SUPERVISED_ENV = "IMURUN_SUPERVISED"   # a launcher restarts us on EXIT_RESTART
AFTER_UPDATE_ENV = "IMURUN_AFTER_UPDATE"


# --------------------------------------------------------------------------- #
# Dependency checks
# --------------------------------------------------------------------------- #
def _print_missing(missing):
    """Explain a missing dependency (never a raw traceback)."""
    ui = install.ui
    py = sys.executable
    ui.line()
    ui.fail(ui.c("IMURUN cannot start: required Python packages are missing", "bold"))
    for dist in missing:
        ui.line(f"      {ui.c(ui.sym['fail'], 'red')} {dist}")
    ui.hint(f"Interpreter: {py} (Python {sys.version.split()[0]})")
    ui.line()
    if install.INSTALLED:
        ui.info("Fix it with:")
        ui.line("      " + ui.c(f'"{py}" -m pip install --upgrade --force-reinstall imurun', "cyan"))
        ui.hint("or start once with:  imurun --install   (installs the missing packages here)")
        ui.line()
        return
    ui.info("Fix it with ONE of:")
    ui.line(f"      {ui.c('python start.py --bootstrap', 'cyan')}   create .venv, install, start")
    ui.line(f"      {ui.c('python install.py', 'cyan')}             create .venv and install")
    here_cmd = '"%s" -m pip install -r requirements.txt' % py
    ui.line("      " + ui.c(here_cmd, "cyan"))
    ui.hint("See docs/setup/README.md for details.")
    ui.line()


def _reexec(python):
    """Run this script again under ``python`` and exit with its status."""
    env = dict(os.environ)
    env[REEXEC_ENV] = "1"
    cmd = [str(python), os.path.abspath(__file__), *sys.argv[1:]]
    install.ui.info("Using project environment " + install.ui.c(str(python), "grey"))
    proc = subprocess.Popen(cmd, env=env)
    while True:
        try:
            sys.exit(proc.wait())
        except KeyboardInterrupt:
            # The child shares the console and receives Ctrl+C itself; keep
            # waiting so it can shut the server down cleanly.
            continue


def _want_window(args):
    """Install pywebview during setup unless the user clearly does not want it."""
    return args.window or (not args.browser and not args.no_open and install.window_supported_by_default())


def _setup_disabled():
    return os.environ.get("IMURUN_SKIP_SETUP", "0") == "1"


def run_install_step(args, reasons, *, here=True):
    """Run install.py (same interpreter, non-interactive) with progress on the console.

    Returns True when it finished; the caller re-checks what it needs. Only a
    failure to install the core packages stops IMURUN (install.py exits 1 and
    names the failing step); optional parts are reported and recorded.
    """
    ui = install.ui
    ui.info("Setting up IMURUN: " + "; ".join(reasons))
    cmd = [sys.executable, str(Path(install.__file__).resolve()), "--auto",
           "--window" if _want_window(args) else "--no-window"]
    if here:
        cmd.append("--here")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        rc = subprocess.call(cmd, env=env)
    except OSError as exc:
        ui.fail(f"Could not run the installer ({exc}).")
        return False
    importlib.invalidate_caches()
    return rc == 0


def _bootstrap_venv(args):
    """Create or repair .venv, install everything into it, re-exec from it."""
    if not run_install_step(args, ["creating the project .venv"], here=False):
        install.ui.fail("Setup failed; see the step above. Retry with:  python install.py")
        sys.exit(2)
    _reexec(install.venv_python())


def ensure_dependencies(args):
    """Return normally only when every required package can be imported.

    Missing packages are installed automatically (install.py, non-interactive)
    unless IMURUN_SKIP_SETUP=1.
    """
    missing = install.missing_modules()
    if install.INSTALLED:
        # pip installed us: there is no project .venv to create or switch to
        # (it would land in site-packages); install into this interpreter.
        if args.bootstrap:
            install.ui.warn("--bootstrap only applies to a source checkout; ignoring it.")
        if not missing:
            return
        if not _setup_disabled():
            run_install_step(args, ["missing Python packages: " + ", ".join(missing)])
            if not install.missing_modules():
                return
        _print_missing(install.missing_modules())
        sys.exit(2)
    if args.bootstrap and not install.in_venv() and os.environ.get(REEXEC_ENV) != "1":
        # Explicit request: always run from the project venv.
        if install.venv_healthy() and not install.missing_modules(install.venv_python()):
            _reexec(install.venv_python())
        _bootstrap_venv(args)
    if not missing:
        return

    # 1. A healthy project .venv that already has everything: use it.
    if not install.in_venv() and os.environ.get(REEXEC_ENV) != "1":
        problem = install.venv_problem()
        if problem:
            install.ui.warn(f"Ignoring .venv because {problem}.")
        elif install.VENV.exists() and not install.missing_modules(install.venv_python()):
            _reexec(install.venv_python())

    if _setup_disabled():
        _print_missing(missing)
        sys.exit(2)

    # 2. Inside a venv (the project .venv, or one the user made), or --install:
    #    install into the running interpreter and continue.
    if install.in_venv() or sys.prefix != sys.base_prefix or args.install:
        run_install_step(args, ["missing Python packages: " + ", ".join(missing)])
        still = install.missing_modules()
        if still:
            _print_missing(still)
            sys.exit(2)
        return

    # 3. A system Python in a checkout: create .venv, install, run from it.
    if os.environ.get(REEXEC_ENV) != "1":
        _bootstrap_venv(args)
    _print_missing(missing)
    sys.exit(2)


def ensure_installed(args):
    """Fast check on every start; run the install step only when something is missing.

    install.quick_check costs a few ms when all is in place (state file +
    find_spec + stat; no engine load, no heavy imports).
    """
    if _setup_disabled() or args.skip_setup:
        return
    reasons = install.quick_check()
    if not reasons:
        return
    if not run_install_step(args, reasons):
        if install.missing_modules():
            install.ui.fail("Setup failed at step 1 (Python packages); IMURUN cannot start.")
            sys.exit(2)
        install.ui.warn("Setup reported a problem (above); starting anyway.")


def _import_server():
    """Import the server modules, turning a missing package into a message."""
    try:
        from services.config import load_config
        from services.server.app import build_server, write_runtime, clear_runtime
    except ModuleNotFoundError as exc:
        top = (exc.name or "").split(".")[0]
        if top and top not in PROJECT_PACKAGES:
            dist = install.REQUIRED_MODULES.get(top, top)
            _print_missing([dist])
            sys.exit(2)
        raise
    return load_config, build_server, write_runtime, clear_runtime


# --------------------------------------------------------------------------- #
# UI opening
# --------------------------------------------------------------------------- #
def has_webview():
    try:
        return importlib.util.find_spec(install.WINDOW_MODULE[0]) is not None
    except (ImportError, ValueError):
        return False


def after_update(args):
    return bool(getattr(args, "restart_after_update", False)) or os.getenv(AFTER_UPDATE_ENV) == "1"


def resolve_open_mode(args):
    if args.no_open or os.getenv("IMURUN_NO_BROWSER", "0") == "1":
        return "none"
    if args.browser:
        # After an update the open tab polls /health and reloads itself.
        return "none" if after_update(args) else "browser"
    if has_webview():
        return "window"
    if after_update(args):
        return "none"
    if args.window:
        print(
            "Native window support is not installed; opening the browser instead.\n"
            "Install it with:  pip install pywebview   (or: python install.py --window)",
            file=sys.stderr,
        )
    return "browser"


def open_browser_later(url):
    def _open():
        time.sleep(0.6)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - a missing browser must not stop the server
            pass

    threading.Thread(target=_open, name="imurun-browser", daemon=True).start()


# The native window lives in window.py and runs as a child process, so no
# UI-stack failure (icon, WebView2, GTK, driver) can reach the server.
WINDOW_SCRIPT = Path(__file__).resolve().parent / "window.py"
# A window that dies this quickly did not get to show anything useful.
WINDOW_STARTUP_GRACE = 6.0


def run_window(url, stop=None):
    """Show the UI in a native window; return when the user closes it.

    The window runs in a separate process (`window.py`): nothing that goes
    wrong inside the UI stack can take the server down. If the window process
    exits with an error, or dies within the first few seconds, this returns
    False and the caller opens the browser while the server keeps serving.
    """
    if not WINDOW_SCRIPT.is_file():
        print("Native window unavailable (window.py missing).", file=sys.stderr)
        return False
    try:
        # The window gets our PID and closes itself if we disappear; we also
        # terminate it on every exit path below, so no orphan window is ever
        # left pointing at a dead server.
        proc = subprocess.Popen(
            [sys.executable, str(WINDOW_SCRIPT), url, "IMURUN", str(os.getpid())],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Native window could not be launched ({exc}).", file=sys.stderr)
        return False

    import atexit

    def _close_window() -> None:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    atexit.register(_close_window)

    print("Close the IMURUN window (or press Ctrl+C) to stop.\n", flush=True)
    started = time.monotonic()
    try:
        while True:
            try:
                _, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if stop is not None and stop.is_set():
                    # An update was applied: close this window; the restarted
                    # process opens a new one.
                    _close_window()
                    return True
    except KeyboardInterrupt:
        _close_window()
        return True
    except BaseException:
        _close_window()
        raise
    lived = time.monotonic() - started
    if proc.returncode == 0 and lived >= WINDOW_STARTUP_GRACE:
        return True
    reason = (err or "").strip().splitlines()
    detail = reason[-1] if reason else f"exit code {proc.returncode}"
    print(
        f"The native window could not be shown ({detail}).\n"
        "Opening the browser instead; the server is unaffected. "
        "See docs/setup/README.md (pywebview backends).",
        file=sys.stderr,
    )
    try:
        (Path.home() / ".imurun" / "logs").mkdir(parents=True, exist_ok=True)
        (Path.home() / ".imurun" / "logs" / "window.log").write_text(err or "", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return False


def wait_forever(stop=None):
    while stop is None or not stop.is_set():
        time.sleep(0.5)


def _restart_event():
    """services.update.RESTART, or an Event that never fires if it is unavailable."""
    try:
        from services.update import RESTART

        return RESTART
    except Exception:  # noqa: BLE001
        return threading.Event()


def _cleanup_old_engines():
    """Remove `fish.dll.old-<pid>` copies left by an update (services/update)."""
    try:
        from services.update import remove_stale_engine_copies

        remove_stale_engine_copies([Path(ROOT) / "bin", install.user_engine_dir()])
    except Exception:  # noqa: BLE001
        pass


def restart_command(argv=None):
    rest = [a for a in (sys.argv[1:] if argv is None else argv) if a != "--restart-after-update"]
    if install.INSTALLED:
        return [sys.executable, "-m", "imurun", *rest, "--restart-after-update"]
    return [sys.executable, os.path.abspath(__file__), *rest, "--restart-after-update"]


def restart_after_update(argv=None):
    """Start the updated version (see the module docstring for the mechanism)."""
    ui = install.ui
    if os.environ.get(SUPERVISED_ENV) == "1":
        ui.info("Update applied; the launcher starts the new version.")
        sys.exit(EXIT_RESTART)
    ui.info("Update applied; starting the new version...")
    env = dict(os.environ)
    env.pop(REEXEC_ENV, None)
    try:
        proc = subprocess.Popen(restart_command(argv), env=env)
    except OSError as exc:
        ui.fail(f"Could not restart ({exc}); start IMURUN again.")
        sys.exit(1)
    while True:
        try:
            sys.exit(proc.wait())
        except KeyboardInterrupt:
            continue


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _version_text():
    try:
        from imurun.cli import version_line

        return version_line()
    except Exception:  # noqa: BLE001
        return f"imurun {install.app_version()}"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="IMURUN - Local AI Chat with FISH Engine",
        epilog="commands:  imurun doctor   (check this machine)    "
               "imurun install [--force] [--no-voice] [--tuning]   (re-run setup)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Server port (default: {DEFAULT_PORT})")
    parser.add_argument("--model", type=str, help="Model path (overrides config)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    ui = parser.add_mutually_exclusive_group()
    ui.add_argument("--window", action="store_true",
                    help="open a native window (default when pywebview is installed)")
    ui.add_argument("--browser", action="store_true", help="open the default web browser")
    ui.add_argument("--no-open", action="store_true", help="do not open any UI")
    setup = parser.add_mutually_exclusive_group()
    setup.add_argument("--bootstrap", action="store_true",
                       help="create/repair .venv, install requirements and run from it")
    setup.add_argument("--install", action="store_true",
                       help="pip install missing requirements into the current Python")
    parser.add_argument("--skip-setup", action="store_true",
                        help="do not run the automatic install step (also: IMURUN_SKIP_SETUP=1)")
    parser.add_argument("--restart-after-update", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--get-engine", action="store_true",
                        help="download the prebuilt FISH engine for this OS into ~/.imurun/bin and exit")
    parser.add_argument("--version", action="version", version=_version_text())
    return parser.parse_args(argv)


def get_engine():
    ui = install.ui
    asset = install.engine_asset_name()
    version = install.app_version()
    ui.info(f"Downloading the FISH engine ({asset or 'unsupported platform'}) for IMURUN {version}...")
    try:
        path = install.download_engine(version)
    except RuntimeError as exc:
        ui.fail(str(exc))
        ui.hint(f"Releases: {install.RELEASES_URL}")
        return 1
    ui.ok(f"Engine saved to {ui.c(str(path), 'grey')}")
    return 0


def main(argv=None):
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw and raw[0] in ("doctor", "install", "update"):
        # The `imurun` shim of a checkout runs start.py; subcommands live in imurun/cli.py.
        from imurun.cli import main as cli_main

        sys.exit(cli_main(raw))
    args = parse_args(argv)
    install.use_system_certificates()  # OS trust store for every HTTPS call (unknown-CA failures)
    # IMURUN_STARTUP_TRACE=<seconds>: dump every thread's stack to stderr if
    # start-up has not finished by then (CI diagnostics for a hanging server;
    # the launcher test sets it so a failure shows where the server stuck).
    trace_after = os.environ.get("IMURUN_STARTUP_TRACE")
    if trace_after:
        try:
            import faulthandler

            faulthandler.dump_traceback_later(float(trace_after), repeat=False, exit=False)
        except (ValueError, RuntimeError):
            pass
    if args.get_engine:
        sys.exit(get_engine())

    if sys.version_info < install.MIN_PY:
        sys.exit(f"IMURUN needs Python {install.MIN_PY[0]}.{install.MIN_PY[1]}+, "
                 f"this is {sys.version.split()[0]}.")

    ensure_dependencies(args)
    ensure_installed(args)
    _cleanup_old_engines()

    # Set model path via env if provided
    if args.model:
        os.environ["FISH_MODEL"] = args.model
    if args.host != "127.0.0.1":
        os.environ["IMURUN_HOST"] = args.host
    if args.port != DEFAULT_PORT:
        os.environ["IMURUN_PORT"] = str(args.port)

    load_config, build_server, write_runtime, clear_runtime = _import_server()

    # Load config
    cfg = load_config()

    # Override with CLI args
    if args.model:
        cfg.fish.model_path = args.model
    if args.host != "127.0.0.1":
        cfg.host = args.host
    if args.port != DEFAULT_PORT:
        cfg.port = args.port

    ui = install.ui
    if ui.tty:
        ui.logo()
        ui.line()
    ui.info(f"Starting IMURUN on {cfg.host}:{cfg.port}")
    ui.info(f"Model: {cfg.fish.model_path or ui.c('(none - choose one in the UI)', 'grey')}")
    engine = install.find_engine_library()
    if engine is None:
        ui.warn("Engine: FISH library not found - the UI will start in 'provisioning' state.")
        if install.INSTALLED:
            for line in install.engine_build_instructions().splitlines():
                ui.hint(line)
        else:
            ui.hint("Run `python install.py --check` for build instructions, "
                    "or `python start.py --get-engine` for the prebuilt one.")
    else:
        ui.ok(f"Engine: {ui.c(str(engine), 'grey')}")
        # The bindings look in <root>/bin and model/fish/build only; a library
        # fetched with --get-engine lives in ~/.imurun/bin, so hand it over.
        if not os.environ.get("FISH_LIBRARY") and engine.parent == install.user_engine_dir():
            os.environ["FISH_LIBRARY"] = str(engine)

    if install.INSTALLED:
        # The model manager defaults to <app root>/models, which is inside
        # site-packages for a pip install. Pin it to the data root first.
        try:
            from services.model_manager import get_model_manager

            get_model_manager(str(Path(cfg.data_root) / "models"))
        except ImportError:
            pass

    # Build and start server
    try:
        server = build_server(root=cfg.data_root, port=cfg.port, config=cfg)
        server.start()
    except OSError as exc:
        ui.fail(f"Could not start the server on port {cfg.port}: {exc}")
        ui.hint("Is IMURUN already running? Try another port with --port.")
        sys.exit(1)

    _stop_on_signals()
    try:
        # Write runtime info
        runtime_path = write_runtime(cfg.data_root, server.port, server.token)
        # The server binds loopback; the token is injected into the served HTML.
        url = f"http://127.0.0.1:{server.port}/"
        ui.line()
        ui.ok(f"Server running at {ui.c(url, 'cyan', 'bold')}")
        ui.hint(f"Runtime config: {runtime_path}")

        restart = _restart_event()
        mode = resolve_open_mode(args)
        shown = mode == "window" and run_window(url, restart)
        if shown and not restart.is_set():
            ui.info("Window closed.")
            return
        if not restart.is_set():
            if mode in ("window", "browser"):
                open_browser_later(url)
            ui.line(ui.c("  Press Ctrl+C to stop", "grey"))
            ui.line()
            wait_forever(restart)
    except KeyboardInterrupt:
        ui.line()
        ui.info("Shutting down...")
    finally:
        server.stop()
        clear_runtime(cfg.data_root)
        ui.ok("Stopped.")
    if _restart_event().is_set():
        restart_after_update(argv)


def _stop_on_signals():
    """`kill`, logout/terminal close and Ctrl+Break take the same clean path as Ctrl+C.

    Without this, SIGTERM (what `kill`, systemd, launchd and most process
    managers send) ended the process at once: no server.stop(), and
    runtime/api.json kept a dead port and token for the next client to find.
    """
    import signal

    def _interrupt(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _interrupt)
        except (ValueError, OSError):  # not the main thread / not allowed here
            pass


if __name__ == "__main__":
    main()
