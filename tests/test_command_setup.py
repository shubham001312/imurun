"""install.ensure_command: the `imurun` command works from a new terminal however IMURUN
was installed, and setup never touches a file it did not write."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import install

POSIX = os.name != "nt"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch HOME, bin dir and PATH; nothing reaches the real profile."""
    h = tmp_path / "home"
    h.mkdir()
    # Inside the fake profile: on Windows a launcher folder outside the user
    # profile is refused (install._safe_dir), and USERPROFILE points at `h`.
    bin_dir = h / "bin"
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("IMURUN_DATA_ROOT", str(h / ".imurun"))
    monkeypatch.setenv("IMURUN_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("PATH", os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("IMURUN_NO_PATH", raising=False)
    monkeypatch.setattr(install.shutil, "which", lambda name, *a, **k: None)
    return h, bin_dir


def test_writes_launcher_without_touching_path_when_disabled(home, monkeypatch):
    h, bin_dir = home
    monkeypatch.setenv("IMURUN_NO_PATH", "1")
    info = install.ensure_command()
    shim = install.command_path()
    assert info["ok"] and shim.parent == bin_dir and shim.is_file()
    text = shim.read_text(encoding="utf-8")
    assert install.SHIM_MARK in text and f"{install._py_token()} -P -m imurun" in text
    assert "IMURUN_NO_PATH" in info["detail"]
    assert not (h / ".profile").exists() and not (h / ".bashrc").exists()


@pytest.mark.skipif(not POSIX, reason="runs the POSIX launcher")
def test_launcher_runs_imurun(home, monkeypatch):
    monkeypatch.setenv("IMURUN_NO_PATH", "1")
    install.ensure_command()
    shim = install.command_path()
    assert os.access(shim, os.X_OK)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run([str(shim), "--version"], capture_output=True, text=True, env=env,
                         cwd=str(home[0]), timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == f"imurun {install.app_version()}"


@pytest.mark.skipif(not POSIX, reason="shell rc files")
def test_path_added_once_to_shell_rc(home):
    h, bin_dir = home
    first = install.ensure_command()
    assert first["ok"] and first["path_updated"]
    profile = (h / ".profile").read_text(encoding="utf-8")
    assert install.RC_BEGIN in profile and str(bin_dir) in profile
    assert (h / ".bashrc").exists()
    second = install.ensure_command()
    assert second["path_updated"] == []
    assert (h / ".profile").read_text(encoding="utf-8").count(install.RC_BEGIN) == 1
    # The line itself works in a POSIX shell and is idempotent.
    out = subprocess.run(["sh", "-c", f". {h / '.profile'}; . {h / '.profile'}; echo \"$PATH\""],
                         capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(h)})
    assert out.stdout.split(":").count(str(bin_dir)) == 1


def test_foreign_file_is_left_alone(home, monkeypatch):
    monkeypatch.setenv("IMURUN_NO_PATH", "1")
    shim = install.command_path()
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\necho someone else\n", encoding="utf-8")
    info = install.ensure_command()
    assert not info["ok"] and "another program" in info["detail"]
    assert shim.read_text(encoding="utf-8") == "#!/bin/sh\necho someone else\n"


def test_existing_command_for_this_python_is_kept(home, monkeypatch, tmp_path):
    script = tmp_path / "venvbin" / "imurun"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\nfrom imurun.cli import main\n", encoding="utf-8")
    monkeypatch.setattr(install.shutil, "which", lambda name, *a, **k: str(script))
    info = install.ensure_command()
    assert info == {"ok": True, "path": str(script), "detail": "on PATH"}
    assert not install.command_path().exists()


def test_stale_launcher_is_repointed(home, monkeypatch):
    monkeypatch.setenv("IMURUN_NO_PATH", "1")
    shim = install.command_path()
    shim.parent.mkdir(parents=True)
    shim.write_text(f"#!/bin/sh\n# {install.SHIM_MARK}\nexec \"/old/venv/python\" -m imurun \"$@\"\n",
                    encoding="utf-8")
    monkeypatch.setattr(install.shutil, "which", lambda name, *a, **k: str(shim))
    info = install.ensure_command()
    assert info["ok"]
    assert install._py_token() in shim.read_text(encoding="utf-8")


def test_command_inside_activated_venv_still_gets_launcher(home, monkeypatch, tmp_path):
    """`source venv/bin/activate; pip install imurun` - a new terminal has no venv on PATH."""
    monkeypatch.setenv("IMURUN_NO_PATH", "1")
    prefix = tmp_path / "venv"
    script = prefix / "bin" / "imurun"
    script.parent.mkdir(parents=True)
    script.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    monkeypatch.setattr(install.sys, "prefix", str(prefix))
    monkeypatch.setattr(install.sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setattr(install.shutil, "which", lambda name, *a, **k: str(script))
    info = install.ensure_command()
    assert info["ok"] and install.command_path().is_file()


@pytest.mark.skipif(not POSIX, reason="shell rc files")
def test_macos_style_bash_profile_and_zsh_get_the_line(home, monkeypatch):
    h, bin_dir = home
    (h / ".bash_profile").write_text("# user's own\n", encoding="utf-8")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    info = install.ensure_command()
    assert info["ok"]
    for name in (".bash_profile", ".zshrc", ".profile"):
        text = (h / name).read_text(encoding="utf-8")
        assert install.RC_BEGIN in text and str(bin_dir) in text, name
    assert (h / ".bash_profile").read_text(encoding="utf-8").startswith("# user's own\n")


@pytest.mark.skipif(os.name != "nt" or not os.environ.get("CI"),
                    reason="writes the real HKCU Path; Windows CI runners only")
def test_windows_user_path_registry(tmp_path):
    import winreg

    folder = tmp_path / "imurun cmd"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
        try:
            original = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            original = None
    try:
        assert install._add_to_user_path(folder) == [r"HKCU\Environment\Path"]
        assert install._add_to_user_path(folder) == []  # idempotent
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, "Path")
        assert install._on_path(folder, value)
    finally:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_WRITE) as key:
            if original is None:
                winreg.DeleteValue(key, "Path")
            else:
                winreg.SetValueEx(key, "Path", 0, original[1], original[0])


def test_https_uses_the_os_trust_store(monkeypatch):
    """Unknown-CA failures (corporate proxy, macOS python.org build): verify against the OS store."""
    import ssl

    truststore = pytest.importorskip("truststore")
    monkeypatch.delenv("IMURUN_NO_TRUSTSTORE", raising=False)
    try:
        assert install.use_system_certificates() == "system trust store"
        ctx = ssl.create_default_context()
        assert type(ctx).__module__.startswith("truststore")
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    finally:
        truststore.extract_from_ssl()


def test_trust_store_opt_out(monkeypatch):
    monkeypatch.setenv("IMURUN_NO_TRUSTSTORE", "1")
    assert "IMURUN_NO_TRUSTSTORE" in install.use_system_certificates()


@pytest.mark.skipif(not POSIX, reason="POSIX launcher")
def test_launcher_ignores_modules_in_the_current_directory(home, monkeypatch, tmp_path):
    """`imurun` typed inside an untrusted folder must not import that folder's code."""
    monkeypatch.setenv("IMURUN_NO_PATH", "1")
    install.ensure_command()
    evil = tmp_path / "evil"
    (evil / "imurun").mkdir(parents=True)
    (evil / "imurun" / "__init__.py").write_text("raise SystemExit('PWNED')\n", encoding="utf-8")
    (evil / "imurun" / "__main__.py").write_text("raise SystemExit('PWNED')\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run([str(install.command_path()), "--version"], capture_output=True, text=True,
                         env=env, cwd=str(evil), timeout=60)
    assert "PWNED" not in out.stdout + out.stderr
    assert out.stdout.strip() == f"imurun {install.app_version()}"


@pytest.mark.skipif(not POSIX, reason="POSIX permissions")
def test_world_writable_bin_dir_is_refused(home, monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    monkeypatch.setenv("IMURUN_BIN_DIR", str(shared))
    info = install.ensure_command()
    assert not info["ok"] and "every user" in info["detail"]
    assert not (shared / "imurun").exists()


@pytest.mark.skipif(not POSIX, reason="POSIX symlinks")
def test_planted_symlink_is_not_followed(home, monkeypatch, tmp_path):
    monkeypatch.setenv("IMURUN_NO_PATH", "1")
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    shim = install.command_path()
    shim.parent.mkdir(parents=True)
    shim.symlink_to(victim)
    install.ensure_command()
    assert victim.read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.skipif(not POSIX, reason="sh quoting")
def test_odd_characters_in_paths_are_quoted(home, monkeypatch, tmp_path):
    h, _ = home
    odd = tmp_path / 'bin $(touch PWNED) "q" `x`'
    monkeypatch.setenv("IMURUN_BIN_DIR", str(odd))
    info = install.ensure_command()
    assert info["ok"], info
    out = subprocess.run(["sh", "-c", f". {h / '.profile'}; echo \"$PATH\""], capture_output=True,
                         text=True, cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin", "HOME": str(h)})
    assert out.returncode == 0, out.stderr
    assert str(odd) in out.stdout.split(":")
    assert not (tmp_path / "PWNED").exists()
