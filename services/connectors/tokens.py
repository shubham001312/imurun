"""Secret storage for connectors: one ``SecretStore`` interface, the backend
chosen at runtime, never a token in a log, an error or an API response.

Backends, in the order ``build_secret_store`` tries them:

==================  ===========================================  =================
backend             where                                        ``backend_name()``
==================  ===========================================  =================
keyring             the ``keyring`` package, when importable     ``keyring (<impl>)``
macOS Keychain      ``security`` CLI; secrets go through stdin   ``macOS Keychain``
                    of ``security -i``, never the argument list
libsecret           ``secret-tool``; the secret goes via stdin   ``libsecret``
encrypted file      ``<home>/secrets/connectors.json`` (0600):   ``Windows DPAPI file`` /
                    each value DPAPI-encrypted (Windows) or      ``AES-256-GCM file``
                    AES-256-GCM (``cryptography`` installed)
plain file          same file, values NOT encrypted; marked      ``plain file (not encrypted)``
                    ``"__encryption__": "none"`` and reported as
                    a warning by ``GET /api/connectors/{id}/config``
==================  ===========================================  =================

``IMURUN_TOKEN_STORE`` forces one: ``memory``, ``file``, ``keyring``,
``keychain``, ``libsecret``. Every store has ``get`` / ``set`` / ``delete``,
``backend_name()``, ``encrypted`` and ``warning``.

File format: ``{"__encryption__": "dpapi"|"aes-gcm"|"none", "<key>": "<value>"}``
with values ``dpapi:<b64>``, ``aesgcm:<b64 nonce+ciphertext>`` or plain.
Plain values (older versions) are read and re-encrypted on the next write.
The AES key is a random 256-bit key in ``connectors.key`` (0600) next to the
file: it protects against the file being copied alone (a backup, a sync
folder), not against someone who can read both files as the same user.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import sys
import threading
from pathlib import Path
from typing import Callable, Protocol

SERVICE = "imurun"
MARKER = "__encryption__"


class TokenStoreUnavailable(RuntimeError):
    """The requested backend is not available on this machine."""


class SecretStore(Protocol):
    encrypted: bool
    warning: str

    def get(self, provider: str) -> str | None: ...
    def set(self, provider: str, token: str) -> None: ...
    def delete(self, provider: str) -> None: ...
    def backend_name(self) -> str: ...


def _require(provider: str, token: str | None = "x") -> None:
    if not provider or not token:
        raise ValueError("provider and token are required")


# --------------------------------------------------------------------- memory
class MemoryTokenStore:
    """Test double; never used for real credentials."""

    encrypted = False
    warning = "tokens are kept in memory only and are lost when IMURUN stops"

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def get(self, provider: str) -> str | None:
        return self._tokens.get(provider)

    def set(self, provider: str, token: str) -> None:
        _require(provider, token)
        self._tokens[provider] = token

    def delete(self, provider: str) -> None:
        self._tokens.pop(provider, None)

    def backend_name(self) -> str:
        return "memory"


# -------------------------------------------------------------------- keyring
class KeyringTokenStore:
    """The ``keyring`` package (Credential Manager, Keychain, Secret Service)."""

    encrypted = True
    warning = ""

    def __init__(self, service: str = SERVICE, *, module=None) -> None:
        if module is None:
            try:
                import keyring as module  # type: ignore
            except ImportError as exc:  # pragma: no cover - depends on the machine
                raise TokenStoreUnavailable("the keyring package is not installed") from exc
        self._keyring = module
        self._service = service

    # A locked keyring (Linux Secret Service, macOS Keychain) answers a read by
    # showing an unlock prompt and waiting. The server once never finished
    # starting because of that. Every call waits at most KEYRING_TIMEOUT; after
    # a timeout the keyring is treated as locked for LOCKED_BACKOFF seconds, so
    # one prompt on screen does not turn into one per request.
    KEYRING_TIMEOUT = 4.0
    LOCKED_BACKOFF = 120.0
    _locked_until = 0.0

    def _bounded(self, fn, *args):
        cls = type(self)
        if time.monotonic() < cls._locked_until:
            raise TokenStoreUnavailable("the system keyring is locked; unlock it to use saved keys")
        box: dict = {}

        def work() -> None:
            try:
                box["value"] = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                box["error"] = exc

        thread = threading.Thread(target=work, name="imurun-keyring", daemon=True)
        thread.start()
        thread.join(self.KEYRING_TIMEOUT)
        if thread.is_alive():
            cls._locked_until = time.monotonic() + self.LOCKED_BACKOFF
            raise TokenStoreUnavailable("the system keyring did not answer (locked?); unlock it to use saved keys")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def get(self, provider: str) -> str | None:
        return self._bounded(self._keyring.get_password, self._service, provider)

    def set(self, provider: str, token: str) -> None:
        _require(provider, token)
        self._bounded(self._keyring.set_password, self._service, provider, token)

    def delete(self, provider: str) -> None:
        try:
            self._keyring.delete_password(self._service, provider)
        except Exception:  # noqa: BLE001 - deleting a missing token is fine
            pass

    def backend_name(self) -> str:
        try:
            impl = type(self._keyring.get_keyring()).__name__
        except Exception:  # noqa: BLE001
            impl = "default"
        return f"keyring ({impl})"


# ----------------------------------------------------------- CLI-backed stores
Runner = Callable[..., subprocess.CompletedProcess]


def _run(args: list[str], *, input_text: str | None = None, timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=input_text, capture_output=True, text=True, timeout=timeout, check=False)


def _quote_security(value: str) -> str:
    # ``security -i`` splits like a shell; double quotes with \" and \\ escaped.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MacKeychainStore:
    """macOS login keychain through ``/usr/bin/security``.

    ``set`` feeds ``add-generic-password -U -s imurun -a <key> -w <secret>``
    to ``security -i`` on stdin, so the secret never appears in the process
    list. ``get`` uses ``find-generic-password -w`` (the secret comes back on
    stdout); ``delete`` uses ``delete-generic-password``.
    """

    encrypted = True
    warning = ""

    def __init__(self, service: str = SERVICE, *, runner: Runner = _run, binary: str = "security") -> None:
        self._service = service
        self._run = runner
        self._bin = binary

    def get(self, provider: str) -> str | None:
        result = self._run([self._bin, "find-generic-password", "-s", self._service, "-a", provider, "-w"])
        if result.returncode != 0:
            return None
        value = (result.stdout or "").rstrip("\n")
        return value or None

    def set(self, provider: str, token: str) -> None:
        _require(provider, token)
        if "\n" in token or "\r" in token:
            raise ValueError("a secret cannot contain line breaks")
        command = (f"add-generic-password -U -s {_quote_security(self._service)} "
                   f"-a {_quote_security(provider)} -w {_quote_security(token)}\n")
        result = self._run([self._bin, "-i"], input_text=command)
        if result.returncode != 0 or "error" in (result.stderr or "").lower():
            raise TokenStoreUnavailable("the macOS Keychain refused to store the secret")

    def delete(self, provider: str) -> None:
        self._run([self._bin, "delete-generic-password", "-s", self._service, "-a", provider])

    def backend_name(self) -> str:
        return "macOS Keychain"


class SecretToolStore:
    """Linux Secret Service (GNOME Keyring, KWallet) through ``secret-tool``.

    ``secret-tool store`` reads the secret from stdin, so it is never an argument.
    """

    encrypted = True
    warning = ""

    def __init__(self, service: str = SERVICE, *, runner: Runner = _run, binary: str = "secret-tool") -> None:
        self._service = service
        self._run = runner
        self._bin = binary

    def _attrs(self, provider: str) -> list[str]:
        return ["service", self._service, "account", provider]

    def get(self, provider: str) -> str | None:
        result = self._run([self._bin, "lookup", *self._attrs(provider)])
        if result.returncode != 0:
            return None
        return (result.stdout or "").rstrip("\n") or None

    def set(self, provider: str, token: str) -> None:
        _require(provider, token)
        result = self._run([self._bin, "store", f"--label=IMURUN {provider}", *self._attrs(provider)],
                           input_text=token)
        if result.returncode != 0:
            raise TokenStoreUnavailable("the Secret Service refused to store the secret")

    def delete(self, provider: str) -> None:
        self._run([self._bin, "clear", *self._attrs(provider)])

    def backend_name(self) -> str:
        return "libsecret"


# ------------------------------------------------------------- file codecs
_DPAPI_PREFIX = "dpapi:"
_AES_PREFIX = "aesgcm:"
_DPAPI_ENTROPY = b"imurun-connector-tokens"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI (CryptProtectData / CryptUnprotectData) via ctypes.

    The blob can only be decrypted by the same Windows user on the same
    machine. Raises OSError when the call fails.
    """
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    source = ctypes.create_string_buffer(data, len(data))
    entropy_buffer = ctypes.create_string_buffer(_DPAPI_ENTROPY, len(_DPAPI_ENTROPY))
    blob_in = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_char)))
    entropy = Blob(len(_DPAPI_ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    if not function(ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None, 0x1,
                    ctypes.byref(blob_out)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def dpapi_available() -> bool:
    if os.name != "nt":
        return False
    try:
        return _dpapi(_dpapi(b"probe", True), False) == b"probe"
    except Exception:  # noqa: BLE001
        return False


class _DpapiCodec:
    name, label, prefix = "dpapi", "Windows DPAPI file", _DPAPI_PREFIX

    def encode(self, text: str) -> str:
        return self.prefix + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")

    def decode(self, value: str) -> str:
        return _dpapi(base64.b64decode(value[len(self.prefix):]), False).decode("utf-8")


class _AesGcmCodec:
    name, label, prefix = "aes-gcm", "AES-256-GCM file", _AES_PREFIX

    def __init__(self, key_path: Path, aead_class) -> None:
        self._key_path = key_path
        self._aead_class = aead_class
        self._aead = None

    def _cipher(self):
        if self._aead is None:
            if self._key_path.is_file():
                key = base64.b64decode(self._key_path.read_text(encoding="ascii").strip())
            else:
                key = os.urandom(32)
                _write_private(self._key_path, base64.b64encode(key).decode("ascii"))
            if len(key) != 32:
                raise OSError("the connector key file is damaged")
            self._aead = self._aead_class(key)
        return self._aead

    def encode(self, text: str) -> str:
        nonce = os.urandom(12)
        sealed = self._cipher().encrypt(nonce, text.encode("utf-8"), b"imurun")
        return self.prefix + base64.b64encode(nonce + sealed).decode("ascii")

    def decode(self, value: str) -> str:
        raw = base64.b64decode(value[len(self.prefix):])
        return self._cipher().decrypt(raw[:12], raw[12:], b"imurun").decode("utf-8")


class _PlainCodec:
    name, label, prefix = "none", "plain file (not encrypted)", ""

    def encode(self, text: str) -> str:
        return text

    def decode(self, value: str) -> str:
        return value


def _write_private(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _aes_class():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
    except ImportError:
        return None
    return AESGCM


def _choose_codec(path: Path, encryption: str | None):
    """``encryption``: None = best available, or "dpapi" / "aes-gcm" / "none"."""
    if encryption in (None, "dpapi") and dpapi_available():
        return _DpapiCodec()
    if encryption == "dpapi":
        raise TokenStoreUnavailable("Windows DPAPI is not available here")
    if encryption in (None, "aes-gcm"):
        aead = _aes_class()
        if aead is not None:
            return _AesGcmCodec(path.with_name("connectors.key"), aead)
        if encryption == "aes-gcm":
            raise TokenStoreUnavailable("the cryptography package is not installed")
    return _PlainCodec()


class FileTokenStore:
    """``<home>/secrets/connectors.json``: 0600 file in a 0700 folder.

    Values are encrypted with the best codec available (DPAPI, else
    AES-256-GCM, else none - marked and warned). Values written with another
    codec, or plain, are still read; every write re-encodes them all with the
    current codec (migration).
    """

    def __init__(self, path: Path | str, *, protect: bool | None = None,
                 encryption: str | None = None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if protect is False:
            encryption = "none"
        self._codec = _choose_codec(self._path, encryption)
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError:
            pass

    @property
    def protected(self) -> bool:
        return self._codec.name != "none"

    @property
    def encrypted(self) -> bool:
        return self.protected

    @property
    def encryption(self) -> str:
        return self._codec.name

    @property
    def warning(self) -> str:
        if self.protected:
            return ""
        return (f"connector secrets are stored WITHOUT encryption in {self._path} "
                "(only file permissions protect them); install the keyring or cryptography "
                "package to encrypt them")

    def backend_name(self) -> str:
        return self._codec.label

    def _read(self) -> dict:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _decode(self, value: str) -> str | None:
        try:
            if value.startswith(_DPAPI_PREFIX):
                return _DpapiCodec().decode(value)
            if value.startswith(_AES_PREFIX):
                if isinstance(self._codec, _AesGcmCodec):
                    return self._codec.decode(value)
                aead = _aes_class()
                if aead is None:
                    return None
                return _AesGcmCodec(self._path.with_name("connectors.key"), aead).decode(value)
        except Exception:  # noqa: BLE001 - another user's / machine's blob reads as missing
            return None
        return value

    def _write(self, data: dict) -> None:
        data[MARKER] = self._codec.name
        _write_private(self._path, json.dumps(data, indent=2))

    def get(self, provider: str) -> str | None:
        if provider == MARKER:
            return None
        with self._lock:
            value = self._read().get(provider)
        if not value or not isinstance(value, str):
            return None
        return self._decode(value) or None

    def set(self, provider: str, token: str) -> None:
        _require(provider, token)
        if provider == MARKER:
            raise ValueError("reserved key")
        with self._lock:
            data = self._read()
            migrated: dict = {}
            for key, value in data.items():
                if key == MARKER or not isinstance(value, str):
                    continue
                if self._codec.prefix and value.startswith(self._codec.prefix):
                    migrated[key] = value
                    continue
                plain = self._decode(value)
                if plain is not None:
                    migrated[key] = self._codec.encode(plain)
                else:
                    migrated[key] = value      # unreadable here: keep, never destroy
            migrated[provider] = self._codec.encode(str(token))
            self._write(migrated)

    def delete(self, provider: str) -> None:
        with self._lock:
            data = self._read()
            if provider in data:
                data.pop(provider, None)
                self._write(data)


# --------------------------------------------------------------- choosing
class MirroredStore:
    """The OS store first, with an encrypted file copy as a fallback.

    A locked Linux keyring (or a Keychain that wants a password) cannot be
    read at start-up without a prompt, so saved API keys looked "lost" after
    every restart. Each write now also goes to the encrypted file store; a
    read falls back to it when the OS store is locked, slow or empty. Only
    used when the file is encrypted (never a plain-text copy).
    """

    def __init__(self, primary, fallback) -> None:
        self.primary = primary
        self.fallback = fallback
        self.encrypted = True
        self.warning = ""

    def get(self, provider: str) -> str | None:
        try:
            value = self.primary.get(provider)
        except Exception:  # noqa: BLE001 - locked or unavailable: use the copy
            value = None
        if value:
            return value
        try:
            return self.fallback.get(provider)
        except Exception:  # noqa: BLE001
            return None

    def set(self, provider: str, token: str) -> None:
        _require(provider, token)
        self.fallback.set(provider, token)  # always lands, even with the keyring locked
        try:
            self.primary.set(provider, token)
        except Exception:  # noqa: BLE001 - the encrypted copy holds it
            pass

    def delete(self, provider: str) -> None:
        for store in (self.primary, self.fallback):
            try:
                store.delete(provider)
            except Exception:  # noqa: BLE001
                pass

    def backend_name(self) -> str:
        name = self.primary.backend_name() if hasattr(self.primary, "backend_name") else "os store"
        return f"{name} + encrypted file"


def build_secret_store(home: Path | str, *, platform: str | None = None,
                       which: Callable[[str], str | None] = shutil.which, runner: Runner = _run,
                       keyring_module=None, choice: str | None = None):
    """The best store this machine offers (see the module docstring)."""
    platform = platform or sys.platform
    choice = (choice if choice is not None else os.getenv("IMURUN_TOKEN_STORE", "")).strip().lower()
    file_path = Path(home) / "secrets" / "connectors.json"
    if choice == "memory":
        return MemoryTokenStore()
    if choice == "file":
        return FileTokenStore(file_path)
    if choice in ("", "keyring", "auto") and keyring_module is not False:
        try:
            primary = KeyringTokenStore(module=keyring_module)
        except TokenStoreUnavailable:
            if choice == "keyring":
                raise
        else:
            if choice == "keyring":
                return primary
            try:
                copy = FileTokenStore(file_path)
            except Exception:  # noqa: BLE001 - no file store: keyring alone
                return primary
            return MirroredStore(primary, copy) if copy.encrypted else primary
    if choice in ("", "auto", "keychain") and platform == "darwin" and which("security"):
        return MacKeychainStore(runner=runner, binary=which("security") or "security")
    if choice == "keychain":
        raise TokenStoreUnavailable("the macOS security tool is not available")
    if choice in ("", "auto", "libsecret") and platform.startswith("linux") and which("secret-tool"):
        return SecretToolStore(runner=runner, binary=which("secret-tool") or "secret-tool")
    if choice == "libsecret":
        raise TokenStoreUnavailable("secret-tool (libsecret) is not installed")
    return FileTokenStore(file_path)


def store_view(store) -> dict:
    """For config views: which backend, whether it encrypts, any warning."""
    name = store.backend_name() if hasattr(store, "backend_name") else type(store).__name__
    return {"backend": name, "encrypted": bool(getattr(store, "encrypted", False)),
            "warning": str(getattr(store, "warning", "") or "")}


def redact(token: str | None) -> str:
    """Safe form for logs: never the token itself."""
    if not token:
        return "(none)"
    if len(token) <= 4:
        return "****"
    return "****" + token[-4:]
