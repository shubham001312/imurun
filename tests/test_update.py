"""services/update: versions, the ask-again policy, check() with mocked HTTP,
apply() command selection per layout, /api/update round trip, `imurun update`."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from services.update import manager as um
from services.update import routes as update_routes
from services.update.cli import main as update_cli


# --------------------------------------------------------------------------- helpers
def fake_fetch(pypi="0.4.0", github="v0.4.0", fail=()):
    calls = []

    def fetch(url, timeout):
        calls.append((url, timeout))
        if ("pypi" in url and "pypi" in fail) or ("github" in url and "github" in fail):
            raise OSError("offline")
        if "pypi" in url:
            return {"info": {"version": pypi}}
        return {"tag_name": github, "html_url": f"https://github.com/x/releases/tag/{github}",
                "draft": False, "prerelease": False}

    fetch.calls = calls
    return fetch


def make(tmp_path, *, current="0.3.0", source="pip", fetch=None, runner=None, clock=None,
         restart=None, root=None):
    return um.UpdateManager(
        data_root=tmp_path, current=current, source=source, root=root or tmp_path,
        fetch=fetch or fake_fetch(), runner=runner, clock=clock or time.time,
        python="py", engine_paths=[], restart=restart or (lambda: None),
    )


@pytest.fixture()
def prompts_on(monkeypatch):
    """enabled() is False under pytest (no network); the policy tests need it on."""
    monkeypatch.setattr(um, "enabled", lambda: True)


# --------------------------------------------------------------------------- versions
@pytest.mark.parametrize("newer,older", [
    ("0.4.0", "0.3.0"), ("v0.3.1", "0.3.0"), ("0.10.0", "0.9.9"), ("0.4.0", "0.4.0rc1"),
    ("0.4.0rc2", "0.4.0rc1"), ("1.0", "0.99.99"),
])
def test_version_order(newer, older):
    assert um.is_newer(newer, older)
    assert not um.is_newer(older, newer)


def test_version_equal_and_garbage():
    assert not um.is_newer("0.3.0", "0.3")
    assert not um.is_newer(None, "0.3.0")
    assert not um.is_newer("nonsense", "0.3.0")
    assert um.is_newer("0.3.0", "0+unknown") is False or um.parse_version("0+unknown") is not None


# --------------------------------------------------------------------------- policy
def test_should_prompt_rules():
    base = dict(current="0.3.0", latest="0.4.0", launches_since_dismiss=0)
    assert um.should_prompt(dismissed=None, **base)
    assert not um.should_prompt(dismissed=None, snoozed=True, **base)
    assert not um.should_prompt(dismissed="0.4.0", **base)
    assert not um.should_prompt(dismissed="0.4.0", **{**base, "launches_since_dismiss": 2})
    assert um.should_prompt(dismissed="0.4.0", **{**base, "launches_since_dismiss": 3})
    assert um.should_prompt(dismissed="0.3.5", **base)  # newer than the dismissed one
    assert not um.should_prompt(dismissed=None, **{**base, "latest": "0.3.0"})


def test_dismiss_then_three_launches_brings_prompt_back(tmp_path, prompts_on):
    m = make(tmp_path, fetch=fake_fetch("0.4.0", "v0.4.0"))
    m.record_launch()
    m.check()
    assert m.view()["prompt"] is True
    m.dismiss()
    assert m.view()["prompt"] is False and m.view()["dismissedVersion"] == "0.4.0"
    for n in (1, 2):
        m = make(tmp_path)          # a new launch reads the persisted state
        m.record_launch()
        assert m.view()["launchesSinceDismiss"] == n and m.view()["prompt"] is False
    m = make(tmp_path)
    m.record_launch()
    assert m.view()["launchesSinceDismiss"] == 3 and m.view()["prompt"] is True
    # Dismissing again restarts the count.
    m.dismiss()
    assert m.view()["launchesSinceDismiss"] == 0 and m.view()["prompt"] is False


def test_newer_release_resets_dismissal(tmp_path, prompts_on):
    m = make(tmp_path, fetch=fake_fetch("0.4.0", "v0.4.0"))
    m.check()
    m.dismiss()
    m = make(tmp_path, fetch=fake_fetch("0.5.0", "v0.4.0"))
    m.record_launch()
    m.check(force=True)
    v = m.view()
    assert v["latest"] == "0.5.0" and v["prompt"] is True and v["launchesSinceDismiss"] == 1


def test_later_snoozes_this_launch_only(tmp_path, prompts_on):
    m = make(tmp_path)
    m.check()
    m.later()
    assert m.view()["prompt"] is False
    m2 = make(tmp_path)
    m2.record_launch()
    assert m2.view()["prompt"] is True


def test_updated_past_dismissed_version_clears_it(tmp_path):
    m = make(tmp_path)
    m.check()
    m.dismiss()
    m = make(tmp_path, current="0.4.0")
    m.record_launch()
    assert m.view()["dismissedVersion"] is None and m.view()["available"] is False


def test_prompt_off_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("IMURUN_UPDATE_CHECK", "0")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert um.enabled() is False
    m = make(tmp_path)
    m.check()
    assert m.view()["available"] is True and m.view()["prompt"] is False
    assert m.view()["enabled"] is False


# --------------------------------------------------------------------------- check()
def test_check_takes_newest_of_pypi_and_github_and_caches(tmp_path):
    fetch = fake_fetch("0.4.0", "v0.4.1")
    now = [1_000_000.0]
    m = make(tmp_path, fetch=fetch, clock=lambda: now[0])
    v = m.check()
    assert v["latest"] == "0.4.1" and v["available"] and v["state"] == "idle"
    assert v["releaseNotesUrl"].endswith("v0.4.1")
    assert {u for u, _ in fetch.calls} == {um.PYPI_URL, um.GITHUB_URL}
    assert all(t == 3.0 for _, t in fetch.calls)
    saved = json.loads((tmp_path / "update-state.json").read_text())
    assert saved["latest"] == "0.4.1"
    # Within 6 h: no network.
    now[0] += 5 * 3600
    make(tmp_path, fetch=fetch, clock=lambda: now[0]).check()
    assert len(fetch.calls) == 2
    # After 6 h it asks again; force always does.
    now[0] += 2 * 3600
    make(tmp_path, fetch=fetch, clock=lambda: now[0]).check()
    assert len(fetch.calls) == 4
    make(tmp_path, fetch=fetch, clock=lambda: now[0]).check(force=True)
    assert len(fetch.calls) == 6


def test_check_survives_one_source_down(tmp_path):
    m = make(tmp_path, fetch=fake_fetch("0.4.2", fail=("github",)))
    v = m.check()
    assert v["latest"] == "0.4.2" and v["state"] == "idle"
    assert v["releaseNotesUrl"].endswith("/tag/v0.4.2")


def test_check_offline_is_an_error_state_not_an_exception(tmp_path):
    events = []
    m = make(tmp_path, fetch=fake_fetch(fail=("pypi", "github")))
    m.publish = lambda name, data: events.append((name, data["state"]))
    v = m.check()
    assert v["state"] == "error" and "offline" in v["error"] and v["latest"] is None
    assert v["available"] is False and v["prompt"] is False
    assert events == [("update", "checking"), ("update", "error")]


def test_github_prerelease_ignored(tmp_path):
    def fetch(url, timeout):
        if "pypi" in url:
            return {"info": {"version": "0.3.0"}}
        return {"tag_name": "v0.9.0", "prerelease": True}

    assert make(tmp_path, fetch=fetch).check()["latest"] == "0.3.0"


def test_pre_release_offered_only_to_pre_release_users(tmp_path):
    fetch = fake_fetch(pypi="0.4.0rc1", github="v0.3.1")
    assert make(tmp_path / "a", fetch=fetch).check()["latest"] == "0.3.1"
    assert make(tmp_path / "b", current="0.4.0b1", fetch=fetch).check()["latest"] == "0.4.0rc1"


def test_newer_of_pypi_and_github_by_number_not_text(tmp_path):
    v = make(tmp_path, current="0.3.9", fetch=fake_fetch(pypi="0.3.10", github="v0.3.9")).check()
    assert v["latest"] == "0.3.10" and v["available"] is True


def test_detect_source_uv_tool():
    assert um.detect_source(installed=True, prefix="/home/u/.local/share/uv/tools/imurun",
                            env={}) == "uv"


def test_cached_notice_for_version(tmp_path):
    make(tmp_path, fetch=fake_fetch("0.4.0")).check()
    assert "update available: 0.4.0" in um.cached_notice(tmp_path, current="0.3.0")
    assert um.cached_notice(tmp_path, current="0.4.0") is None
    assert um.cached_notice(tmp_path / "nowhere", current="0.3.0") is None


# --------------------------------------------------------------------------- layout + apply()
def test_detect_source(tmp_path):
    assert um.detect_source(installed=False) == "checkout"
    assert um.detect_source(installed=True, prefix=str(tmp_path / "venv"), env={},
                            data_root=tmp_path) == "npm"
    assert um.detect_source(installed=True, prefix="/x", env={"IMURUN_LAUNCHER": "npm"}) == "npm"
    assert um.detect_source(installed=True, prefix="/home/u/.local/pipx/venvs/imurun", env={}) == "pipx"
    assert um.detect_source(installed=True, prefix=r"C:\Users\u\pipx\venvs\imurun", env={}) == "pipx"
    assert um.detect_source(installed=True, prefix="/opt/venv", env={}, data_root=tmp_path) == "pip"


def test_apply_plan_per_layout(tmp_path):
    root = tmp_path
    assert um.apply_plan("checkout", root=root, python="py") == [
        ["git", "-C", str(root), "pull", "--ff-only"], ["py", str(root / "install.py"), "--auto"]]
    pip = ["py", "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "imurun"]
    assert um.apply_plan("pip", root=root, python="py") == [pip]
    assert um.apply_plan("npm", root=root, python="py") == [pip]
    assert um.apply_plan("pipx", root=root, python="py") == [["pipx", "upgrade", "imurun"]]
    assert um.apply_plan("uv", root=root, python="py") == [["uv", "tool", "upgrade", "imurun"]]
    assert um.apply_plan("pip", root=root, python="py", version="0.3.10") == [
        pip[:-1] + ["imurun==0.3.10"]]
    assert um.apply_plan("npm", root=root, python="py", version="garbage") == [pip]
    assert um.describe_plan(um.apply_plan("checkout", root=root, python=sys.executable)) == \
        "git pull --ff-only, then python install.py --auto"


def _ok(cmd):
    return subprocess.CompletedProcess(cmd, 0, "done", "")


def test_apply_runs_plan_then_requests_restart(tmp_path):
    ran, restarts, events = [], [], []
    m = make(tmp_path, runner=lambda cmd: ran.append(cmd) or _ok(cmd), restart=lambda: restarts.append(1))
    m.publish = lambda name, data: events.append(data["state"])
    m.check()
    m.dismiss()
    v = m.apply()
    # Pinned to the checked version, so a release missing from PyPI fails loudly.
    assert ran == [["py", "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
                    "imurun==0.4.0"]]
    assert v["state"] == "restart_pending" and restarts == [1]
    assert v["dismissedVersion"] is None
    assert events[-2:] == ["updating", "restart_pending"]


def test_apply_failure_keeps_old_version_running(tmp_path):
    def runner(cmd):
        return subprocess.CompletedProcess(cmd, 1, "", "ERROR: No matching distribution")

    restarts = []
    m = make(tmp_path, runner=runner, restart=lambda: restarts.append(1))
    m.check()
    v = m.apply()
    assert v["state"] == "error" and "No matching distribution" in v["error"] and not restarts


def test_checkout_dirty_tree_refuses(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    ran = []
    monkeypatch.setattr(um, "_git", lambda root, *a, **k: subprocess.CompletedProcess(a, 0, " M start.py\n", ""))
    m = make(tmp_path, source="checkout", runner=lambda c: ran.append(c) or _ok(c))
    m.check()
    v = m.view()
    assert v["canApply"] is False and "local changes" in v["applyBlocked"]
    v = m.apply()
    assert v["state"] == "error" and "local changes" in v["error"] and ran == []


def test_checkout_clean_tree_pulls_then_installs(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(um, "_git", lambda root, *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    ran = []
    m = make(tmp_path, source="checkout", runner=lambda c: ran.append(c) or _ok(c))
    m.check()
    assert m.view()["canApply"] is True
    assert m.apply()["state"] == "restart_pending"
    assert ran[0][-2:] == ["pull", "--ff-only"] and ran[1][-1] == "--auto"


def test_checkout_without_git_folder_is_blocked(tmp_path):
    m = make(tmp_path, source="checkout")
    m.check()
    assert "not a git checkout" in m.view()["applyBlocked"]


def test_release_engine_lock_swaps_in_a_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(um.os, "name", "nt")
    lib = tmp_path / "fish.dll"
    lib.write_bytes(b"engine")
    assert um.release_engine_lock([lib]) == [str(lib)]
    assert lib.read_bytes() == b"engine"
    olds = list(tmp_path.glob("fish.dll.old-*"))
    assert len(olds) == 1
    assert um.remove_stale_engine_copies([tmp_path]) == 1 and not list(tmp_path.glob("*.old-*"))


def test_request_restart_sets_event():
    um.RESTART.clear()
    um.request_restart(0)
    assert um.RESTART.is_set()
    um.RESTART.clear()


# --------------------------------------------------------------------------- /api/update
@pytest.fixture()
def api(tmp_path):
    from services.persona.store import PersonaStore
    from services.server.api import ApiServer
    from tests.test_agentic_api import FakeModel

    server = ApiServer(persona_store=PersonaStore(tmp_path / "persona.json"),
                       sessions_root=tmp_path / "sessions", data_root=tmp_path,
                       model=FakeModel(), static_root=None)
    ran = []
    mgr = make(tmp_path, runner=lambda c: ran.append(c) or _ok(c))
    mgr.publish = server._events.publish
    server._update = mgr
    server.start()
    yield server, mgr, ran
    server.stop()


def _wait_state(server, req, want, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _, body = req(server, "GET", "/api/update")
        if body["state"] in want:
            return body
        time.sleep(0.02)
    raise AssertionError(f"state never reached {want}: {body}")


def test_route_round_trip(api, prompts_on):
    from tests.test_agentic_api import req

    server, mgr, ran = api
    assert req(server, "GET", "/api/update", token=False)[0] == 401
    status, body = req(server, "GET", "/api/update")
    assert status == 200
    assert set(body) >= {"current", "latest", "available", "source", "releaseNotesUrl", "prompt",
                         "state", "error", "checkedAt", "enabled", "canApply", "applyBlocked",
                         "command", "dismissedVersion", "launchesSinceDismiss"}
    assert body["latest"] is None and body["state"] == "idle" and body["available"] is False

    assert req(server, "POST", "/api/update", {"action": "nope"})[0] == 400
    assert req(server, "POST", "/api/update", {})[0] == 400
    status, body = req(server, "POST", "/api/update", {"action": "apply"})
    assert status == 409 and "no update" in body["error"]

    status, body = req(server, "POST", "/api/update", {"action": "check"})
    assert status == 202 and body["ok"] is True
    view = _wait_state(server, req, ("idle",))
    assert view["latest"] == "0.4.0" and view["available"] and view["prompt"] is True

    status, body = req(server, "POST", "/api/update", {"action": "later"})
    assert status == 200 and body["update"]["prompt"] is False
    status, body = req(server, "POST", "/api/update", {"action": "dismiss"})
    assert status == 200 and body["update"]["dismissedVersion"] == "0.4.0"

    restarts = []
    mgr.restart = lambda: restarts.append(1)
    status, body = req(server, "POST", "/api/update", {"action": "apply"})
    assert status == 202 and body["update"]["state"] in ("updating", "restart_pending")
    view = _wait_state(server, req, ("restart_pending",))
    assert ran and restarts == [1] and view["prompt"] is False


def test_update_event_published(api):
    server, mgr, _ = api
    sub = server._events.subscribe() if hasattr(server._events, "subscribe") else None
    if sub is None:
        pytest.skip("event broker has no subscribe()")
    mgr.check()
    names = []
    end = time.monotonic() + 2
    while time.monotonic() < end and len(names) < 2:
        ev = sub.get(timeout=0.5) if hasattr(sub, "get") else None
        if ev is None:
            break
        names.append((getattr(ev, "type", None) or getattr(ev, "event_type", None), ev.data["state"]))
    assert ("update", "checking") in names and ("update", "idle") in names


def test_start_checker_counts_launch_without_network(tmp_path):
    class S:  # minimal server stand-in
        _home = tmp_path
        _events = None

    s = S()
    mgr = update_routes.start_checker(s)
    assert mgr is s._update and mgr._persist["launches"] == 1  # enabled() is False under pytest


# --------------------------------------------------------------------------- imurun update
def test_cli_check_only(tmp_path, capsys):
    m = make(tmp_path)
    assert update_cli(["--check"], manager=m) == 0
    out = capsys.readouterr().out
    assert "Update available: 0.3.0 -> 0.4.0" in out and "imurun update" in out


def test_cli_no_answer_dismisses(tmp_path):
    ran = []
    m = make(tmp_path, runner=lambda c: ran.append(c) or _ok(c))
    assert update_cli([], manager=m, ask=lambda _: "n") == 0
    assert ran == [] and m.view()["dismissedVersion"] == "0.4.0"


def test_cli_yes_applies_without_restart(tmp_path, capsys):
    ran, restarts = [], []
    m = make(tmp_path, runner=lambda c: ran.append(c) or _ok(c), restart=lambda: restarts.append(1))
    assert update_cli(["--yes"], manager=m) == 0
    assert ran and not restarts and "Updated to 0.4.0" in capsys.readouterr().out


def test_cli_up_to_date(tmp_path, capsys):
    assert update_cli([], manager=make(tmp_path, current="0.4.0")) == 0
    assert "Up to date" in capsys.readouterr().out


# ------------------------------------------------------------ the `imurun` launcher
def test_imurun_launcher_restarts_on_exit_75(tmp_path, monkeypatch):
    """install._shim_text under the real shell: args pass through, 75 runs it again.

    A fake `imurun` package (found through the launcher's PYTHONPATH, as for a
    checkout) exits 75 on its first run and 3 on the second.
    """
    import install

    pkg = tmp_path / "root" / "imurun"
    pkg.mkdir(parents=True)
    counter = tmp_path / "count"
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(
        "import os, sys, pathlib\n"
        f"p = pathlib.Path({str(counter)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\n"
        "print('ARGS', sys.argv[1:], os.environ.get('IMURUN_SUPERVISED'),"
        " os.environ.get('IMURUN_AFTER_UPDATE'))\n"
        "sys.exit(75 if n == 0 else 3)\n")
    monkeypatch.setattr(install, "ROOT", tmp_path / "root")
    monkeypatch.setattr(install, "INSTALLED", False)
    shim = tmp_path / ("imurun.cmd" if install.IS_WINDOWS else "imurun")
    shim.write_bytes(install._shim_text().encode("utf-8"))
    cmd = ["cmd", "/c", str(shim)] if install.IS_WINDOWS else ["sh", str(shim)]
    env = {k: v for k, v in os.environ.items()
           if k not in ("IMURUN_SUPERVISED", "IMURUN_AFTER_UPDATE", "PYTHONPATH")}
    res = subprocess.run([*cmd, "--version", "x"], capture_output=True, text=True,
                         cwd=str(tmp_path), env=env, timeout=60)
    assert res.returncode == 3, res.stdout + res.stderr
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("ARGS")]
    assert lines == ["ARGS ['--version', 'x'] 1 None", "ARGS ['--version', 'x'] 1 1"]
