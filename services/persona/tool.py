"""Persona tool: the only way the model may read persona data.

The model never touches the file. This tool returns a small structured view
and never raises: a missing or corrupted file becomes `configured=False`
with a reason, so a failure costs no extra turn, compute or latency (V28).
"""
from __future__ import annotations

from dataclasses import dataclass

from .store import PersonaStore


@dataclass(frozen=True)
class PersonaView:
    configured: bool
    name: str = ""
    email_role: str = ""
    description: str = ""
    error: str = ""


class PersonaTool:
    def __init__(self, store: PersonaStore) -> None:
        self._store = store

    def read(self) -> PersonaView:
        try:
            persona = self._store.load()
        except Exception as exc:  # noqa: BLE001 - a tool must answer, not crash
            return PersonaView(configured=False, error=f"{type(exc).__name__}: {exc}")
        if persona is None:
            return PersonaView(configured=False)
        return PersonaView(
            configured=True,
            name=persona.name,
            email_role=persona.email_role,
            description=persona.description,
        )
