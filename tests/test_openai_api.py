"""The OpenAI-compatible /v1 API on a real in-process ApiServer with fake models."""
from __future__ import annotations

import http.client
import json
import os
import stat

import pytest

from services.persona.store import PersonaStore
from services.server.api import ApiServer
from services.server.openai_api import StopFilter
from services.skills.store import SkillsStore


class _Config:
    max_tokens = 64


class FakeModel:
    """Streams scripted chunks; records what it was asked."""

    available = True
    config = _Config()

    def __init__(self, chunks=None):
        self.chunks = chunks or ["Hello", ",", " world", "!"]
        self.seen: list[tuple[list[dict], object]] = []
        self.closed_early = False

    def selected_name(self):
        return "Fake-1B-Q4_K_M.gguf"

    def generate(self, messages):
        self.seen.append((messages, None))
        return "".join(self.chunks)

    async def generate_stream(self, messages, params=None):
        self.seen.append((messages, params))
        try:
            for chunk in self.chunks:
                yield chunk
        except GeneratorExit:
            self.closed_early = True
            raise

    def stats(self):
        return {"prompt_tokens": 11, "generated_tokens": len(self.chunks)}

    def context_limit(self):
        return 4096


class UnavailableModel(FakeModel):
    available = False


def _server(tmp_path, model):
    return ApiServer(
        persona_store=PersonaStore(tmp_path / "persona.json"),
        sessions_root=tmp_path / "sessions",
        data_root=tmp_path,
        model=model,
        skills=SkillsStore(tmp_path / "skills"),
        static_root=None,
    ).start()


@pytest.fixture()
def api(tmp_path):
    model = FakeModel()
    server = _server(tmp_path, model)
    yield server, model, tmp_path
    server.stop()


def raw(server, method, path, payload=None, key=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    sent = dict(headers or {})
    if key:
        sent["Authorization"] = f"Bearer {key}"
    body = None
    if payload is not None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        sent["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=sent)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, resp.getheader("Content-Type") or "", data


def req(server, method, path, payload=None, key=None):
    status, _, data = raw(server, method, path, payload, key)
    return status, (json.loads(data) if data else {})


def api_key(server):
    status, view = req(server, "GET", "/api/openai", key=server.token)
    assert status == 200
    return view["apiKey"]


def sse(data: str) -> list:
    frames = [line[6:] for line in data.splitlines() if line.startswith("data: ")]
    assert frames and frames[-1] == "[DONE]", frames
    return [json.loads(frame) for frame in frames[:-1]]


def test_ui_view_shows_base_url_key_and_model(api):
    server, _, root = api
    status, view = req(server, "GET", "/api/openai", key=server.token)
    assert status == 200
    assert view["baseUrl"] == f"http://127.0.0.1:{server.port}/v1"
    assert view["apiKey"].startswith("sk-imurun-") and len(view["apiKey"]) > 40
    assert view["model"] == "Fake-1B-Q4_K_M"
    assert view["available"] is True
    # The UI route needs the shell token; the API key alone is not enough.
    assert req(server, "GET", "/api/openai")[0] == 401
    assert req(server, "GET", "/api/openai", key=view["apiKey"])[0] == 401
    key_file = root / "secrets" / "openai_api_key"
    assert key_file.read_text().strip() == view["apiKey"]
    if os.name == "posix":
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700


def test_models_list(api):
    server, _, _ = api
    status, body = req(server, "GET", "/v1/models", key=api_key(server))
    assert status == 200
    assert body["object"] == "list"
    assert body["data"] == [{"id": "Fake-1B-Q4_K_M", "object": "model",
                             "created": body["data"][0]["created"], "owned_by": "imurun"}]
    status, card = req(server, "GET", "/v1/models/Fake-1B-Q4_K_M", key=api_key(server))
    assert status == 200 and card["id"] == "Fake-1B-Q4_K_M"
    status, err = req(server, "GET", "/v1/models/nope", key=api_key(server))
    assert status == 404 and err["error"]["code"] == "model_not_found"


def test_auth_failures_use_openai_error_shape(api):
    server, _, _ = api
    for key in (None, "sk-imurun-wrong", "x" * 10):
        status, body = req(server, "GET", "/v1/models", key=key)
        assert status == 401
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["code"] == "invalid_api_key"
        assert set(body["error"]) >= {"message", "type", "code"}
    status, body = req(server, "POST", "/v1/chat/completions",
                       {"model": "x", "messages": [{"role": "user", "content": "hi"}]}, key="bad")
    assert status == 401 and body["error"]["code"] == "invalid_api_key"
    # The per-run shell token also works on /v1 (same trust as the UI).
    assert req(server, "GET", "/v1/models", key=server.token)[0] == 200


def test_scoped_client_token_needs_chat_scope(api):
    server, _, _ = api
    chat = server._clients.create("openclaw", ["chat"])["token"]
    readonly = server._clients.create("viewer", ["read-only"])["token"]
    assert req(server, "GET", "/v1/models", key=chat)[0] == 200
    assert req(server, "GET", "/v1/models", key=readonly)[0] == 401


def test_non_streaming_completion(api):
    server, model, _ = api
    status, body = req(server, "POST", "/v1/chat/completions", {
        "model": "whatever-the-tool-calls-it",
        "messages": [
            {"role": "developer", "content": "Be brief."},
            {"role": "user", "content": [{"type": "text", "text": "Say hi"}]},
        ],
        "max_tokens": 32, "temperature": 0.2, "top_p": 0.5,
    }, key=api_key(server))
    assert status == 200, body
    assert body["id"].startswith("chatcmpl-")
    assert body["object"] == "chat.completion"
    assert isinstance(body["created"], int)
    assert body["model"] == "Fake-1B-Q4_K_M"
    choice = body["choices"][0]
    assert choice["index"] == 0
    assert choice["message"] == {"role": "assistant", "content": "Hello, world!", "refusal": None}
    assert choice["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}
    messages, params = model.seen[-1]
    assert messages == [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Say hi"}]
    assert (params.max_tokens, params.temperature, params.top_p) == (32, 0.2, 0.5)


def test_stop_sequence_and_length(api):
    server, model, _ = api
    status, body = req(server, "POST", "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "x"}], "stop": [" wor"],
    }, key=api_key(server))
    assert status == 200
    assert body["choices"][0]["message"]["content"] == "Hello,"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert model.closed_early  # the generation was closed, not run to the end
    status, body = req(server, "POST", "/v1/chat/completions", {
        "messages": [{"role": "user", "content": "x"}], "max_tokens": 4,
    }, key=api_key(server))
    assert body["choices"][0]["finish_reason"] == "length"


def test_streaming_completion(api):
    server, _, _ = api
    status, ctype, data = raw(server, "POST", "/v1/chat/completions", {
        "model": "m", "stream": True, "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "hi"}],
    }, key=api_key(server))
    assert status == 200 and ctype.startswith("text/event-stream")
    chunks = sse(data)
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    assert len({c["id"] for c in chunks}) == 1
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "Hello, world!"
    finals = [c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
    assert len(finals) == 1 and finals[0]["choices"][0]["finish_reason"] == "stop"
    assert finals[0]["choices"][0]["delta"] == {}
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["total_tokens"] == 15


def test_streaming_with_stop(api):
    server, _, _ = api
    _, _, data = raw(server, "POST", "/v1/chat/completions", {
        "stream": True, "stop": "!", "messages": [{"role": "user", "content": "hi"}],
    }, key=api_key(server))
    chunks = sse(data)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "Hello, world"


@pytest.mark.parametrize("body,param", [
    ({}, "messages"),
    ({"messages": []}, "messages"),
    ({"messages": [{"role": "robot", "content": "x"}]}, "messages"),
    ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]}, "messages"),
    ({"messages": [{"role": "user", "content": "x"}], "max_tokens": 0}, "max_tokens"),
    ({"messages": [{"role": "user", "content": "x"}], "temperature": 5}, "temperature"),
    ({"messages": [{"role": "user", "content": "x"}], "top_p": "a"}, "top_p"),
    ({"messages": [{"role": "user", "content": "x"}], "stop": ["a", "b", "c", "d", "e"]}, "stop"),
    ({"messages": [{"role": "user", "content": "x"}], "stream": "yes"}, "stream"),
    ({"messages": [{"role": "user", "content": "x"}], "n": 2}, "n"),
])
def test_bad_bodies_are_400(api, body, param):
    server, _, _ = api
    status, err = req(server, "POST", "/v1/chat/completions", body, key=api_key(server))
    assert status == 400, err
    assert err["error"]["type"] == "invalid_request_error"
    assert err["error"]["param"] == param


def test_invalid_json_and_unknown_route(api):
    server, _, _ = api
    status, _, data = raw(server, "POST", "/v1/chat/completions", b"{not json", key=api_key(server))
    assert status == 400 and json.loads(data)["error"]["type"] == "invalid_request_error"
    status, err = req(server, "POST", "/v1/embeddings", {"input": "x"}, key=api_key(server))
    assert status == 404 and "error" in err


def test_no_model_is_503(tmp_path):
    server = _server(tmp_path, UnavailableModel())
    try:
        key = api_key(server)
        status, err = req(server, "POST", "/v1/chat/completions",
                          {"messages": [{"role": "user", "content": "hi"}]}, key=key)
        assert status == 503
        assert err["error"]["code"] == "model_not_available"
        assert set(err["error"]) >= {"message", "type", "code"}
        assert req(server, "GET", "/v1/models", key=key) == (200, {"object": "list", "data": []})
    finally:
        server.stop()


def test_key_persists_across_restart_and_rotates(tmp_path):
    first = _server(tmp_path, FakeModel())
    try:
        key = api_key(first)
        run_token = first.token
    finally:
        first.stop()
    second = _server(tmp_path, FakeModel())
    try:
        assert second.token != run_token  # the shell token is per run...
        assert api_key(second) == key  # ...the API key is not
        assert req(second, "GET", "/v1/models", key=key)[0] == 200
        status, view = req(second, "POST", "/api/openai/rotate", key=second.token)
        assert status == 200 and view["apiKey"] != key
        assert req(second, "GET", "/v1/models", key=key)[0] == 401
        assert req(second, "GET", "/v1/models", key=view["apiKey"])[0] == 200
        assert (tmp_path / "secrets" / "openai_api_key").read_text().strip() == view["apiKey"]
    finally:
        second.stop()


def test_key_is_never_logged_or_marked(tmp_path):
    marks = []
    server = ApiServer(
        persona_store=PersonaStore(tmp_path / "persona.json"),
        sessions_root=tmp_path / "sessions", data_root=tmp_path, model=FakeModel(),
        skills=SkillsStore(tmp_path / "skills"), static_root=None,
        mark=lambda *args: marks.append(args),
    ).start()
    try:
        req(server, "GET", "/v1/models", key="sk-imurun-guess-123")
        assert marks and all("sk-imurun-guess-123" not in repr(m) for m in marks)
    finally:
        server.stop()


def test_stop_filter_holds_back_partial_matches():
    f = StopFilter(["END"])
    assert f.feed("abc") == "a"
    assert f.feed("E") == "b"
    assert f.feed("N") == "c"
    assert f.feed("D more") == "" and f.hit
    g = StopFilter([])
    assert g.feed("x") == "x"
