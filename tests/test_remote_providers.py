"""Hosted model providers: registry, G5 host allow-listing, routes, selection.

Every network call goes to a fake OpenAI-compatible server on 127.0.0.1
(tests/remote_fakes.py). No real provider is ever called and no real key
exists here; the keys below are made-up strings.
"""
from __future__ import annotations

import http.client
import json
import logging
import time
from urllib.parse import urlsplit

import pytest

from model.engine_client import EngineModelClient
from model.remote_client import OpenAICompatClient, RemoteError, check_base, scrub
from services.persona.store import PersonaStore
from services.remote import hosted, providers
from services.remote.providers import BadBaseUrl, clean_custom_url, key_mismatch, split_id
from services.server.api import ApiServer
from services.skills.store import SkillsStore
from tests.remote_fakes import FakeProvider

KEY = "sk-fake-provider-key-0123456789"
NV_KEY = "nvapi-fake-nvidia-key-0123456789"


class FakeFish:
    """A local engine stand-in: configured, one model, scripted reply."""

    configured = True
    loaded = True

    def __init__(self) -> None:
        self.selected: list = []

    def select(self, path):
        self.selected.append(path)
        return True

    def selected_name(self):
        return "Local-1B-Q4_K_M.gguf"

    def generate(self, messages):
        return "local reply"

    async def generate_stream(self, messages):
        yield "local reply"

    def context_limit(self):
        return 4096

    def stats(self):
        return {}

    def model_info(self):
        return {"name": "Local 1B"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    # Secrets go to an encrypted file under the test home, never the OS keychain;
    # no developer environment key leaks into a test.
    monkeypatch.setenv("IMURUN_TOKEN_STORE", "file")
    for provider in providers.PROVIDERS.values():
        if provider.env_key:
            monkeypatch.delenv(provider.env_key, raising=False)
        monkeypatch.delenv(f"IMURUN_{provider.id.upper()}_BASE_URL", raising=False)


@pytest.fixture()
def fake():
    server = FakeProvider().start()
    yield server
    server.stop()


def _server(home):
    engine = EngineModelClient(fish=FakeFish())
    server = ApiServer(
        persona_store=PersonaStore(home / "persona.json"),
        sessions_root=home / "sessions",
        data_root=home,
        model=engine,
        skills=SkillsStore(home / "skills"),
        static_root=None,
    ).start()
    hosted.attach(server, engine, home)  # what services/server/app.py does at start-up
    return server, engine


@pytest.fixture()
def api(tmp_path):
    server, engine = _server(tmp_path)
    yield server, engine, tmp_path
    server.stop()


BODIES: list[str] = []


def req(server, method, path, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    headers = {"Authorization": f"Bearer {server.token}"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    BODIES.append(data)
    return resp.status, (json.loads(data) if data else {})


def _connect_custom(server, fake, key=KEY, name="Office gateway"):
    return req(server, "POST", "/api/remote/custom/key", {"key": key, "base_url": fake.base_url, "name": name})


# ------------------------------------------------------------------ registry

def test_registry_lists_every_provider_with_fixed_https_hosts():
    ids = list(providers.PROVIDERS)
    assert ids == ["nvidia", "openrouter", "deepseek", "openai", "groq", "together", "mistral", "custom"]
    for provider in providers.PROVIDERS.values():
        assert provider.label and provider.key_format
        if provider.custom:
            assert provider.base_url == "" and provider.hosts == ()
            continue
        parts = urlsplit(provider.base_url)
        assert parts.scheme == "https" and parts.hostname in provider.hosts, provider.id
        assert urlsplit(provider.key_url).scheme == "https"
        assert provider.recommended, provider.id
    assert providers.PROVIDERS["nvidia"].secret_name == "model.nvidia"
    assert providers.PROVIDERS["deepseek"].base_url == "https://api.deepseek.com"
    assert dict(providers.PROVIDERS["openrouter"].headers)["HTTP-Referer"].startswith("https://")
    assert providers.PROVIDERS["openai"].token_param == "max_completion_tokens"


def test_ids_split_on_the_first_colon_only():
    provider, model = split_id("openrouter:deepseek/deepseek-r1:free")
    assert provider.id == "openrouter" and model == "deepseek/deepseek-r1:free"
    assert split_id("nvidia:meta/llama-3.3-70b-instruct")[1] == "meta/llama-3.3-70b-instruct"
    assert split_id("unknown:model") is None and split_id("nvidia:") is None and split_id("plain") is None


def test_key_format_checks_catch_the_wrong_provider():
    assert "nvapi-" in key_mismatch(providers.PROVIDERS["nvidia"], "sk-something-long")
    assert "OpenRouter" in key_mismatch(providers.PROVIDERS["deepseek"], "sk-or-v1-abcdefgh")
    assert "NVIDIA" in key_mismatch(providers.PROVIDERS["groq"], "nvapi-abcdefgh")
    assert key_mismatch(providers.PROVIDERS["openai"], "sk-proj-abcdefgh") == ""
    assert key_mismatch(providers.PROVIDERS["custom"], "nvapi-abcdefgh") == ""


# ------------------------------------------------------- G5: host allow-list

def test_a_key_is_only_sent_to_its_own_host():
    with pytest.raises(RemoteError) as caught:
        OpenAICompatClient(KEY, base_url="https://api.example.net/v1", hosts=("api.openai.com",), label="OpenAI")
    assert caught.value.kind == "host"
    with pytest.raises(RemoteError):  # right host, plain http
        check_base("http://api.openai.com/v1", ("api.openai.com",))
    with pytest.raises(RemoteError):  # credentials in the URL
        check_base("https://user:pw@api.openai.com/v1", ("api.openai.com",))
    assert check_base("https://api.openai.com/v1/", ("api.openai.com",)) == "https://api.openai.com/v1"
    assert check_base("http://127.0.0.1:9/v1", ()) == "http://127.0.0.1:9/v1"  # loopback (tests)
    client = OpenAICompatClient(KEY, base_url="http://127.0.0.1:9/v1", hosts=())
    for url in ("http://127.0.0.1:10/v1/models", "https://evil.example/v1/models", "http://localhost:9/v1/models"):
        with pytest.raises(RemoteError) as caught:
            client._url(url)
        assert caught.value.kind == "host"


def test_custom_urls_need_https_unless_loopback():
    for bad in ("http://llm.example.com/v1", "ftp://x.example/v1", "https://", "", None,
                "https://u:p@llm.example.com/v1", "https://llm.example.com/v1?k=1"):
        with pytest.raises(BadBaseUrl):
            clean_custom_url(bad)
    assert clean_custom_url(" https://llm.example.com/v1/ ") == "https://llm.example.com/v1"
    assert clean_custom_url("http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert clean_custom_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1"
    assert clean_custom_url("http://[::1]:8000/v1") == "http://[::1]:8000/v1"


def test_custom_http_to_another_machine_is_refused_before_any_request(api, fake):
    server, _, _ = api
    status, reply = req(server, "POST", "/api/remote/custom/key",
                        {"key": KEY, "base_url": f"http://192.0.2.10:{fake.port}/v1"})
    assert status == 400 and reply["field"] == "base_url" and "https" in reply["error"]
    assert fake.requests == []


def test_redirects_are_not_followed_so_the_key_stays(fake):
    other = FakeProvider(key=None).start()
    try:
        fake.redirect_to = other.base_url + "/models"
        client = OpenAICompatClient(KEY, base_url=fake.base_url, label="Fake")
        with pytest.raises(RemoteError) as caught:
            client.list_models()
        assert caught.value.kind == "redirect"
        assert other.requests == []  # the key never reached the redirect target
    finally:
        other.stop()


def test_pagination_links_to_another_host_are_refused(fake):
    other = FakeProvider(key=None).start()
    try:
        fake.next_link = other.base_url + "/models?page=2"
        client = OpenAICompatClient(KEY, base_url=fake.base_url, label="Fake")
        with pytest.raises(RemoteError) as caught:
            client.list_models()
        assert caught.value.kind == "host" and other.requests == []
        fake.next_link = "/v1/models?page=2"  # same host: followed
        assert {m["id"] for m in client.list_models()} >= {"fake/chat-large"}
    finally:
        other.stop()


def test_provider_error_text_is_scrubbed_of_the_key(fake):
    client = OpenAICompatClient("sk-wrong-key-abcdefghijkl", base_url=fake.base_url, label="Fake")
    with pytest.raises(RemoteError) as caught:
        client.list_models()
    assert caught.value.kind == "key" and "sk-wrong" not in str(caught.value)
    assert scrub("bad key sk-proj-ABCDEFGHIJ12 here", None) == "bad key **** here"


# --------------------------------------------------------------- client shapes

def test_catalogue_shapes_and_filters(fake):
    client = OpenAICompatClient(KEY, base_url=fake.base_url, label="Fake", recommended=("fake/chat-small",))
    rows = client.list_models()
    assert [r["id"] for r in rows] == ["fake/chat-small", "fake/chat-large"]  # recommended first, embed dropped
    assert rows[0]["recommended"] and rows[0]["context"] == 8192 and rows[1]["context"] == 65536
    fake.shape = "list"  # Together answers with a bare list
    fake.models = [{"id": "a/chat", "type": "chat", "context_length": 4096}, {"id": "a/img", "type": "image"}]
    assert [r["id"] for r in client.list_models()] == ["a/chat"]
    fake.shape = "data"  # Mistral marks chat capability; OpenRouter output modalities
    fake.models = [{"id": "m-chat", "capabilities": {"completion_chat": True}, "max_context_length": 32768},
                   {"id": "m-ocr2", "capabilities": {"completion_chat": False}},
                   {"id": "x/painter", "architecture": {"output_modalities": ["image"]}}]
    assert [(r["id"], r["context"]) for r in client.list_models()] == [("m-chat", 32768)]
    fake.shape = "none"
    with pytest.raises(RemoteError) as caught:
        client.list_models()
    assert caught.value.kind == "no_models"


def test_provider_request_differences(fake):
    openai = providers.PROVIDERS["openai"]
    client = OpenAICompatClient(KEY, "gpt-5-mini", base_url=fake.base_url, label="OpenAI",
                                token_param=openai.token_param, no_sampling_prefixes=openai.no_sampling_prefixes,
                                headers={"X-Title": "IMURUN"})
    assert client.generate([{"role": "user", "content": "hi"}]) == "Hello from the fake"
    sent = fake.chat_requests()[-1]
    assert sent["body"]["max_completion_tokens"] and "max_tokens" not in sent["body"]
    assert "temperature" not in sent["body"] and sent["headers"]["x-title"] == "IMURUN"
    plain = OpenAICompatClient(KEY, "fake/chat-large", base_url=fake.base_url, stream_usage=False)
    plain.generate([{"role": "user", "content": "hi"}])
    body = fake.chat_requests()[-1]["body"]
    assert body["max_tokens"] and body["temperature"] == 0.5 and "stream_options" not in body


def test_clear_errors_for_rate_limit_and_gone_models(fake):
    client = OpenAICompatClient(KEY, "fake/old", base_url=fake.base_url, label="Fake")
    fake.chat_status = 429
    with pytest.raises(RemoteError) as caught:
        client.generate([{"role": "user", "content": "hi"}])
    assert caught.value.kind == "rate_limit" and "rate limit" in str(caught.value)
    fake.chat_status, fake.chat_error = 404, "The model `fake/old` does not exist"
    with pytest.raises(RemoteError) as caught:
        client.generate([{"role": "user", "content": "hi"}])
    assert caught.value.kind == "model_gone" and "no longer offers fake/old" in str(caught.value)
    fake.chat_status, fake.chat_error = 400, "model_decommissioned: the model fake/old was decommissioned"
    with pytest.raises(RemoteError) as caught:
        client.generate([{"role": "user", "content": "hi"}])
    assert caught.value.kind == "model_gone"
    fake.chat_status = 402
    with pytest.raises(RemoteError) as caught:
        client.generate([{"role": "user", "content": "hi"}])
    assert caught.value.kind == "credits"


def test_small_context_windows_are_respected():
    client = OpenAICompatClient(KEY, base_url="http://127.0.0.1:9/v1")
    client.set_model("m", context=8192)
    assert client.context_limit() == 8192
    client.set_model("m", context=200000)
    assert client.context_limit() == 32768
    client.set_model("m")
    assert client.context_limit() == 32768


# ---------------------------------------------------------------------- routes

def test_list_shows_every_provider_and_nothing_is_set_up(api):
    server, _, _ = api
    status, view = req(server, "GET", "/api/remote")
    assert status == 200 and view["selected"] is None
    assert [p["id"] for p in view["providers"]] == list(providers.PROVIDERS)
    assert not any(p["configured"] for p in view["providers"])
    assert req(server, "GET", "/api/remote/nope")[0] == 404
    assert req(server, "GET", "/api/remote/openai/models")[0] == 400
    status, models = req(server, "GET", "/api/models")
    assert status == 200 and models["hosted"] == []


def test_custom_provider_end_to_end(api, fake, caplog):
    caplog.set_level(logging.DEBUG)
    server, engine, home = api
    status, view = _connect_custom(server, fake)
    assert status == 200, view
    assert view["configured"] and view["verified"] and view["label"] == "Office gateway"
    assert view["key_hint"] == "****" + KEY[-4:] and view["base_url"] == fake.base_url
    # first connect switched on the first models the server lists
    assert view["enabled"] == ["fake/chat-large", "fake/chat-small"]

    status, cat = req(server, "GET", "/api/remote/custom/models")
    assert status == 200 and cat["has_models"] and {m["id"] for m in cat["models"]} == {"fake/chat-large", "fake/chat-small"}
    status, view = req(server, "POST", "/api/remote/custom/models", {"enabled": ["custom:fake/chat-small", "fake/chat-small"]})
    assert status == 200 and view["enabled"] == ["fake/chat-small"] and view["contexts"] == {"fake/chat-small": 8192}

    status, picked = req(server, "POST", "/api/models/select", {"id": "custom:fake/chat-small"})
    assert status == 200, picked
    assert picked["remote"] and picked["selected"] == "Chat Small (Office gateway)"
    assert engine.remote and engine.engine == "custom" and engine.context_limit() == 8192

    status, models = req(server, "GET", "/api/models")
    assert [h["id"] for h in models["hosted"]] == ["custom:fake/chat-small"]
    assert models["hosted"][0]["selected"] and models["hosted"][0]["context"] == 8192
    assert not any(m.get("selected") for m in models["models"])

    status, session = req(server, "POST", "/api/sessions", {"first_command": "hello"})
    status, turn = req(server, "POST", "/api/turns", {"session_id": session["id"], "text": "say hello"})
    assert status == 200, turn
    view = {}
    for _ in range(200):
        status, view = req(server, "GET", f"/api/turns/{turn['turn_id']}")
        if view.get("state") != "running":
            break
        time.sleep(0.05)
    assert view["state"] == "done", view
    assert "Hello from the fake" in view["answer"]
    sent = fake.chat_requests()[-1]
    assert sent["headers"]["authorization"] == f"Bearer {KEY}" and sent["body"]["model"] == "fake/chat-small"

    # The key is never in a response, the preference file or a log line.
    assert not any(KEY in body for body in BODIES)
    assert KEY not in (home / hosted.PREFS_FILE).read_text()
    assert KEY not in caplog.text

    # Removing the provider routes back to the local engine.
    status, view = req(server, "DELETE", "/api/remote/custom/key")
    assert status == 200 and not view["configured"] and not engine.remote
    assert req(server, "GET", "/api/models")[1]["hosted"] == []


def test_bad_key_is_refused_and_not_stored(api, fake):
    server, _, _ = api
    status, reply = _connect_custom(server, fake, key="sk-not-the-right-one-123")
    assert status == 400 and reply["kind"] == "key" and "rejected" in reply["error"]
    assert "sk-not-the-right" not in json.dumps(reply)
    assert not req(server, "GET", "/api/remote/custom")[1]["configured"]


def test_custom_server_without_model_list_or_key(api):
    server, engine, _ = api
    fake = FakeProvider(key=None).start()
    fake.shape = "none"
    try:
        status, view = req(server, "POST", "/api/remote/custom/key",
                           {"key": "", "base_url": fake.base_url, "name": "Laptop llama"})
        assert status == 200 and view["keyless"] and view["configured"] and "type the model ids" in view["warning"]
        status, cat = req(server, "GET", "/api/remote/custom/models")
        assert status == 200 and cat["has_models"] is False and cat["models"] == []
        req(server, "POST", "/api/remote/custom/models", {"enabled": ["my-local-model"]})
        status, picked = req(server, "POST", "/api/models/select", {"id": "custom:my-local-model"})
        assert status == 200 and engine.remote
        assert engine.generate([{"role": "user", "content": "hi"}]) == "Hello from the fake"
        assert "authorization" not in fake.chat_requests()[-1]["headers"]
    finally:
        fake.stop()


def test_custom_key_is_bound_to_the_host_it_was_saved_with(api, fake):
    server, engine, _ = api
    assert _connect_custom(server, fake)[0] == 200
    req(server, "POST", "/api/models/select", {"id": "custom:fake/chat-large"})
    other = FakeProvider(key=None).start()
    try:
        # A new address with the key field left empty: the saved key may not follow.
        status, reply = req(server, "POST", "/api/remote/custom/key",
                            {"key": "", "base_url": f"http://localhost:{other.port}/v1"})
        assert status == 400 and reply["field"] == "key" and "127.0.0.1" in reply["error"]
        assert other.requests == []
        # Same host, new name, empty key field: the saved key stays.
        status, view = req(server, "POST", "/api/remote/custom/key",
                           {"key": "", "base_url": fake.base_url, "name": "Renamed"})
        assert status == 200 and view["key_hint"].endswith(KEY[-4:]) and view["label"] == "Renamed"
        assert engine.selected_name() == "Chat Large (Renamed)"
        # Explicitly keyless on a new host: the old key is deleted, not reused.
        status, view = req(server, "POST", "/api/remote/custom/key",
                           {"key": "", "no_key": True, "base_url": other.base_url})
        assert status == 200 and view["keyless"] and view["key_hint"] == ""
        assert engine.remote and engine.generate([{"role": "user", "content": "hi"}])
        assert all("authorization" not in r["headers"] for r in other.requests)
    finally:
        other.stop()


def test_keys_a_header_cannot_carry_are_refused_without_echo(api, fake, caplog):
    caplog.set_level(logging.DEBUG)
    server, _, _ = api
    two_lines = "sk-first-line-abcdef\nsk-second-line-ghijkl"
    status, reply = _connect_custom(server, fake, key=two_lines)
    assert status == 400 and reply["field"] == "key" and "line break" in reply["error"]
    assert "sk-first-line" not in json.dumps(reply) and "sk-first-line" not in caplog.text
    assert fake.requests == []
    # The client itself turns http.client's ValueError (which quotes the header) into a clean error.
    client = OpenAICompatClient(two_lines, base_url=fake.base_url, label="Fake")
    with pytest.raises(RemoteError) as caught:
        client.list_models()
    assert caught.value.kind == "key" and "sk-first" not in str(caught.value)


def test_custom_key_is_bound_to_the_port_too(api, fake):
    server, _, _ = api
    assert _connect_custom(server, fake)[0] == 200
    other = FakeProvider(key=None).start()
    try:
        status, reply = req(server, "POST", "/api/remote/custom/key", {"key": "", "base_url": other.base_url})
        assert status == 400 and reply["field"] == "key" and other.requests == []
    finally:
        other.stop()


def test_a_failed_key_save_never_pairs_the_old_key_with_the_new_host(api, fake, monkeypatch):
    server, _, _ = api
    assert _connect_custom(server, fake)[0] == 200
    other = FakeProvider(key=None).start()
    try:
        store = server._remote_settings._store()

        def broken(*_args):
            raise OSError("disk full")

        monkeypatch.setattr(store, "set", broken)
        status, reply = req(server, "POST", "/api/remote/custom/key",
                            {"key": "sk-new-key-for-other-host-123", "base_url": other.base_url})
        assert status == 500 and "OSError" in reply["error"]
        monkeypatch.undo()
        # Only the verify probe reached the new host, with the new key.
        assert [r["headers"].get("authorization") for r in other.requests] == ["Bearer sk-new-key-for-other-host-123"]
        # The old key was dropped before the address changed: nothing is usable now.
        assert req(server, "GET", "/api/remote/custom/models")[0] == 400
        assert hosted.client_for(server, providers.PROVIDERS["custom"])._key() is None
        assert len(other.requests) == 1
        assert all(r["headers"].get("authorization") != f"Bearer {KEY}" for r in other.requests)
    finally:
        other.stop()


def test_selection_is_restored_after_a_restart(tmp_path, fake):
    server, engine = _server(tmp_path)
    try:
        assert _connect_custom(server, fake)[0] == 200
        assert req(server, "POST", "/api/models/select", {"id": "custom:fake/chat-large"})[0] == 200
    finally:
        server.stop()
    server, engine = _server(tmp_path)
    try:
        assert engine.remote and engine.remote_client.model == "fake/chat-large"
        assert req(server, "GET", "/api/remote")[1]["selected"] == "custom:fake/chat-large"
        # Choosing a local model forgets the hosted choice for the next start.
        engine.select(tmp_path / "local.gguf")
        hosted.local_selected(server)
    finally:
        server.stop()
    server, engine = _server(tmp_path)
    try:
        assert not engine.remote
    finally:
        server.stop()


def test_unknown_or_unconfigured_ids_are_clear_errors(api):
    server, _, _ = api
    status, reply = req(server, "POST", "/api/models/select", {"id": "groq:llama-3.3-70b-versatile"})
    assert status == 400 and "Connect Groq" in reply["error"]
    status, reply = req(server, "POST", "/api/remote/groq/key", {"key": "nvapi-not-a-groq-key"})
    assert status == 400 and "NVIDIA" in reply["error"]
    status, reply = req(server, "POST", "/api/remote/openai/key", {"key": "your-api-key"})
    assert status == 400 and reply["field"] == "key"


def test_openrouter_through_its_preset_sends_attribution_headers(api, monkeypatch):
    server, engine, _ = api
    fake = FakeProvider(key="sk-or-v1-fake-openrouter-key-0123").start()
    monkeypatch.setenv("IMURUN_OPENROUTER_BASE_URL", fake.base_url)
    try:
        fake.models = [{"id": "deepseek/deepseek-chat", "context_length": 131072},
                       {"id": "vendor/model:free", "context_length": 8000}]
        status, view = req(server, "POST", "/api/remote/openrouter/key", {"key": fake.key})
        assert status == 200 and view["enabled"] == ["deepseek/deepseek-chat"]
        assert fake.requests[0]["path"] == "/v1/models"  # verified with the authenticated model list
        assert req(server, "POST", "/api/models/select", {"id": "openrouter:vendor/model:free"})[0] == 200
        engine.generate([{"role": "user", "content": "hi"}])
        headers = fake.chat_requests()[-1]["headers"]
        assert headers["x-title"] == "IMURUN" and headers["http-referer"].startswith("https://")
        assert engine.selected_name() == "Model (free) (OpenRouter)"
    finally:
        fake.stop()


# ---------------------------------------------------- NVIDIA backward compat

def test_nvidia_routes_ids_secret_and_old_prefs_still_work(tmp_path, monkeypatch):
    fake = FakeProvider(key=NV_KEY).start()
    monkeypatch.setenv("IMURUN_NVIDIA_BASE_URL", fake.base_url)
    # The NVIDIA-only code's file format (version 1).
    (tmp_path / "remote_models.json").write_text(json.dumps(
        {"enabled": ["meta/llama-3.3-70b-instruct"], "selected": "meta/llama-3.3-70b-instruct"}))
    try:
        server, engine = _server(tmp_path)
        try:
            assert not engine.remote  # no key yet: nothing restored
            status, view = req(server, "POST", "/api/remote/nvidia/key", {"key": "sk-wrong-shape-key"})
            assert status == 400 and "nvapi-" in view["error"]
            status, view = req(server, "POST", "/api/remote/nvidia/key", {"key": NV_KEY})
            assert status == 200 and view["verified"]
            assert fake.chat_requests()[0]["body"]["max_tokens"] == 1  # NVIDIA's list is public: one-token check
            for field in ("provider", "configured", "key_hint", "from_env", "store", "enabled", "selected",
                          "recommended", "signup_url"):
                assert field in view, field
            assert view["enabled"] == ["meta/llama-3.3-70b-instruct"]  # kept from the old file
            store = server._remote_settings._store()
            assert store.get("model.nvidia") == NV_KEY
            status, picked = req(server, "POST", "/api/models/select", {"id": "nvidia:meta/llama-3.1-8b-instruct"})
            assert status == 200 and picked["id"] == "nvidia:meta/llama-3.1-8b-instruct"
            assert engine.engine == "nvidia" and engine.selected_name().endswith("(NVIDIA)")
            status, view = req(server, "GET", "/api/remote/nvidia")
            assert view["selected"] == "meta/llama-3.1-8b-instruct"
            assert view["enabled"] == ["meta/llama-3.3-70b-instruct", "meta/llama-3.1-8b-instruct"]
            assert not any(NV_KEY in body for body in BODIES)
        finally:
            server.stop()
        data = json.loads((tmp_path / "remote_models.json").read_text())
        assert data["version"] == 2 and data["selected"] == "nvidia:meta/llama-3.1-8b-instruct"
        server, engine = _server(tmp_path)
        try:
            assert engine.remote and engine.remote_client.model == "meta/llama-3.1-8b-instruct"
            status, view = req(server, "DELETE", "/api/remote/nvidia/key")
            assert status == 200 and not view["configured"] and not engine.remote
        finally:
            server.stop()
    finally:
        fake.stop()


def test_old_import_paths_still_resolve():
    from model.nvidia_client import NVIDIA_BASE_URL, NvidiaClient
    from services.remote import nvidia

    client = NvidiaClient(NV_KEY, "meta/llama-3.1-8b-instruct")
    assert client.provider == "nvidia" and client.base_url == NVIDIA_BASE_URL
    assert nvidia.SECRET_NAME == "model.nvidia" and nvidia.ID_PREFIX == "nvidia:"
    with pytest.raises(RemoteError):
        NvidiaClient(NV_KEY, base_url="https://evil.example/v1")
