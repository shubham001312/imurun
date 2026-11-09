"""Approved folders besides the workspace (Desktop and Documents by default),
and credential folders that no tool may touch whatever is approved."""
from __future__ import annotations

import pytest

from services.server.settings import SettingsStore
from services.tools.permissions import DEFAULT_EXTRA_ROOTS, PermissionPolicy
from services.tools.workspace import WorkspaceError


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    for d in ("Desktop", "Documents", ".ssh", ".aws", "secret-project"):
        (h / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws")})
    return h, PermissionPolicy(settings)


def test_desktop_and_documents_are_approved_by_default(home):
    h, policy = home
    assert policy.values()["extraRoots"] == list(DEFAULT_EXTRA_ROOTS)
    assert policy.workspace.resolve("~/Desktop/demo/index.html", kind="file_write") == h / "Desktop/demo/index.html"
    assert policy.workspace.resolve(str(h / "Documents/notes.md")) == h / "Documents/notes.md"


def test_other_home_folders_stay_outside(home):
    h, policy = home
    with pytest.raises(WorkspaceError, match="outside the folders IMURUN may use"):
        policy.workspace.resolve(str(h / "secret-project/a.txt"))


@pytest.mark.parametrize("path", ["~/.ssh/id_ed25519", "~/.aws/credentials"])
def test_credential_folders_are_refused_even_inside_an_approved_folder(home, path):
    h, policy = home
    policy.update({"extraRoots": [str(h)]})  # the whole home folder approved
    assert policy.workspace.resolve("~/secret-project/a.txt") == h / "secret-project/a.txt"
    with pytest.raises(WorkspaceError, match="secrets file|protected"):
        policy.workspace.resolve(path)


def test_extra_roots_are_validated(home):
    _, policy = home
    for bad in (["relative/path"], ["/"], "not-a-list"):
        with pytest.raises(ValueError):
            policy.update({"extraRoots": bad})
    policy.update({"extraRoots": []})
    assert policy.values()["extraRoots"] == []
