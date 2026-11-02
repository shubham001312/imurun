"""The compact prompt profile for small local models (services/response/profile.py)."""
from __future__ import annotations

import pytest

from services.response import profile
from services.response.loop import AgentLoop

LLAMA_1B = {"layers": 16, "embd": 2048, "ff": 8192, "vocab": 128256, "experts": 0}
PHI3_MINI = {"layers": 32, "embd": 3072, "ff": 8192, "vocab": 32064, "experts": 0}


class _Model:
    def __init__(self, info=None, remote=False, raises=False):
        self._info, self.remote, self._raises = info or {}, remote, raises
        self.seen: list[list[dict]] = []

    def model_info(self):
        if self._raises:
            raise RuntimeError("no engine")
        return self._info

    def generate(self, messages):
        self.seen.append(messages)
        return "Paris."


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.delenv("IMURUN_COMPACT_PROMPT", raising=False)


def test_estimate_separates_1b_from_phi3():
    assert 1.0e9 < profile.estimate_params(LLAMA_1B) < profile.SMALL_MODEL_PARAMS
    assert profile.estimate_params(PHI3_MINI) > profile.SMALL_MODEL_PARAMS
    assert profile.estimate_params({}) == 0


def test_is_compact_by_size_remote_and_errors():
    assert profile.is_compact(_Model(LLAMA_1B))
    assert not profile.is_compact(_Model(PHI3_MINI))
    assert not profile.is_compact(_Model(LLAMA_1B, remote=True))      # a hosted model is full-size
    assert not profile.is_compact(_Model(raises=True))                 # unknown keeps tools
    assert not profile.is_compact(_Model({"error": "not loaded"}))
    assert not profile.is_compact(None)


@pytest.mark.parametrize("value,expected", [("1", True), ("0", False)])
def test_env_override(monkeypatch, value, expected):
    monkeypatch.setenv("IMURUN_COMPACT_PROMPT", value)
    assert profile.is_compact(_Model(PHI3_MINI if expected else LLAMA_1B)) is expected


def test_compact_context_prefers_narrow_tools_and_trims():
    results = [
        {"tool": "web_search", "ok": True, "content": "Web research ... encyclopedia climate excerpt"},
        {"tool": "weather", "ok": True, "content": "Kolkata: clear sky, 26.8 C. " + "x" * 5000},
    ]
    text = profile.compact_context(results)
    assert text.startswith("Live information")
    assert "Kolkata" in text and "encyclopedia" not in text
    assert len(text) < profile.COMPACT_CONTEXT_CHARS + 100


def test_compact_context_failure_and_empty():
    assert profile.compact_context([]) == ""
    assert "could not be fetched" in profile.compact_context([{"tool": "weather", "ok": False, "content": ""}])


def test_small_model_gets_one_line_prompt_and_no_tool_catalogue():
    model = _Model(LLAMA_1B)
    result = AgentLoop(model, session_id="s").run("What is the capital of France?")
    system = model.seen[0][0]["content"]
    assert system.startswith(profile.COMPACT_DIRECTIVE)
    assert "# Tools" not in system and "em dash" not in system and ".gguf" not in system
    assert result.answer == "Paris."
    assert len(model.seen) == 1


def test_large_model_keeps_full_prompt():
    model = _Model(PHI3_MINI)
    AgentLoop(model, session_id="s").run("What is the capital of France?")
    assert profile.COMPACT_DIRECTIVE not in model.seen[0][0]["content"]
