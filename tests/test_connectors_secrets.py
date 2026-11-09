"""The connector secret store: every backend (CLI tools and modules faked),
the real DPAPI path on Windows, migration, and the "not encrypted" warning."""
from __future__ import annotations

import json
import subprocess

import pytest

from services.connectors import tokens as T
from services.connectors.tokens import (
    FileTokenStore,
    KeyringTokenStore,
    MacKeychainStore,
    MemoryTokenStore,
    SecretToolStore,
    TokenStoreUnavailable,
    build_secret_store,
    dpapi_available,
    store_view,
)


class FakeRunner:
    """Records argv and stdin; answers like the real tools."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.vault: dict[str, str] = {}

    def __call__(self, args, *, input_text=None, timeout=15.0):
        self.calls.append((list(args), input_text))
        tool = args[0]
        if tool.endswith("security"):
            if args[1] == "-i":
                # add-generic-password -U -s "imurun" -a "key" -w "secret"
                parts = __import__("shlex").split(input_text)
                key, secret = parts[parts.index("-a") + 1], parts[parts.index("-w") + 1]
                self.vault[key] = secret
                return subprocess.CompletedProcess(args, 0, "", "")
            key = args[args.index("-a") + 1]
            if args[1] == "find-generic-password":
                return subprocess.CompletedProcess(args, 0 if key in self.vault else 44,
                                                   self.vault.get(key, "") + "\n", "")
            self.vault.pop(key, None)
            return subprocess.CompletedProcess(args, 0, "", "")
        key = args[args.index("account") + 1]
        if args[1] == "store":
            self.vault[key] = input_text
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "lookup":
            return subprocess.CompletedProcess(args, 0 if key in self.vault else 1, self.vault.get(key, ""), "")
        self.vault.pop(key, None)
        return subprocess.CompletedProcess(args, 0, "", "")


def roundtrip(store):
    store.set("cloudflare", 'cf-"tok"\\en')
    assert store.get("cloudflare") == 'cf-"tok"\\en'
    store.set("cloudflare", "second")
    assert store.get("cloudflare") == "second"
    store.delete("cloudflare")
    assert store.get("cloudflare") is None


def test_macos_keychain_keeps_the_secret_out_of_argv():
    runner = FakeRunner()
    store = MacKeychainStore(runner=runner)
    roundtrip(store)
    for args, stdin in runner.calls:
        assert "second" not in " ".join(args) and 'cf-"tok"' not in " ".join(args)
    writes = [stdin for args, stdin in runner.calls if args[1] == "-i"]
    assert writes and writes[0].startswith('add-generic-password -U -s "imurun" -a "cloudflare" -w ')
    assert store.backend_name() == "macOS Keychain" and store.encrypted
    with pytest.raises(ValueError):
        store.set("x", "two\nlines")


def test_libsecret_passes_the_secret_on_stdin():
    runner = FakeRunner()
    store = SecretToolStore(runner=runner)
    roundtrip(store)
    stores = [(args, stdin) for args, stdin in runner.calls if args[1] == "store"]
    assert stores[0][0][-4:] == ["service", "imurun", "account", "cloudflare"]
    assert all("second" not in " ".join(args) for args, _ in runner.calls)
    assert store.backend_name() == "libsecret"


def test_keyring_module_backend():
    class FakeKeyring:
        def __init__(self):
            self.data = {}

        def get_password(self, service, key):
            return self.data.get((service, key))

        def set_password(self, service, key, value):
            self.data[(service, key)] = value

        def delete_password(self, service, key):
            del self.data[(service, key)]

        def get_keyring(self):
            return self

    store = KeyringTokenStore(module=FakeKeyring())
    roundtrip(store)
    store.delete("missing")                     # no raise
    assert store.backend_name() == "keyring (FakeKeyring)"


def test_backend_choice(tmp_path):
    def which(present):
        return lambda name: f"/usr/bin/{name}" if name in present else None

    runner = FakeRunner()
    assert isinstance(build_secret_store(tmp_path, platform="darwin", which=which({"security"}), runner=runner,
                                         keyring_module=False, choice=""), MacKeychainStore)
    assert isinstance(build_secret_store(tmp_path, platform="linux", which=which({"secret-tool"}), runner=runner,
                                         keyring_module=False, choice=""), SecretToolStore)
    fallback = build_secret_store(tmp_path, platform="linux", which=which(set()), keyring_module=False, choice="")
    assert isinstance(fallback, FileTokenStore)
    fake = type("K", (), {"get_password": lambda *a: None, "get_keyring": lambda self: self})()
    assert isinstance(build_secret_store(tmp_path, keyring_module=fake, choice=""), KeyringTokenStore)
    assert isinstance(build_secret_store(tmp_path, choice="memory"), MemoryTokenStore)
    assert isinstance(build_secret_store(tmp_path, choice="file", keyring_module=fake), FileTokenStore)
    with pytest.raises(TokenStoreUnavailable):
        build_secret_store(tmp_path, platform="win32", which=which(set()), choice="libsecret", keyring_module=False)


@pytest.mark.skipif(not dpapi_available(), reason="Windows DPAPI only")
def test_real_dpapi_file_and_migration_of_plain_values(tmp_path):
    path = tmp_path / "secrets" / "connectors.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"github": "gho_plain", "render": "rnd_plain"}))   # an older version's file
    store = FileTokenStore(path)
    assert store.backend_name() == "Windows DPAPI file" and store.encrypted and not store.warning
    assert store.get("github") == "gho_plain"                                     # plain still readable
    store.set("cloudflare", "cf-secret")
    raw = json.loads(path.read_text())
    assert raw["__encryption__"] == "dpapi"
    assert all(v.startswith("dpapi:") for k, v in raw.items() if k != "__encryption__")
    assert "plain" not in path.read_text() and "cf-secret" not in path.read_text()
    assert store.get("github") == "gho_plain" and store.get("cloudflare") == "cf-secret"
    assert store.get("__encryption__") is None


class FakeAesGcm:
    """Stands in for cryptography's AESGCM: a reversible, key-dependent transform."""

    def __init__(self, key):
        assert len(key) == 32
        self.key = key

    def _xor(self, nonce, data):
        stream = (self.key + nonce) * (len(data) // 44 + 1)
        return bytes(a ^ b for a, b in zip(data, stream))

    def encrypt(self, nonce, data, aad):
        return self._xor(nonce, data) + b"TAG" + aad

    def decrypt(self, nonce, data, aad):
        if not data.endswith(b"TAG" + aad):
            raise ValueError("auth failed")
        return self._xor(nonce, data[: -3 - len(aad)])


def test_aes_gcm_file_when_cryptography_is_present(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "dpapi_available", lambda: False)
    monkeypatch.setattr(T, "_aes_class", lambda: FakeAesGcm)
    path = tmp_path / "secrets" / "connectors.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"slack": "xoxb-plain"}))
    store = FileTokenStore(path)
    assert store.backend_name() == "AES-256-GCM file" and store.encrypted
    store.set("render", "rnd_secret")
    text = path.read_text()
    assert "rnd_secret" not in text and "xoxb-plain" not in text and '"aes-gcm"' in text
    assert (tmp_path / "secrets" / "connectors.key").is_file()
    assert FileTokenStore(path).get("render") == "rnd_secret"                   # key persisted
    assert store.get("slack") == "xoxb-plain"


def test_no_encryption_is_marked_and_warned(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "dpapi_available", lambda: False)
    monkeypatch.setattr(T, "_aes_class", lambda: None)
    path = tmp_path / "connectors.json"
    store = FileTokenStore(path)
    store.set("render", "rnd_x")
    assert json.loads(path.read_text())["__encryption__"] == "none"
    view = store_view(store)
    assert view == {"backend": "plain file (not encrypted)", "encrypted": False, "warning": store.warning}
    assert "WITHOUT encryption" in store.warning
    with pytest.raises(TokenStoreUnavailable):
        FileTokenStore(tmp_path / "other.json", encryption="aes-gcm")
