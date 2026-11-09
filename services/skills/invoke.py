"""Invoking skills with ``/name`` in a prompt.

A skill is an instruction pack (``store.py``). Writing ``/name`` in a prompt
(at the start or after a space) injects that skill's body into the system
prompt for this one turn. Only names that match a stored skill are treated
as invocations, so ``/usr/bin`` or a URL path never triggers anything.

The token is removed from the prompt the model sees; a prompt that was only
``/name`` becomes "Follow the <name> skill." so the model has something to
answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .store import Skill

INVOKE = re.compile(r"(?:(?<=\s)|^)/([a-z0-9]+(?:[-_][a-z0-9]+)*)(?=\s|$|[.,:;!?])", re.IGNORECASE)
MAX_SKILLS = 3
MAX_SKILL_CHARS = 4000


@dataclass
class Invocation:
    skills: list[Skill] = field(default_factory=list)
    prompt: str = ""

    @property
    def ids(self) -> list[str]:
        return [skill.id for skill in self.skills]


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def find_invocations(prompt: str, skills: list[Skill]) -> Invocation:
    """Split ``prompt`` into the skills it invokes and the remaining text."""
    if not prompt or "/" not in prompt or not skills:
        return Invocation([], prompt)
    by_key: dict[str, Skill] = {}
    for skill in skills:
        by_key.setdefault(_key(skill.id), skill)
        by_key.setdefault(_key(skill.name), skill)
    chosen: list[Skill] = []

    def replace(match: re.Match) -> str:
        skill = by_key.get(_key(match.group(1)))
        if skill is None:
            return match.group(0)
        if skill not in chosen and len(chosen) < MAX_SKILLS:
            chosen.append(skill)
        return ""

    remaining = INVOKE.sub(replace, prompt)
    remaining = re.sub(r"[ \t]{2,}", " ", remaining).strip()
    if chosen and not remaining:
        remaining = "Follow the " + ", ".join(s.name for s in chosen) + " skill" + ("s." if len(chosen) > 1 else ".")
    return Invocation(chosen, remaining if chosen else prompt)


def skills_prompt(skills: list[Skill]) -> str:
    """The system-prompt section for the invoked skills."""
    if not skills:
        return ""
    parts = ["# Skills the user invoked for this request", "Follow these instructions:"]
    for skill in skills:
        body = skill.body.strip()
        if len(body) > MAX_SKILL_CHARS:
            body = body[:MAX_SKILL_CHARS] + "\n... [skill text cut]"
        parts.append(f"## {skill.name}\n{body}")
    return "\n\n".join(parts)
