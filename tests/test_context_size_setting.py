"""Settings > Engine context size reaches the engine.

Before: ``server.contextSize`` was saved but never read; the engine only saw
FISH_CONTEXT_SIZE (default 0 = automatic), so changing the setting did nothing.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services import config as cfg
from services.server.api import _apply_context_size


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("IMURUN_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("FISH_CONTEXT_SIZE", raising=False)
    (tmp_path / "settings").mkdir()
    return tmp_path


def save(root, value):
    (root / "settings" / "server.json").write_text(json.dumps({"contextSize": value}))


def test_saved_size_is_used(root):
    save(root, 16384)
    assert cfg.load_config().fish.context_size == 16384


def test_nothing_saved_means_automatic(root):
    assert cfg.load_config().fish.context_size == 0


def test_environment_still_wins(root, monkeypatch):
    save(root, 16384)
    monkeypatch.setenv("FISH_CONTEXT_SIZE", "4096")
    assert cfg.load_config().fish.context_size == 4096


@pytest.mark.parametrize("bad", [0, 12, -5, 10**9, "lots", None])
def test_out_of_range_or_junk_falls_back_to_automatic(root, bad):
    save(root, bad)
    assert cfg.load_config().fish.context_size == 0


def test_a_save_while_running_updates_the_next_load():
    fish = SimpleNamespace(config=SimpleNamespace(context_size=0))
    server = SimpleNamespace(_model=SimpleNamespace(_fish=fish))
    _apply_context_size(server, 32768)
    assert fish.config.context_size == 32768
    _apply_context_size(server, 5)  # out of range: ignored
    assert fish.config.context_size == 32768
