"""A Windows Smart App Control block on fish.dll is explained, not shown raw."""
from __future__ import annotations

from model.fish_native import WINDOWS_APP_CONTROL_BLOCKED, describe_load_error


class _WinOSError(OSError):
    def __init__(self, winerror: int) -> None:
        super().__init__("An Application Control policy has blocked this file")
        self.winerror = winerror


def test_smart_app_control_block_is_explained():
    msg = describe_load_error("bin/fish.dll", _WinOSError(WINDOWS_APP_CONTROL_BLOCKED))
    assert "Smart App Control" in msg and "signed" in msg


def test_other_load_errors_keep_the_raw_text():
    msg = describe_load_error("bin/fish.dll", OSError("no such file"))
    assert msg == "cannot load bin/fish.dll: no such file"
