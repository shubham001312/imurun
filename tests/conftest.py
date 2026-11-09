"""Shared pytest setup: make the repository importable as top-level packages,
and keep tests that need the real internet off by default."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests (and the start.py subprocesses some of them launch) must never set up the
# `imurun` command in the developer's real ~/.local/bin or shell profile.
os.environ["IMURUN_NO_PATH"] = "1"
if not os.environ.get("IMURUN_BIN_DIR"):
    import tempfile

    os.environ["IMURUN_BIN_DIR"] = tempfile.mkdtemp(prefix="imurun-test-bin-")
# The agent issue log (services/response/issues.py) never lands in the real data root.
if not os.environ.get("IMURUN_ISSUES_LOG"):
    import tempfile

    os.environ["IMURUN_ISSUES_LOG"] = os.path.join(tempfile.mkdtemp(prefix="imurun-test-issues-"),
                                                   "agent-issues.log")


def pytest_configure(config):
    # Registered here (not in pyproject.toml, which is the setup owner's) so
    # --strict-markers accepts it.
    config.addinivalue_line("markers", "network: needs the real internet; skipped unless IMURUN_NETWORK_TESTS=1")
    # The agent loop would hand its model to the research planner for a
    # routing generation, which consumes a reply from the scripted fake models
    # the API/loop tests use. Withhold it on that path only; the research
    # tests drive the planner with their own models directly.
    os.environ.setdefault("IMURUN_LOOP_PLANNER_MODEL", "0")


def pytest_collection_modifyitems(config, items):
    if os.getenv("IMURUN_NETWORK_TESTS", "").strip() in ("1", "true", "yes"):
        return
    skip = pytest.mark.skip(reason="needs the internet; set IMURUN_NETWORK_TESTS=1 to run")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_builtin_connector_apps(tmp_path_factory, monkeypatch):
    """Tests never see a maintainer's real services/connectors/apps.local.json."""
    try:
        from services.connectors import apps
    except Exception:  # noqa: BLE001 - modules that do not import connectors
        return
    monkeypatch.setattr(apps, "LOCAL_FILE", tmp_path_factory.mktemp("apps") / "apps.local.json")
    for key in list(__import__("os").environ):
        if key.endswith(("_CLIENT_ID", "_CLIENT_SECRET")) and key.startswith("IMURUN_"):
            monkeypatch.delenv(key)
