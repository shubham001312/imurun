"""Skills live in ~/.imurun/skills: the agent can add one and use any added.

A skill is a directory with a SKILL.md (or a flat .md file). Creation is
validated and atomic; an existing skill is never silently overwritten. A
skill is instructions only - nothing here executes code.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from ..sessions.session import slugify

SKILL_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME = 80
MAX_DESCRIPTION = 200
MAX_BODY = 100_000


def default_root() -> Path:
    return Path(os.path.expanduser("~/.imurun/skills"))


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    body: str
    path: str
    origin: str = "user"


def _single_line(value: str, limit: int) -> str:
    """Frontmatter is line-based: a newline in a field would inject YAML."""
    cleaned = re.sub(r"[\r\n]+", " ", value).strip()
    return cleaned[:limit]


class SkillsStore:
    def __init__(self, root: Path | str | None = None, mark=None) -> None:
        self._root = Path(root) if root else default_root()
        self._mark = mark
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def list(self) -> list[Skill]:
        if not self._root.exists():
            return []
        found: list[Skill] = []
        for entry in sorted(self._root.iterdir()):
            if entry.is_dir():
                file = entry / "SKILL.md"
            elif entry.suffix == ".md":
                file = entry
            else:
                continue
            if file.is_file():
                skill = self._parse(file)
                if skill is None:
                    self._mark_failure(file, "unreadable skill file")
                    continue
                found.append(skill)
        return found

    def _mark_failure(self, path: Path, why: str) -> None:
        if self._mark is None:
            return
        try:
            self._mark("G3-skills", str(path), why)
        except Exception:  # noqa: BLE001 - a broken sink must not hide the skill list
            pass

    def get(self, skill_id: str) -> Skill | None:
        for skill in self.list():
            if skill.id == skill_id:
                return skill
        return None

    def delete(self, skill_id: str) -> bool:
        """Remove one skill. ``False`` when it does not exist.

        ``DELETE /api/skills/{id}`` has always called this, but the method
        did not exist, so the route raised ``AttributeError`` and the client
        saw a dropped connection.
        """
        if not SKILL_ID.match(skill_id or ""):
            return False
        with self._lock:
            folder = self._root / skill_id
            flat = self._root / f"{skill_id}.md"
            removed = False
            if (folder / "SKILL.md").is_file():
                # Only the files this store writes; a folder holding anything
                # else is left alone rather than deleted recursively.
                for name in ("SKILL.md", "SKILL.tmp"):
                    (folder / name).unlink(missing_ok=True)
                try:
                    folder.rmdir()
                except OSError:
                    pass
                removed = True
            if flat.is_file():
                flat.unlink()
                removed = True
            return removed

    def create(
        self,
        name: str,
        description: str,
        body: str,
        *,
        skill_id: str | None = None,
        origin: str = "user",
    ) -> Skill:
        cleaned_name = _single_line(name, MAX_NAME)
        cleaned_description = _single_line(description, MAX_DESCRIPTION)
        cleaned_body = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", body).strip()
        if not cleaned_name:
            raise ValueError("a skill needs a name")
        if len(cleaned_name) > MAX_NAME:
            raise ValueError(f"a skill name cannot exceed {MAX_NAME} characters")
        if not cleaned_description:
            raise ValueError("a skill needs a description")
        if len(cleaned_description) > MAX_DESCRIPTION:
            raise ValueError(f"a skill description cannot exceed {MAX_DESCRIPTION} characters")
        if not cleaned_body:
            raise ValueError("a skill needs instructions")
        if len(cleaned_body) > MAX_BODY:
            raise ValueError(f"a skill body cannot exceed {MAX_BODY} characters")
        identifier = skill_id or slugify(cleaned_name, max_words=8, max_len=60)
        if not SKILL_ID.match(identifier):
            raise ValueError(f"invalid skill id: {identifier!r}")
        if not re.fullmatch(r"[a-z0-9_-]+", origin or ""):
            raise ValueError("origin must be a short lowercase token")

        with self._lock:
            target = self._root / identifier / "SKILL.md"
            flat = self._root / f"{identifier}.md"
            if target.exists() or flat.exists():
                raise FileExistsError(f"skill {identifier!r} already exists at {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            _private_dir(target.parent)
            _private_dir(self._root)
            text = (
                "---\n"
                f"name: {cleaned_name}\n"
                f"description: {cleaned_description}\n"
                f"origin: {origin}\n"
                "---\n\n"
                f"{cleaned_body}\n"
            )
            temporary = target.with_suffix(".tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, target)
            _private_file(target)
            return Skill(
                id=identifier,
                name=cleaned_name,
                description=cleaned_description,
                body=cleaned_body,
                path=str(target),
                origin=origin,
            )

    def _parse(self, path: Path) -> Skill | None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if not text.startswith("---"):
            return Skill(
                id=path.parent.name if path.name == "SKILL.md" else path.stem,
                name=path.parent.name if path.name == "SKILL.md" else path.stem,
                description="",
                body=text.strip(),
                path=str(path),
            )
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        meta: dict[str, str] = {}
        for line in parts[1].splitlines():
            key, _, value = line.partition(":")
            if key.strip():
                meta[key.strip()] = value.strip()
        identifier = path.parent.name if path.name == "SKILL.md" else path.stem
        return Skill(
            id=identifier,
            name=meta.get("name", identifier),
            description=meta.get("description", ""),
            body=parts[2].strip(),
            path=str(path),
            origin=meta.get("origin", "user"),
        )


def _private_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        if os.name != "nt":
            raise


def _private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        if os.name != "nt":
            raise
