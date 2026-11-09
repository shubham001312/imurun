"""Skills live in ~/.imurun/skills: the agent can add one and use any added.

A skill is a directory with a SKILL.md (or a flat .md file). Creation is
validated and atomic; an existing skill is never silently overwritten. A
skill is instructions only - nothing here executes code.

A skill installed from GitHub (``github.py``) also carries plain-text
resource files next to its SKILL.md and a ``.source.json`` sidecar that
records where it came from and which files the install wrote.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from ..sessions.session import slugify

SKILL_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME = 80
MAX_DESCRIPTION = 200
MAX_BODY = 100_000
SOURCE_FILE = ".source.json"
# A resource next to SKILL.md: plain text only, at most four folders deep,
# every component a plain name (no "..", no leading dot, no separators).
_RESOURCE_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
RESOURCE_SUFFIXES = (".md", ".txt")
MAX_RESOURCE_DEPTH = 4


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
    # ``owner/repo@commit:path`` for a skill installed from GitHub, else "".
    source: str = ""


def _single_line(value: str, limit: int) -> str:
    """Frontmatter is line-based: a newline in a field would inject YAML."""
    cleaned = re.sub(r"[\r\n]+", " ", value).strip()
    return cleaned[:limit]


def safe_resource_path(relative: object) -> str | None:
    """``relative`` as a clean POSIX path inside a skill folder, or ``None``.

    Only ``.md``/``.txt`` files; never a top-level ``SKILL.md`` (that is the
    skill itself); no absolute paths, drive letters, backslashes, ``..``/``.``
    parts or hidden names (which also keeps the sidecar out of reach).
    """
    if not isinstance(relative, str) or not relative or "\\" in relative:
        return None
    if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
        return None
    parts = relative.split("/")
    if len(parts) > MAX_RESOURCE_DEPTH or any(not _RESOURCE_PART.fullmatch(p) for p in parts):
        return None
    if len(parts) == 1 and parts[0].lower() == "skill.md":
        return None
    if not parts[-1].lower().endswith(RESOURCE_SUFFIXES):
        return None
    return "/".join(parts)


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
            if entry.name.startswith("."):
                continue  # an install in progress (.tmp-*) or a swap backup (.old-*)
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

    def exists(self, skill_id: str) -> bool:
        """True when a folder skill, a flat skill, or anything else holds the id."""
        if not SKILL_ID.match(skill_id or ""):
            return False
        folder = self._root / skill_id
        return folder.exists() or folder.is_symlink() or (self._root / f"{skill_id}.md").exists()

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
            if not folder.is_symlink() and (folder / "SKILL.md").is_file():
                # Only the files this store writes (SKILL.md, the sidecar and the
                # resources the sidecar lists); a folder holding anything else is
                # left alone rather than deleted recursively.
                for relative in _manifest_files(folder):
                    file = folder.joinpath(*relative.split("/"))
                    if not file.is_symlink() and file.is_file():
                        file.unlink()
                    _prune_empty(file.parent, folder)
                for name in ("SKILL.md", "SKILL.tmp", SOURCE_FILE):
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
        meta = _validate(name, description, body, skill_id, origin)
        identifier = meta["id"]
        with self._lock:
            target = self._root / identifier / "SKILL.md"
            flat = self._root / f"{identifier}.md"
            if target.exists() or flat.exists():
                raise FileExistsError(f"skill {identifier!r} already exists at {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            _private_dir(target.parent)
            _private_dir(self._root)
            text = _frontmatter(meta, origin, "") + f"{meta['body']}\n"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, target)
            _private_file(target)
            return Skill(
                id=identifier,
                name=meta["name"],
                description=meta["description"],
                body=meta["body"],
                path=str(target),
                origin=origin,
            )

    def install(
        self,
        name: str,
        description: str,
        body: str,
        *,
        skill_id: str,
        origin: str,
        source: str = "",
        files: dict[str, str] | None = None,
        manifest: dict | None = None,
        replace: bool = False,
    ) -> Skill:
        """Write a whole skill folder (SKILL.md + text resources) atomically.

        The folder is built under ``<root>/.tmp-<id>-<random>`` (a directory
        this call creates, so nothing in it can be a planted symlink) and
        renamed into place: a failed install leaves nothing behind and a reader
        never sees half a skill. Without ``replace`` an existing skill raises
        ``FileExistsError``; ``replace`` (Update) swaps a folder skill. ``files``
        maps resource paths (checked by ``safe_resource_path``) to text.
        ``manifest`` becomes the ``.source.json`` sidecar, plus the file list.
        """
        meta = _validate(name, description, body, skill_id, origin)
        cleaned_source = _single_line(source or "", 400)
        resources: dict[str, str] = {}
        for relative, text in (files or {}).items():
            clean = safe_resource_path(relative)
            if clean is None:
                raise ValueError(f"unsafe resource path: {relative!r}")
            if len(text) > MAX_BODY:
                raise ValueError(f"resource {clean} exceeds {MAX_BODY} characters")
            resources[clean] = text
        identifier = meta["id"]
        with self._lock:
            final = self._root / identifier
            flat = self._root / f"{identifier}.md"
            taken = final.exists() or final.is_symlink() or flat.exists()
            if taken and not replace:
                raise FileExistsError(f"skill {identifier!r} already exists")
            if replace and (final.is_symlink() or flat.exists()
                            or (final.exists() and not (final / "SKILL.md").is_file())):
                raise FileExistsError(f"skill {identifier!r} cannot be replaced")
            self._root.mkdir(parents=True, exist_ok=True)
            _private_dir(self._root)
            staging = self._root / f".tmp-{identifier}-{secrets.token_hex(4)}"
            staging.mkdir(mode=0o700)
            try:
                _private_dir(staging)
                skill_text = _frontmatter(meta, origin, cleaned_source) + f"{meta['body']}\n"
                _write_new(staging, "SKILL.md", skill_text)
                for relative, content in sorted(resources.items()):
                    _write_new(staging, relative, content)
                sidecar = dict(manifest or {})
                sidecar["files"] = sorted(resources)
                _write_new(staging, SOURCE_FILE, json.dumps(sidecar, indent=2, sort_keys=True))
                backup = None
                if final.exists():
                    backup = self._root / f".old-{identifier}-{secrets.token_hex(4)}"
                    os.replace(final, backup)
                try:
                    os.replace(staging, final)
                except OSError:
                    if backup is not None:
                        os.replace(backup, final)
                    raise
                if backup is not None:
                    shutil.rmtree(backup, ignore_errors=True)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            return Skill(
                id=identifier, name=meta["name"], description=meta["description"],
                body=meta["body"], path=str(final / "SKILL.md"), origin=origin,
                source=cleaned_source,
            )

    def source_manifest(self, skill_id: str) -> dict:
        """The ``.source.json`` sidecar of an installed skill, or ``{}``."""
        if not SKILL_ID.match(skill_id or ""):
            return {}
        folder = self._root / skill_id
        if folder.is_symlink():
            return {}
        return _read_manifest(folder)

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
            source=meta.get("source", ""),
        )


def _validate(name: str, description: str, body: str, skill_id: str | None, origin: str) -> dict:
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
    return {"id": identifier, "name": cleaned_name, "description": cleaned_description,
            "body": cleaned_body}


def _frontmatter(meta: dict, origin: str, source: str) -> str:
    lines = ["---", f"name: {meta['name']}", f"description: {meta['description']}", f"origin: {origin}"]
    if source:
        lines.append(f"source: {source}")
    return "\n".join(lines) + "\n---\n\n"


def _read_manifest(folder: Path) -> dict:
    file = folder / SOURCE_FILE
    if file.is_symlink() or not file.is_file():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _manifest_files(folder: Path) -> list[str]:
    """Resource paths an install recorded, re-validated (the sidecar is a file)."""
    found = _read_manifest(folder).get("files")
    if not isinstance(found, list):
        return []
    return [clean for clean in (safe_resource_path(item) for item in found) if clean]


def _prune_empty(directory: Path, stop: Path) -> None:
    while directory != stop and stop in directory.parents:
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def _write_new(base: Path, relative: str, text: str) -> None:
    """Create ``base/relative`` exclusively; refuse anything that lands outside ``base``."""
    target = base.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    real_base = os.path.realpath(base)
    real_parent = os.path.realpath(target.parent)
    if os.path.commonpath([real_base, real_parent]) != real_base:
        raise ValueError(f"resource {relative!r} escapes the skill folder")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    _private_file(target)


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
