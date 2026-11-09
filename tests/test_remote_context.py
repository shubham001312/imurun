"""Stats showed 32,768 tokens for every NVIDIA model: the provider's model list
has no window, and a known window was capped at 32,768 anyway."""
from __future__ import annotations

import io
import json
import urllib.error

from model.remote_client import CONTEXT_TOKENS, OpenAICompatClient


def client():
    c = OpenAICompatClient(lambda: "nvapi-test", base_url="https://integrate.api.nvidia.com/v1",
                           hosts=("integrate.api.nvidia.com",), provider="nvidia", label="NVIDIA")
    c.set_model("nvidia/some-model")
    return c


def test_unknown_window_is_reported_as_unknown():
    c = client()
    info = c.model_info()
    assert info["context"] == CONTEXT_TOKENS and info["context_known"] is False


def test_the_real_window_is_reported_and_the_budget_stays_bounded():
    c = client()
    c.set_model("nvidia/some-model", context=131072)
    info = c.model_info()
    assert info["context_window"] == 131072 and info["context_known"] is True
    assert c.context_limit() == CONTEXT_TOKENS  # prompts stay within the working budget


def test_the_window_is_read_from_the_model_details(monkeypatch):
    c = client()
    saved = {}
    c._on_context = lambda mid, value: saved.update({mid: value})

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(c, "_request", lambda method, path, *a, **k: Resp(json.dumps(
        {"id": "nvidia/some-model", "max_model_len": 262144}).encode()))
    assert c.fetch_context() == 262144
    assert c.model_info()["context_window"] == 262144 and saved == {"nvidia/some-model": 262144}


def test_the_window_is_learned_from_a_context_length_error():
    c = client()
    body = json.dumps({"error": {"message": "This model's maximum context length is 131,072 tokens."}}).encode()
    err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(body))
    c._http_error(err, "/chat/completions", "nvapi-test")
    assert c.model_info()["context_window"] == 131072 and c.model_info()["context_known"] is True
