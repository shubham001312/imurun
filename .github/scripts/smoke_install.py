"""Install a built wheel into a fresh venv and prove the `imurun` command works.

    python .github/scripts/smoke_install.py dist/imurun-*.whl [--expect-engine]

Runs from a temporary directory outside the checkout: `imurun --version`,
then `imurun --no-open --port <free>` until GET /health answers 200, then
stops it. Fails if the server prints a traceback, if anything new appears
inside site-packages/imurun (the app must write only to its data root), or,
with --expect-engine, if the bundled engine library is not found.
"""
from __future__ import annotations

import argparse
import glob
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import venv
from pathlib import Path


def main() -> int:
    # The server's log uses symbols a cp1252 console cannot print.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("wheel")
    ap.add_argument("--expect-engine", action="store_true")
    args = ap.parse_args()
    wheels = glob.glob(args.wheel)
    if len(wheels) != 1:
        print(f"expected exactly one wheel for {args.wheel!r}, found {wheels}")
        return 1
    wheel = Path(wheels[0]).resolve()

    work = Path(tempfile.mkdtemp(prefix="imurun-smoke-"))
    env_dir, home = work / "venv", work / "home"
    home.mkdir()
    venv.create(env_dir, with_pip=True)
    bindir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    py = bindir / ("python.exe" if os.name == "nt" else "python")
    exe = bindir / ("imurun.exe" if os.name == "nt" else "imurun")
    subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", str(wheel)], check=True)

    version = subprocess.run([str(exe), "--version"], capture_output=True, text=True, cwd=work, check=True)
    print(version.stdout.strip())
    pkg = Path(subprocess.run([str(py), "-c", "import imurun, os; print(os.path.dirname(imurun.__file__))"],
                              capture_output=True, text=True, check=True).stdout.strip())

    def snapshot() -> set[str]:
        return {p.relative_to(pkg).as_posix() for p in pkg.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts}

    before = snapshot()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, IMURUN_DATA_ROOT=str(home), BROWSER="none", PYTHONIOENCODING="utf-8")
    env.pop("FISH_LIBRARY", None)
    env.pop("PYTHONPATH", None)
    proc = subprocess.Popen([str(exe), "--no-open", "--port", str(port)], cwd=work, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    healthy = False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                healthy = r.status == 200
                break
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.5)
    page_ok = False
    if healthy:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            page_ok = r.status == 200 and b"<html" in r.read().lower()
    proc.terminate()
    try:
        out, err = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    print(out[-3000:])
    print(err[-3000:], file=sys.stderr)
    new = sorted(snapshot() - before)

    problems = []
    if not healthy:
        problems.append("server never answered /health")
    if healthy and not page_ok:
        problems.append("GET / did not return the UI")
    if "Traceback" in err:
        problems.append("traceback on stderr")
    if new:
        problems.append(f"files written into site-packages/imurun: {new}")
    if args.expect_engine:
        if "Engine: FISH library not found" in out:
            problems.append("wheel should bundle the engine but none was found")
        load = subprocess.run(
            [str(py), "-c", "import ctypes, glob, os, imurun; "
             "libs = glob.glob(os.path.join(os.path.dirname(imurun.__file__), 'bin', '*fish*')); "
             "assert libs, 'no library in imurun/bin'; ctypes.CDLL(libs[0]); print('loaded', libs[0])"],
            capture_output=True, text=True)
        print(load.stdout.strip(), load.stderr.strip()[-500:])
        if load.returncode != 0:
            problems.append("the bundled engine library does not load (ctypes.CDLL failed)")
    for p in problems:
        print(f"SMOKE FAIL: {p}")
    if not problems:
        print(f"SMOKE OK: {wheel.name}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
