"""Home > "Change folder" opens the computer's folder dialog (the server runs
on the same machine) and makes the chosen folder the workspace."""
from __future__ import annotations

from types import SimpleNamespace

from services.server.settings import SettingsStore
from services.tools import folder_picker, routes
from services.tools.permissions import PermissionPolicy


def server(tmp_path):
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws")})
    return SimpleNamespace(_policy=PermissionPolicy(settings), _settings=settings)


def test_the_chosen_folder_becomes_the_workspace(tmp_path, monkeypatch):
    project = tmp_path / "my-project"
    project.mkdir()
    monkeypatch.setattr(folder_picker, "pick_folder", lambda start=None, **k: str(project))
    srv = server(tmp_path)
    assert routes.is_post_route("/api/permissions/pick-folder")
    status, view = routes.handle_post(srv, "/api/permissions/pick-folder", {})
    assert status == 200
    assert srv._policy.workspace.root == project.resolve()


def test_closing_the_dialog_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(folder_picker, "pick_folder", lambda start=None, **k: "")
    srv = server(tmp_path)
    before = srv._policy.workspace.root
    assert routes.handle_post(srv, "/api/permissions/pick-folder", {}) == (200, {"cancelled": True})
    assert srv._policy.workspace.root == before


def test_no_dialog_on_a_headless_machine_is_a_clear_reply(tmp_path, monkeypatch):
    def boom(start=None, **k):
        raise folder_picker.PickerUnavailable("no desktop session to show a folder dialog in")
    monkeypatch.setattr(folder_picker, "pick_folder", boom)
    status, body = routes.handle_post(server(tmp_path), "/api/permissions/pick-folder", {})
    assert status == 409 and body["unavailable"] and "desktop session" in body["error"]


def test_a_whole_drive_is_still_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(folder_picker, "pick_folder", lambda start=None, **k: "/")
    status, body = routes.handle_post(server(tmp_path), "/api/permissions/pick-folder", {})
    assert status == 400
