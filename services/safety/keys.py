"""G5 keys: refuse placeholders and never echo a key in a message or a log."""
from __future__ import annotations

MIN_KEY_LENGTH = 8

PLACEHOLDERS = frozenset(
    {
        "changeme",
        "change-me",
        "your-key",
        "your-api-key",
        "replace-me",
        "todo",
        "none",
        "null",
        "undefined",
        "example",
        "secret",
        "password",
    }
)
PLACEHOLDER_PREFIXES = ("your-", "replace-", "insert-", "paste-")


class InvalidKey(ValueError):
    """A key that must not be used. The message never contains the key."""


def validate_key(key) -> str:
    if not isinstance(key, str):
        raise InvalidKey("a key must be a string")
    cleaned = key.strip()
    if len(cleaned) < MIN_KEY_LENGTH:
        raise InvalidKey("the key is too short")
    folded = cleaned.casefold()
    if folded in PLACEHOLDERS or folded.startswith(PLACEHOLDER_PREFIXES):
        raise InvalidKey("the key is a placeholder")
    if len(set(cleaned)) == 1:
        raise InvalidKey("the key is a repeated character")
    return cleaned


def redact(key: str | None) -> str:
    if not key:
        return "(none)"
    if len(key) <= 8:
        return "****"
    return "****" + key[-4:]


def scrub(text: str, key: str | None) -> str:
    """Remove an accidental key occurrence from any outgoing message."""
    if not key:
        return text
    return text.replace(key, redact(key))
