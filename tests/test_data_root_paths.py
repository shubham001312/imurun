"""Stores that default to the data root honour IMURUN_DATA_ROOT.

The tool workspace and the notes / facts stores hard-coded ``~/.imurun``: a
server started with ``IMURUN_DATA_ROOT=<scratch>`` reported
``workspace: ~/.imurun/workspace`` on ``GET /api/permissions`` and wrote notes
to the real home folder.
"""
from __future__ import annotations

from services.tools.notes_tool import FactsTool, NotesTool
from services.tools.workspace import default_workspace


def test_defaults_live_under_the_data_root(monkeypatch, tmp_path):
    monkeypatch.setenv("IMURUN_DATA_ROOT", str(tmp_path / "data"))
    root = tmp_path / "data"
    assert default_workspace() == root / "workspace"
    assert NotesTool().storage_dir == root / "notes"
    assert FactsTool().storage_dir == root / "facts"
