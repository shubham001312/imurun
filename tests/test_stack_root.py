"""``locate_root`` without any docker-compose.yml reports a directory.

It returned ``Path(__file__)`` itself, so ``GET /api/stack/status`` showed
``root: .../services/server/stack.py`` and
``compose_file: .../stack.py/docker-compose.yml``.
"""
from __future__ import annotations

from services.server.stack import locate_root


def test_root_without_compose_file_is_a_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("IMURUN_ROOT", raising=False)
    start = tmp_path / "pkg" / "stack.py"
    start.parent.mkdir()
    start.write_text("")
    root = locate_root(start)
    assert root == start.parent
    assert root.is_dir()


def test_root_finds_the_compose_file(tmp_path, monkeypatch):
    monkeypatch.delenv("IMURUN_ROOT", raising=False)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    start = tmp_path / "a" / "b" / "stack.py"
    start.parent.mkdir(parents=True)
    start.write_text("")
    assert locate_root(start) == tmp_path.resolve()
