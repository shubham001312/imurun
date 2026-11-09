"""install.py: the global `imurun` command shim (no registry writes: fake editor)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import install


class FakeRegistry:
    def __init__(self, value=r"C:\Windows;%USERPROFILE%\bin"):
        self.value = value
        self.appends = 0

    def read(self):
        return self.value

    def append(self, folder):
        if install.path_contains(self.value, folder):
            return False
        self.value = f"{self.value};{folder}"
        self.appends += 1
        return True


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("IMURUN_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("IMURUN_SHIM_DIR", raising=False)
    monkeypatch.delenv("IMURUN_NO_SHIM", raising=False)
    monkeypatch.delenv("IMURUN_LAUNCHER", raising=False)
    monkeypatch.delenv("CI", raising=False)
    # pytest re-sets PYTEST_CURRENT_TEST for every phase, so the guard itself is
    # replaced here (test_disabled_under_ci_pytest_npm covers the real one).
    monkeypatch.setattr(install, "shim_disabled", lambda: None)
    monkeypatch.setattr(install.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    # Never touch the real registry from a test.
    monkeypatch.setattr(install, "WindowsUserPath", lambda: pytest.fail("real registry used"))
    return tmp_path


def test_windows_shim_location_text_and_state(home, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    monkeypatch.setenv("PATH", r"C:\Windows")
    reg = FakeRegistry()
    rec = install.ensure_command(registry=reg, quiet=True)
    path = home / "LocalAppData" / "Programs" / "imurun" / "bin" / "imurun.cmd"
    assert rec["ok"] and rec["path"] == str(path) and path.is_file()
    text = path.read_bytes().decode("utf-8")
    assert text.startswith("@echo off") and "\r\n" in text
    assert 'set "IMURUN_SUPERVISED=1"' in text and '"%IMURUN_RC%"=="75"' in text and "goto run" in text
    target = install.shim_target()
    assert f'"{target[0]}"' in text and text.count("%*") == 1
    assert rec["path_added"] is True and rec["on_user_path"] is True
    assert reg.value.endswith(str(path.parent))
    # The running process can use it right away.
    assert install.path_contains(os.environ["PATH"], path.parent)
    state = json.loads((home / "data" / "install-state.json").read_text())
    assert state["command"]["path"] == str(path) and state["command"]["target"] == target

    # Idempotent: no second PATH entry, no rewrite.
    rec2 = install.ensure_command(registry=reg, quiet=True)
    assert rec2["path_added"] is False and reg.appends == 1 and not rec2.get("changed")


def test_checkout_target_is_venv_python_and_start_py(monkeypatch):
    monkeypatch.setattr(install, "INSTALLED", False)
    target = install.shim_target()
    assert target[1] == str(install.ROOT / "start.py") and Path(target[0]).exists()


def test_installed_target_prefers_console_script(tmp_path, monkeypatch):
    monkeypatch.setattr(install, "INSTALLED", True)
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    exe = scripts / ("imurun.exe" if install.IS_WINDOWS else "imurun")
    exe.write_text("x")
    monkeypatch.setattr(install, "_installed_console_script", lambda: exe)
    assert install.shim_target() == [str(exe)]
    monkeypatch.setattr(install, "_installed_console_script", lambda: None)
    assert install.shim_target() == [sys.executable, "-m", "imurun"]


def test_posix_shim_and_path_hint(home, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(install, "shim_target", lambda: ["/opt/my env/python", "/src/start.py"])
    rec = install.ensure_command(quiet=True)
    path = home / "home" / ".local" / "bin" / "imurun"
    assert rec["ok"] and path.is_file()
    text = path.read_text()
    assert text.startswith("#!/bin/sh") and "'/opt/my env/python' /src/start.py \"$@\"" in text
    assert '[ "$rc" -eq 75 ]' in text
    assert rec["on_user_path"] is False and "~/.zshrc" in rec["hint"]
    assert install.shim_target_of(text) == "/opt/my env/python"


def test_disabled_under_ci_pytest_npm(monkeypatch):
    monkeypatch.delenv("IMURUN_SHIM_DIR", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert install.shim_disabled() == "running under pytest"
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    monkeypatch.setenv("IMURUN_LAUNCHER", "npm")
    monkeypatch.delenv("CI", raising=False)
    assert "npm" in install.shim_disabled()
    monkeypatch.setenv("IMURUN_NO_SHIM", "1")
    assert install.shim_disabled() == "IMURUN_NO_SHIM=1"
    assert install.ensure_command(quiet=True) == {"ok": False, "skipped": "IMURUN_NO_SHIM=1"}


def test_quick_check_repairs_missing_shim(home, monkeypatch):
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    reg = FakeRegistry()
    monkeypatch.setattr(install, "WindowsUserPath", lambda: reg)
    install.ensure_command(quiet=True)
    path = install.shim_path()
    path.unlink()
    assert install.repair_command_if_missing() is True and path.is_file()
    assert install.repair_command_if_missing() is False  # present and healthy: just a stat


def test_path_contains_normalises():
    assert install.path_contains(r"C:\a;C:\Users\X\Programs\imurun\bin\;D:\b", r"c:\users\x\programs\imurun\bin")
    assert not install.path_contains("", r"C:\x")


@pytest.mark.skipif(os.name != "nt", reason="runs the generated .cmd with cmd.exe")
def test_generated_cmd_runs_and_restarts_on_75(tmp_path, monkeypatch):
    """The real shim under cmd.exe: arguments pass through; exit 75 runs it again."""
    script = tmp_path / "fake.py"
    counter = tmp_path / "count"
    script.write_text(
        "import os, sys, pathlib\n"
        f"p = pathlib.Path(r'{counter}')\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\n"
        "print('ARGS', sys.argv[1:], os.environ.get('IMURUN_SUPERVISED'), os.environ.get('IMURUN_AFTER_UPDATE'))\n"
        "sys.exit(75 if n == 0 else 3)\n")
    monkeypatch.setattr(install, "IS_WINDOWS", True)
    shim = tmp_path / "imurun.cmd"
    shim.write_bytes(install.shim_text([sys.executable, str(script)]).encode())
    res = subprocess.run(["cmd", "/c", str(shim), "--version", "a b"], capture_output=True, text=True,
                         cwd=str(tmp_path), timeout=60)
    assert res.returncode == 3
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("ARGS")]
    assert lines == ["ARGS ['--version', 'a b'] 1 None", "ARGS ['--version', 'a b'] 1 1"]
