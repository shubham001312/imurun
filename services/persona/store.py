"""Persona: the user's name and email voice.

The model never reads this file. Only PersonaComposer touches it, derives a
sanitised style directive, and applies the name/signature by code (V28).
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

MAX_NAME = 80
MAX_ROLE = 120
MAX_DESCRIPTION = 500
MAX_FILE_BYTES = 64 * 1024

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class Persona:
    name: str
    email_role: str
    description: str = ""


def _clean(value: str, limit: int) -> str:
    return _CONTROL.sub("", value).strip()[:limit]


class PersonaStore:
    """Private storage for the persona. Not exposed to the model."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else Path(os.path.expanduser("~/.imurun/persona.json"))
        self._lock = threading.Lock()

    def save(self, persona: Persona) -> Persona:
        name = _clean(persona.name, MAX_NAME)
        role = _clean(persona.email_role, MAX_ROLE)
        description = _clean(persona.description, MAX_DESCRIPTION)
        if not name:
            raise ValueError("the persona name is required")
        if not role:
            raise ValueError("the persona email role is required")
        cleaned = Persona(name=name, email_role=role, description=description)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _private_dir(self._path.parent)
            payload = {
                "name": cleaned.name,
                "email_role": cleaned.email_role,
                "description": cleaned.description,
            }
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self._path)
            _private_file(self._path)
        return cleaned

    def load(self) -> Persona | None:
        with self._lock:
            if not self._path.exists():
                return None
            if self._path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("the persona file is too large")
            data = json.loads(self._path.read_text(encoding="utf-8"))
        return Persona(
            name=str(data.get("name", "")),
            email_role=str(data.get("email_role", "")),
            description=str(data.get("description", "")),
        )

    def exists(self) -> bool:
        return self._path.exists()


class PersonaComposer:
    """The only bridge between the persona file and the model."""

    def __init__(self, store: PersonaStore) -> None:
        self._store = store

    def directive(self) -> str:
        """A short style instruction. Never the raw file, never the path."""
        persona = self._store.load()
        if persona is None:
            return ""
        lines = [
            f"You are writing email on behalf of {persona.name}.",
            f"Your role: {persona.email_role}.",
        ]
        if persona.description:
            lines.append(f"Behaviour the user asked for: {persona.description}.")
        lines.append(
            "Write in the user's voice. Never invent facts about the user. "
            "If asked directly whether you are an assistant, say so honestly."
        )
        return " ".join(lines)

    def apply_signature(self, body: str) -> str:
        """Apply the name and role by code, not by asking the model."""
        persona = self._store.load()
        if persona is None:
            return body
        signature = f"{persona.name}"
        if persona.email_role:
            signature = f"{persona.name}\n{persona.email_role}"
        block = f"\n\n--\n{signature}"
        if body.rstrip().endswith(signature):
            return body
        return body.rstrip() + block


def _private_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
