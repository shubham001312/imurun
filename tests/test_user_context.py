""""Who am I": the profile and saved memory reach the model; a stated name is saved.

From the field: "who am I" got "I don't know your personal identity" although
the user had a profile. services/response/user_context.py fixes that.
"""
from __future__ import annotations

import json

import pytest

from services.response import user_context
from services.response.loop import AgentLoop
from services.server.settings import SettingsStore


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def generate(self, messages):
        self.seen.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else "ok"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("IMURUN_DATA_ROOT", str(tmp_path / "data"))
    settings = SettingsStore(tmp_path / "data" / "settings")
    return settings, tmp_path / "data" / "facts" / "facts.json"


def system_text(model):
    return model.seen[0][0]["content"]


def test_profile_reaches_the_model(env):
    settings, _ = env
    settings.write("profile", {"name": "Shubham Mallick", "email": "dev@imutechz.in",
                               "role": "Founder", "timezone": "Asia/Kolkata"})
    model = FakeModel(["You are Shubham Mallick."])
    AgentLoop(model, session_id="s", settings=settings).run("who am I")
    text = system_text(model)
    assert "# About the user" in text and "Name: Shubham Mallick" in text
    assert "Timezone: Asia/Kolkata" in text and "Role: Founder" in text


def test_unknown_name_tells_the_model_to_ask(env):
    settings, _ = env
    model = FakeModel(["I don't know your name yet. What should I call you?"])
    AgentLoop(model, session_id="s", settings=settings).run("who am I")
    assert "not known yet" in system_text(model) and "ask for it" in system_text(model)


def test_stated_name_is_saved_to_memory_and_profile(env):
    settings, facts = env
    model = FakeModel(["Nice to meet you, Shubham!"])
    AgentLoop(model, session_id="s", settings=settings).run("my name is shubham")
    saved = json.loads(facts.read_text())
    assert saved["user"]["name"]["value"] == "Shubham"
    assert settings.read("profile")["name"] == "Shubham"
    user_msg = [m for m in model.seen[0] if m["role"] == "user"][-1]["content"]
    assert "[memory] The user just told you their name: Shubham" in user_msg
    # Next turn, the name is part of what the model knows.
    model2 = FakeModel(["You are Shubham."])
    AgentLoop(model2, session_id="s2", settings=settings).run("who am I")
    assert "Name: Shubham" in system_text(model2)


def test_existing_profile_name_is_not_overwritten(env):
    settings, facts = env
    settings.write("profile", {"name": "Shubham Mallick"})
    AgentLoop(FakeModel(["Hi Sam"]), session_id="s", settings=settings).run("call me Sam")
    assert settings.read("profile")["name"] == "Shubham Mallick"
    assert json.loads(facts.read_text())["user"]["name"]["value"] == "Sam"


@pytest.mark.parametrize("text, name", [
    ("my name is shubham", "Shubham"),
    ("Hi, I am Shubham", "Shubham"),
    ("call me Shubham Mallick.", "Shubham Mallick"),
    ("I'm called Ria", "Ria"),
])
def test_stated_name_forms(text, name):
    assert user_context.stated_name(text) == name


@pytest.mark.parametrize("text", [
    "I am tired", "I am working on the website", "I am fine thanks", "who am I",
    "my name is not important, just help me", "what is my name",
])
def test_not_a_name(text):
    assert user_context.stated_name(text) is None


def test_saved_user_facts_are_listed_newest_first(env):
    _, facts = env
    facts.parent.mkdir(parents=True)
    facts.write_text(json.dumps({"user": {
        "job": {"value": "builds IMURUN", "updated": 2},
        "editor": {"value": "VS Code", "updated": 5}},
        "general": {"wifi": {"value": "secret", "updated": 9}}}))
    assert list(user_context.user_facts(facts)) == ["editor", "job"]  # only the user category
    text = user_context.block(None, facts_path=facts)
    assert "editor: VS Code" in text and "wifi" not in text


def test_sub_agents_do_not_get_the_profile(env):
    settings, _ = env
    settings.write("profile", {"name": "Shubham"})
    model = FakeModel(["done"])
    AgentLoop(model, session_id="s", settings=settings, depth=1, directive="helper").run("task")
    assert "About the user" not in system_text(model)
