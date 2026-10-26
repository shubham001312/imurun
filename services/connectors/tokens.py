"""Token storage for connectors: OS keychain by default, never in files or logs."""
from __future__ import annotations


class TokenStoreUnavailable(RuntimeError):
    """No OS keychain is available; tokens must not fall back to a plain file."""


class MemoryTokenStore:
    """Test double; never used for real credentials."""

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    def get(self, provider: str) -> str | None:
        return self._tokens.get(provider)

    def set(self, provider: str, token: str) -> None:
        if not provider or not token:
            raise ValueError("provider and token are required")
        self._tokens[provider] = token

    def delete(self, provider: str) -> None:
        self._tokens.pop(provider, None)


class KeyringTokenStore:
    """Uses the OS keychain (Windows Credential Manager, macOS Keychain, libsecret)."""

    def __init__(self, service: str = "imurun") -> None:
        try:
            import keyring  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on the machine
            raise TokenStoreUnavailable(
                "no OS keychain library is installed; connectors need the keyring package"
            ) from exc
        self._keyring = keyring
        self._service = service

    def get(self, provider: str) -> str | None:
        return self._keyring.get_password(self._service, provider)

    def set(self, provider: str, token: str) -> None:
        if not provider or not token:
            raise ValueError("provider and token are required")
        self._keyring.set_password(self._service, provider, token)

    def delete(self, provider: str) -> None:
        try:
            self._keyring.delete_password(self._service, provider)
        except Exception:  # noqa: BLE001 - deleting a missing token is fine
            pass


def redact(token: str | None) -> str:
    """Safe form for logs: never the token itself."""
    if not token:
        return "(none)"
    if len(token) <= 4:
        return "****"
    return "****" + token[-4:]
