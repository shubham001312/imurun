#!/usr/bin/env node
/*
 * IMURUN npm launcher.
 *
 *   npx imurun [args]            or   npm i -g imurun && imurun [args]
 *
 * IMURUN itself is a Python app published to PyPI. This launcher, with no
 * dependencies of its own:
 *   1. finds Python 3.11+ (py -3.x / python3.x / python3 / python),
 *   2. creates ~/.imurun/venv if it is missing or broken,
 *   3. installs imurun==<this package's version> into it when it is missing or
 *      OLDER than this launcher (a newer one installed by `imurun update` / the
 *      in-app update is kept, never downgraded),
 *   4. runs `python -m imurun <args>` from that venv and passes its exit code on;
 *      exit code 75 means "an update was applied, start me again": the launcher
 *      runs it again with IMURUN_AFTER_UPDATE=1 (IMURUN_SUPERVISED=1 tells the
 *      app a launcher does this; docs/setup/README.md "Updates");
 *      on its first run `imurun` installs the rest itself (voice, engine check,
 *      GPU check; see docs/setup/README.md), recorded in ~/.imurun/install-state.json.
 *
 * Environment:
 *   IMURUN_PYTHON=<path>     use this interpreter to create the venv
 *   IMURUN_VENV=<dir>        venv location (default ~/.imurun/venv)
 *   IMURUN_WHEEL=<path>      install from this wheel/sdist instead of PyPI
 *   IMURUN_REINSTALL=1       reinstall even when the version already matches
 *   IMURUN_NO_WINDOW_EXTRA=1 do not install the native-window extra (pywebview)
 *
 * No telemetry: the only network traffic is pip talking to PyPI (or your
 * configured index) when something has to be installed.
 */
"use strict";

const { spawnSync, spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const VERSION = require("../package.json").version;
const IS_WIN = process.platform === "win32";
const MIN = [3, 11];
const EXIT_RESTART = 75; // the app asks to be started again after an update
const PREFERRED_MAX = [3, 14]; // newest minor with binary wheels for aiohttp

function say(msg) {
  process.stderr.write(`imurun: ${msg}\n`);
}

function dataRoot() {
  return process.env.IMURUN_DATA_ROOT || path.join(os.homedir(), ".imurun");
}

function venvDir() {
  return process.env.IMURUN_VENV || path.join(dataRoot(), "venv");
}

function venvPython(dir) {
  return IS_WIN ? path.join(dir, "Scripts", "python.exe") : path.join(dir, "bin", "python");
}

function run(cmd, args, opts) {
  return spawnSync(cmd, args, Object.assign({ encoding: "utf8", windowsHide: true }, opts || {}));
}

function cmp(a, b) {
  return a[0] - b[0] || a[1] - b[1];
}

/** [major, minor] of an interpreter, or null if it does not run. */
function pythonVersion(cmd, preArgs) {
  const r = run(cmd, [...preArgs, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], {
    timeout: 20000,
  });
  if (r.error || r.status !== 0) return null;
  const m = /^(\d+)\.(\d+)/.exec((r.stdout || "").trim());
  return m ? [Number(m[1]), Number(m[2])] : null;
}

function candidates() {
  const list = [];
  if (process.env.IMURUN_PYTHON) list.push([process.env.IMURUN_PYTHON, []]);
  if (IS_WIN) {
    for (const v of ["3.12", "3.13", "3.11", "3.14"]) list.push(["py", [`-${v}`]]);
    list.push(["py", ["-3"]], ["python", []], ["python3", []]);
  } else {
    for (const v of ["3.12", "3.13", "3.11", "3.14"]) list.push([`python${v}`, []]);
    list.push(["python3", []], ["python", []]);
  }
  return list;
}

/** Best interpreter: 3.11-3.14 first, any newer 3.x as a last resort. */
function findPython() {
  let fallback = null;
  for (const [cmd, pre] of candidates()) {
    const v = pythonVersion(cmd, pre);
    if (!v || cmp(v, MIN) < 0) continue;
    if (cmp(v, PREFERRED_MAX) <= 0) return { cmd, pre, v };
    if (!fallback) fallback = { cmd, pre, v };
  }
  return fallback;
}

function pythonMissing() {
  say("Python 3.11 or newer is required and was not found.");
  if (IS_WIN) {
    say("  Install it:  winget install -e --id Python.Python.3.12");
    say("  or download: https://www.python.org/downloads/windows/  (tick 'Add python.exe to PATH')");
  } else if (process.platform === "darwin") {
    say("  Install it:  brew install python@3.12");
    say("  or download: https://www.python.org/downloads/macos/");
  } else {
    say("  Install it:  sudo apt install python3 python3-venv   (dnf/pacman: python3)");
    say("  or see:      https://www.python.org/downloads/");
  }
  say("Then open a new terminal and run imurun again (or set IMURUN_PYTHON=<path>).");
  process.exit(1);
}

function venvWorks(dir) {
  const py = venvPython(dir);
  return fs.existsSync(py) && pythonVersion(py, []) !== null;
}

function createVenv(dir) {
  const found = findPython();
  if (!found) pythonMissing();
  const { cmd, pre, v } = found;
  if (cmp(v, PREFERRED_MAX) > 0) {
    say(`only Python ${v.join(".")} was found; some packages may have no wheels for it yet.`);
  }
  if (fs.existsSync(dir)) {
    say(`the environment at ${dir} is broken; recreating it.`);
    fs.rmSync(dir, { recursive: true, force: true });
  }
  fs.mkdirSync(path.dirname(dir), { recursive: true });
  say(`creating ${dir} with Python ${v.join(".")} ...`);
  const r = run(cmd, [...pre, "-m", "venv", dir], { stdio: "inherit" });
  if (r.error || r.status !== 0 || !venvWorks(dir)) {
    say("could not create the virtual environment.");
    if (!IS_WIN && process.platform !== "darwin") {
      say("  On Debian/Ubuntu install the venv module:  sudo apt install python3-venv");
    }
    process.exit(1);
  }
  run(venvPython(dir), ["-m", "pip", "install", "--upgrade", "--quiet", "pip"], { stdio: "inherit" });
}

/** Numeric release parts of "0.4.1rc1" -> [0, 4, 1]; pre-release suffixes ignored. */
function versionParts(v) {
  const m = /^v?(\d+(?:\.\d+)*)/.exec(String(v || "").trim());
  return m ? m[1].split(".").map(Number) : null;
}

/** True when `have` is an older release than `want` (unknown counts as older). */
function olderThan(have, want) {
  const a = versionParts(have);
  const b = versionParts(want);
  if (!a) return true;
  if (!b) return false;
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const d = (a[i] || 0) - (b[i] || 0);
    if (d !== 0) return d < 0;
  }
  return false;
}

function installedVersion(py) {
  const r = run(py, ["-c", "import importlib.metadata as m; print(m.version('imurun'))"], {
    timeout: 30000,
  });
  return r.status === 0 ? (r.stdout || "").trim() : null;
}

/** Version in a wheel or sdist file name (imurun-0.3.0-py3-none-any.whl,
 *  imurun-0.3.0.tar.gz), or null when the name does not follow that form. */
function distVersion(file) {
  const m = /^imurun-([^-]+?)(?:-.+\.whl|\.tar\.gz|\.zip)$/i.exec(path.basename(file));
  return m ? m[1] : null;
}

function pipInstall(py) {
  const wheel = process.env.IMURUN_WHEEL;
  const base = wheel ? path.resolve(wheel) : `imurun==${VERSION}`;
  // The native window (pywebview) installs cleanly on Windows and macOS; on
  // Linux it needs system GTK/Qt packages, so it stays opt-in there.
  const wantWindow =
    (IS_WIN || process.platform === "darwin") && process.env.IMURUN_NO_WINDOW_EXTRA !== "1";
  const spec = (s, extra) => {
    if (!extra) return s;
    if (wheel) return `${s}[${extra}]`;
    return s.replace(/^imurun==/, `imurun[${extra}]==`);
  };
  // stdout streams (progress); stderr is captured so a known failure can be
  // explained, and echoed unchanged.
  let lastErr = "";
  const pip = (target) => {
    const r = run(py, ["-m", "pip", "install", "--upgrade", "--disable-pip-version-check", target], {
      stdio: ["inherit", "inherit", "pipe"],
    });
    lastErr = r.stderr || "";
    if (lastErr) process.stderr.write(lastErr);
    return r;
  };
  say(`installing ${wheel ? base : `imurun ${VERSION}`} into ${venvDir()} ...`);
  let r = pip(spec(base, wantWindow ? "window" : ""));
  if (r.status !== 0 && wantWindow) {
    say("the native-window extra failed to install; retrying without it (IMURUN opens in the browser).");
    r = pip(base);
  }
  if (!r.error && r.status === 0 && process.env.IMURUN_REINSTALL === "1") {
    // pip skips a wheel whose version is already installed; replace the app
    // files themselves without touching the dependencies.
    r = run(py, ["-m", "pip", "install", "--force-reinstall", "--no-deps", "--disable-pip-version-check", base], {
      stdio: "inherit",
    });
  }
  if ((r.error || r.status !== 0) && !wheel && /No matching distribution found for imurun/i.test(lastErr)) {
    say(`imurun ${VERSION} is not on PyPI (yet). This npm launcher needs the matching Python release.`);
    say("  If you just installed it, the release may still be uploading: try again in a few minutes.");
    say("  Otherwise install a built wheel directly:  set IMURUN_WHEEL=<path to imurun-*.whl>  and rerun,");
    say("  or use the source checkout (run.bat / ./run.sh). Releases: https://github.com/shubham001312/imurun/releases");
    process.exit(1);
  }
  if (r.error || r.status !== 0) {
    say("pip could not install IMURUN. Check your internet connection or proxy (HTTPS_PROXY),");
    say(`or install it yourself:  "${py}" -m pip install imurun==${VERSION}`);
    process.exit(1);
  }
}

function main() {
  const args = process.argv.slice(2);
  const dir = venvDir();

  if (args[0] === "--launcher-info") {
    const found = findPython();
    console.log(`imurun npm launcher ${VERSION}`);
    console.log(`venv:   ${dir} (${venvWorks(dir) ? "ok" : "not created"})`);
    console.log(`python: ${found ? `${found.cmd} ${found.pre.join(" ")} (${found.v.join(".")})` : "not found"}`);
    return 0;
  }

  if (!venvWorks(dir)) createVenv(dir);
  const py = venvPython(dir);
  const have = installedVersion(py);
  // With IMURUN_WHEEL the wanted version is the wheel's own (from its file
  // name), not this launcher's; if the name does not tell, install anyway
  // rather than keep running whatever is already in the venv. That is an
  // explicit choice, so it may downgrade. Without a wheel, only a MISSING or
  // OLDER install is replaced: a newer one put there by `imurun update` / the
  // in-app update is kept, never downgraded to this launcher's version.
  const wheel = process.env.IMURUN_WHEEL;
  const wanted = wheel ? distVersion(wheel) : VERSION;
  const stale = wheel ? !wanted || have !== wanted : olderThan(have, wanted);
  if (process.env.IMURUN_REINSTALL === "1" || !have || stale) {
    pipInstall(py);
  }

  // Ctrl+C reaches the child directly (same console / process group); the
  // launcher only waits for it and passes the exit status on.
  const ignore = () => {};
  process.on("SIGINT", ignore);
  let child = null;
  process.on("SIGTERM", () => child && child.kill("SIGTERM"));
  const launch = (afterUpdate) => {
    // IMURUN_LAUNCHER/IMURUN_VENV tell the updater (services/update) that this
    // venv belongs to the npm launcher, so it updates it with that venv's pip.
    const env = Object.assign({}, process.env, {
      IMURUN_SUPERVISED: "1",
      IMURUN_LAUNCHER: "npm",
      IMURUN_VENV: dir,
    });
    if (afterUpdate) env.IMURUN_AFTER_UPDATE = "1";
    // -P: keep the current directory off sys.path, so running `imurun` inside an
    // untrusted folder cannot import that folder's imurun/ or json.py (Python 3.11+).
    child = spawn(py, ["-P", "-m", "imurun", ...args], { stdio: "inherit", env });
    child.on("error", (err) => {
      say(`could not start ${py}: ${err.message}`);
      process.exit(1);
    });
    child.on("exit", (code, signal) => {
      if (code === EXIT_RESTART) {
        say("update applied; starting the new version ...");
        launch(true);
        return;
      }
      process.exit(code === null ? (signal === "SIGINT" ? 130 : 1) : code);
    });
  };
  launch(false);
  return null;
}

const rc = main();
if (typeof rc === "number") process.exit(rc);
