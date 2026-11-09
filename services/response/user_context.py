"""Who the user is: the profile and saved facts, given to the model every turn.

Before this, "who am I" got "I don't know your personal identity" even with a
name in Settings > Profile: nothing put the profile or the saved facts in
front of the model. Now every turn carries a short "About the user" block:

* ``settings/profile.json`` (name, email, role, timezone; Settings > Profile);
* facts saved in the ``user`` category of the facts store
  (``<data root>/facts/facts.json``, the same file the ``facts`` tool writes).

When the name is unknown, the block tells the model to say so and ask. When
the user states their name ("my name is Shubham", "call me Shubham"),
``capture`` saves it by code - into the facts store and, if the profile has
no name yet, into the profile - so it works with small models that get no
tools. Nothing here leaves the computer.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

MAX_FACTS = 12
MAX_VALUE = 160
_LOCK = threading.Lock()

_NAME = r"(?P<name>[A-Za-zÀ-ɏऀ-෿][\wÀ-ɏऀ-෿'.-]{0,30}(?:\s+[A-Za-zÀ-ɏऀ-෿][\wÀ-ɏऀ-෿'.-]{0,30}){0,2})"
_STATED_NAME = (
    re.compile(r"\bmy\s+name\s+is\s+" + _NAME + r"\s*(?:[.!,]|$)", re.IGNORECASE),
    re.compile(r"\bcall\s+me\s+" + _NAME + r"\s*(?:[.!,]|$)", re.IGNORECASE),
    re.compile(r"\bi'?m\s+called\s+" + _NAME + r"\s*(?:[.!,]|$)", re.IGNORECASE),
    # "I am Shubham" only as a short message with a capitalised name, so
    # "I am tired" or "I am working on X" never become a name.
    re.compile(r"^\s*(?i:hi|hello|hey)?[,!\s]*(?i:i\s+am)\s+(?P<name>[A-Z][\w'.-]{1,30}(?:\s+[A-Z][\w'.-]{1,30})?)\s*[.!]?\s*$"),
)
_NOT_NAMES = {"fine", "good", "ok", "okay", "here", "back", "done", "ready", "sorry", "sure", "not",
              "busy", "tired", "working", "trying", "using", "looking", "new", "the", "a", "an"}


def facts_file(data_root: Path | None = None) -> Path:
    if data_root is None:
        from ..paths import data_root as _root

        data_root = _root()
    return Path(data_root) / "facts" / "facts.json"


def _read_facts(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _profile(settings: Any) -> dict:
    if settings is None:
        return {}
    try:
        return dict(settings.read("profile") or {})
    except Exception:  # noqa: BLE001 - a broken settings file means "unknown"
        return {}


def _clean(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:MAX_VALUE]


def user_facts(path: Path | None = None) -> dict[str, str]:
    """Facts saved about the user (category ``user``), newest first."""
    data = _read_facts(path or facts_file())
    entries = data.get("user") or {}
    items = []
    for key, entry in entries.items() if isinstance(entries, dict) else []:
        value = entry.get("value") if isinstance(entry, dict) else entry
        if _clean(value):
            updated = entry.get("updated", 0) if isinstance(entry, dict) else 0
            items.append((float(updated or 0), _clean(key), _clean(value)))
    items.sort(reverse=True)
    return {key: value for _, key, value in items[:MAX_FACTS]}


def block(settings: Any, *, facts_path: Path | None = None, can_save: bool = True) -> str:
    """The "About the user" section of the system prompt."""
    profile = _profile(settings)
    facts = user_facts(facts_path)
    name = _clean(profile.get("name")) or facts.get("name", "")
    lines = ["# About the user",
             "From the user's profile and saved memory on this computer. Use it when it helps; "
             "never invent anything that is not here."]
    if name:
        lines.append(f"- Name: {name}")
    for label, key in (("Email", "email"), ("Role", "role"), ("Timezone", "timezone")):
        value = _clean(profile.get(key))
        if value and not (key == "role" and value.lower() == "local"):
            lines.append(f"- {label}: {value}")
    for key, value in facts.items():
        if key != "name":
            lines.append(f"- {key}: {value}")
    if not name:
        lines.append("- Name: not known yet. If the user asks who they are, or the answer needs their "
                     "name, say you do not know it yet and ask for it.")
    if can_save:
        lines.append("When the user tells you something lasting about themselves (name, job, projects, "
                     "preferences), save it with the facts tool: action set, category user, a short key.")
    return "\n".join(lines)


def stated_name(prompt: str) -> str | None:
    """The name the user states in this message, or None."""
    text = (prompt or "").strip()
    if not text or len(text) > 300:
        return None
    for pattern in _STATED_NAME:
        match = pattern.search(text)
        if match:
            name = re.sub(r"\s+", " ", match.group("name")).strip(" .'-")
            if name and name.split()[0].lower() not in _NOT_NAMES and len(name) >= 2:
                return " ".join(part[:1].upper() + part[1:] for part in name.split())
    return None


def capture(prompt: str, settings: Any, *, facts_path: Path | None = None) -> str:
    """Save a name the user just stated. Returns a context note for the model, or ""."""
    name = stated_name(prompt)
    if not name:
        return ""
    path = facts_path or facts_file()
    with _LOCK:
        data = _read_facts(path)
        data.setdefault("user", {})["name"] = {"value": name, "updated": time.time()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            return ""
    profile_note = ""
    if settings is not None and not _clean(_profile(settings).get("name")):
        try:
            current = _profile(settings)
            current["name"] = name
            settings.write("profile", current)
            profile_note = " and in their profile"
        except Exception:  # noqa: BLE001 - the fact is saved either way
            profile_note = ""
    return (f"[memory] The user just told you their name: {name}. It is now saved in memory"
            f"{profile_note}. Greet them by name and confirm you will remember it.")
