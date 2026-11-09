"""User report 2026-11-09: a saved NVIDIA key "vanished" after a restart (the
keyring was locked), the app fell back to the small local model, and its
keyword rule searched the web for "what's on my desktop right now"."""
from __future__ import annotations

import pytest

from services.connectors.tokens import (FileTokenStore, KeyringTokenStore, MirroredStore,
                                        TokenStoreUnavailable, build_secret_store)
from services.research.planner import heuristic_plan


class Keyring:
    def __init__(self):
        self.data = {}
        self.locked = False

    def get_password(self, service, name):
        if self.locked:
            raise RuntimeError("locked")
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        if self.locked:
            raise RuntimeError("locked")
        self.data[(service, name)] = value

    def delete_password(self, service, name):
        self.data.pop((service, name), None)


def test_a_saved_key_survives_a_restart_with_the_keyring_locked(tmp_path):
    ring = Keyring()
    store = build_secret_store(tmp_path, keyring_module=ring)
    if not isinstance(store, MirroredStore):
        pytest.skip("no encrypted file store on this machine (cryptography missing)")
    store.set("remote:nvidia", "nvapi-secret-1")
    ring.locked = True  # next start: the keyring wants a password
    again = build_secret_store(tmp_path, keyring_module=ring)
    assert again.get("remote:nvidia") == "nvapi-secret-1"
    # A new key replaces the old one, even while the keyring is locked.
    again.set("remote:nvidia", "nvapi-secret-2")
    assert build_secret_store(tmp_path, keyring_module=ring).get("remote:nvidia") == "nvapi-secret-2"
    # And it is never stored in plain text.
    assert "nvapi-secret" not in (tmp_path / "secrets" / "connectors.json").read_text()


def test_delete_removes_both_copies(tmp_path):
    ring = Keyring()
    store = build_secret_store(tmp_path, keyring_module=ring)
    if not isinstance(store, MirroredStore):
        pytest.skip("no encrypted file store")
    store.set("remote:nvidia", "k")
    store.delete("remote:nvidia")
    assert store.get("remote:nvidia") is None


@pytest.mark.parametrize("question", [
    "What is on my Desktop right now?", "whats on my desktop", "list the files in my documents",
    "how are you today", "is my laptop running hot right now", "what is 17 * 23 right now",
])
def test_no_keyword_search_for_local_or_time_only_questions(question):
    assert not heuristic_plan(question).needs_web


@pytest.mark.parametrize("question", [
    "latest news on India", "who is the current prime minister of India", "bitcoin price",
    "weather in Delhi today", "search the web for the specs of my laptop model",
])
def test_world_questions_still_search(question):
    assert heuristic_plan(question).needs_web
