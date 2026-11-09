"""Install skills from a GitHub link: preview what a link holds, then install.

Accepted links (``parse_link``):

* ``https://github.com/<owner>/<repo>``                    - the whole repo, default branch
* ``https://github.com/<owner>/<repo>/tree/<ref>[/<path>]`` - one folder
* ``https://github.com/<owner>/<repo>/blob/<ref>/<path>/SKILL.md`` - one skill
* ``https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>/SKILL.md``

Flow: the link is resolved to a commit SHA (``/repos/{o}/{r}/commits/{ref}``),
the commit's tree is read once (``/git/trees/{sha}?recursive=1``), every
``SKILL.md`` under the linked path is a skill, and its folder's plain-text
files (``.md``/``.txt``) are its resources. SKILL.md and resources are read
from ``raw.githubusercontent.com`` (no API quota), or through the contents API
when a GitHub token is available (so private repos work).

Security (each has a test in ``tests/test_skills_github.py``):

* Only ``api.github.com``, ``raw.githubusercontent.com`` and ``github.com`` over
  https are ever contacted; redirects are followed by hand and a hop to any
  other host or scheme is refused. The bases are module constants; tests
  inject a fake on 127.0.0.1 through ``GitHubFetcher(_bases=...)``, which
  nothing in production passes.
* Caps: ``MAX_BODY`` per file, ``MAX_SKILLS`` per import, ``MAX_TOTAL_BYTES``
  per import, ``MAX_RESOURCES`` per skill, ``MAX_TREE_BYTES`` for the tree, and
  a timeout per request.
* Nothing executes. Only SKILL.md and ``.md``/``.txt`` files inside the skill's
  folder are written; scripts, binaries, executables, symlinks and submodules
  are listed as skipped. Paths are validated (``safe_resource_path``) and
  written by ``SkillsStore.install`` into a folder it creates itself.
* An id that is already installed is reported (``exists``), never overwritten.
* The token goes only to the API host, never into a message, log or file.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote, unquote, urlsplit

from ..sessions.session import slugify
from .invoke import MAX_SKILL_CHARS  # a turn injects only this much of a skill
from .store import (
    MAX_BODY,
    MAX_DESCRIPTION,
    MAX_NAME,
    SKILL_ID,
    SkillsStore,
    safe_resource_path,
)

API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
WEB_BASE = "https://github.com"
WEB_HOSTS = ("github.com", "www.github.com")
RAW_HOST = "raw.githubusercontent.com"

MAX_SKILLS = 50
MAX_RESOURCES = 20
MAX_TOTAL_BYTES = 5_000_000
MAX_TREE_BYTES = 12_000_000
MAX_SKIPPED_LISTED = 40
MAX_REDIRECTS = 3
TIMEOUT = 12.0

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SEGMENT = re.compile(r"^[^\x00-\x1f\\]{1,255}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_USER_AGENT = "imurun-skill-import"


class GitHubError(Exception):
    """A failure the API reports as ``{error, code}`` with HTTP ``status``."""

    def __init__(self, message: str, status: int = 400, code: str = "bad_link") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------- link parsing


@dataclass(frozen=True)
class Link:
    owner: str
    repo: str
    kind: str                 # "repo" | "tree" | "blob" | "raw"
    rest: tuple[str, ...]     # ref + path segments after /tree/, /blob/ or the raw repo


def _segments(path: str) -> list[str]:
    parts = []
    for raw in path.split("/"):
        if raw == "":
            continue
        part = unquote(raw)
        if part in (".", "..") or "/" in part or not _SEGMENT.fullmatch(part):
            raise GitHubError("The link has an unsafe path segment.")
        parts.append(part)
    return parts


def parse_link(url: str) -> Link:
    """Parse a pasted link. Raises ``GitHubError`` for anything not accepted."""
    text = str(url or "").strip()
    if not text:
        raise GitHubError("Paste a GitHub link.")
    if len(text) > 2000:
        raise GitHubError("That link is too long.")
    if re.match(r"^(?:www\.)?(?:github\.com|raw\.githubusercontent\.com)/", text, re.IGNORECASE):
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        raise GitHubError("That is not a valid link.") from None
    if parts.scheme.lower() != "https":
        raise GitHubError("Only https GitHub links are accepted.")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise GitHubError("Only plain github.com links are accepted.")
    host = (parts.hostname or "").lower()
    segments = _segments(parts.path)
    if host in WEB_HOSTS:
        if len(segments) < 2:
            raise GitHubError("Link a repository: https://github.com/<owner>/<repo>.")
        owner, repo = segments[0], segments[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        _check_repo(owner, repo)
        if len(segments) == 2:
            return Link(owner, repo, "repo", ())
        kind = segments[2]
        rest = tuple(segments[3:])
        if kind == "tree":
            if not rest:
                raise GitHubError("The link names no branch after /tree/.")
            return Link(owner, repo, "tree", rest)
        if kind == "blob":
            if len(rest) < 2 or rest[-1] != "SKILL.md":
                raise GitHubError("A file link must point at a SKILL.md.")
            return Link(owner, repo, "blob", rest)
        raise GitHubError("Link a repository, a folder (/tree/...) or a SKILL.md (/blob/...).")
    if host == RAW_HOST:
        if len(segments) < 4 or segments[-1] != "SKILL.md":
            raise GitHubError("A raw link must point at a SKILL.md.")
        owner, repo = segments[0], segments[1]
        _check_repo(owner, repo)
        rest = tuple(segments[2:])
        if rest[:2] in (("refs", "heads"), ("refs", "tags")) and len(rest) > 3:
            rest = rest[2:]
        return Link(owner, repo, "raw", rest)
    raise GitHubError("Only github.com and raw.githubusercontent.com links are accepted.")


def _check_repo(owner: str, repo: str) -> None:
    if not _OWNER.fullmatch(owner) or not _REPO.fullmatch(repo) or repo in (".", ".."):
        raise GitHubError("The link does not name a valid owner/repository.")


# ---------------------------------------------------------------- HTTP


@dataclass(frozen=True)
class Bases:
    api: str = API_BASE
    raw: str = RAW_BASE
    web: str = WEB_BASE


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.netloc or "").lower()


class GitHubFetcher:
    """GET against the allow-listed GitHub hosts only, size- and time-capped.

    ``_bases`` exists for tests (a fake GitHub on 127.0.0.1); production code
    never passes it, so the allow-list is exactly the three https hosts.
    """

    def __init__(
        self,
        token_provider: Callable[[], str | None] | None = None,
        *,
        client=None,
        timeout: float = TIMEOUT,
        _bases: Bases | None = None,
    ) -> None:
        self.bases = _bases or Bases()
        self._allowed = {_origin(self.bases.api), _origin(self.bases.raw), _origin(self.bases.web)}
        self._token_provider = token_provider
        self._client = client
        self._timeout = timeout
        self.calls = 0  # API calls made (for tests and for the rate-limit note)

    def allowed(self, url: str) -> bool:
        return _origin(url) in self._allowed

    def token(self) -> str | None:
        if self._token_provider is None:
            return None
        try:
            value = self._token_provider()
        except Exception:  # noqa: BLE001 - a broken connector means anonymous
            return None
        return value or None

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(follow_redirects=False, trust_env=False,
                                        headers={"User-Agent": _USER_AGENT})
        return self._client

    def get(self, url: str, *, accept: str = "application/vnd.github+json",
            max_bytes: int = MAX_BODY * 4, auth: bool = True) -> tuple[int, dict, bytes]:
        """``(status, headers, body)``; refuses any URL or redirect off the allow-list."""
        import httpx

        token = self.token() if auth else None
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            if not self.allowed(current):
                raise GitHubError(f"Refused to contact {urlsplit(current).netloc or current}: "
                                  "only GitHub hosts over https are allowed.", 502, "host_refused")
            headers = {"Accept": accept, "User-Agent": _USER_AGENT}
            if token and _origin(current) == _origin(self.bases.api):
                headers["Authorization"] = f"Bearer {token}"
                headers["X-GitHub-Api-Version"] = "2022-11-28"
            if _origin(current) == _origin(self.bases.api):
                self.calls += 1
            try:
                with self._http().stream(
                    "GET", current, headers=headers,
                    timeout=httpx.Timeout(self._timeout, connect=min(self._timeout, 6.0)),
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location", "")
                        if not location:
                            raise GitHubError("GitHub sent a redirect without a target.", 502, "upstream")
                        current = str(httpx.URL(current).join(location))
                        continue
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise GitHubError(f"A file from GitHub is larger than {max_bytes:,} bytes.",
                                              413, "too_large")
                        chunks.append(chunk)
                    return (response.status_code,
                            {k.lower(): v for k, v in response.headers.items()}, b"".join(chunks))
            except httpx.TimeoutException:
                raise GitHubError("GitHub did not answer in time.", 504, "timeout") from None
            except httpx.HTTPError as exc:
                raise GitHubError(f"Could not reach GitHub ({type(exc).__name__}).", 502, "unreachable") from None
        raise GitHubError("GitHub redirected too many times.", 502, "upstream")

    def api_json(self, path: str, *, max_bytes: int = 2_000_000, missing_ok: bool = False):
        status, headers, body = self.get(self.bases.api + path, max_bytes=max_bytes)
        if status == 200:
            try:
                return json.loads(body.decode("utf-8"))
            except ValueError:
                raise GitHubError("GitHub sent an unreadable answer.", 502, "upstream") from None
        if missing_ok and status in (404, 422):
            return None
        raise self._failure(status, headers, body)

    def api_text(self, path: str, accept: str, *, missing_ok: bool = False) -> str | None:
        status, headers, body = self.get(self.bases.api + path, accept=accept, max_bytes=4096)
        if status == 200:
            return body.decode("utf-8", "replace").strip()
        if missing_ok and status in (404, 422):
            return None
        raise self._failure(status, headers, body)

    def _failure(self, status: int, headers: dict, body: bytes) -> GitHubError:
        text = body[:400].decode("utf-8", "replace").lower()
        if status in (403, 429) and (headers.get("x-ratelimit-remaining") == "0" or "rate limit" in text):
            reset = headers.get("x-ratelimit-reset", "")
            when = ""
            try:
                when = " It resets at " + time.strftime("%H:%M UTC", time.gmtime(int(reset))) + "."
            except (TypeError, ValueError):
                pass
            hint = "" if self.token() else (" Without sign-in GitHub allows 60 requests an hour;"
                                            " connecting GitHub in Connectors raises that.")
            return GitHubError("GitHub's rate limit was reached." + when + hint, 429, "rate_limited")
        if status == 401:
            return GitHubError("GitHub refused the connected account's token.", 502, "auth")
        if status == 404:
            return GitHubError("Not found on GitHub (a private repository needs the GitHub connector).",
                               404, "not_found")
        return GitHubError(f"GitHub answered HTTP {status}.", 502, "upstream")


# ---------------------------------------------------------------- frontmatter


def parse_skill_file(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into ``(frontmatter fields, body)``.

    A small YAML subset: ``key: value`` lines, quoted values, and ``>``/``|``
    block scalars (folded to one line; frontmatter we write is one line per key).
    """
    if not text.startswith("---"):
        return {}, text.strip()
    match = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        return {}, text.strip()
    meta: dict[str, str] = {}
    lines = match.group(1).splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#") or line[:1] in (" ", "\t"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if value[:1] in (">", "|"):
            block = []
            while index < len(lines) and (not lines[index].strip() or lines[index][:1] in (" ", "\t")):
                block.append(lines[index].strip())
                index += 1
            value = " ".join(part for part in block if part)
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key] = value
    return meta, text[match.end():].strip()


def _field(value: str) -> str:
    """A frontmatter-safe single line: no newlines, no ``---`` fence."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value or "")
    cleaned = re.sub(r"-{3,}", "-", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


# ---------------------------------------------------------------- discovery


@dataclass
class Found:
    path: str                   # the skill folder, repo-relative ("" for the root)
    file_size: int
    resources: list[dict] = field(default_factory=list)   # {path, size, repo_path}
    skipped: list[dict] = field(default_factory=list)     # {path, reason}
    skipped_more: int = 0


@dataclass
class Resolved:
    link: Link
    ref: str
    commit: str
    base: str                   # linked folder, repo-relative
    found: list[Found]
    total_found: int
    notes: list[str]


def _join(*parts: str) -> str:
    return "/".join(part for part in parts if part)


class SkillImporter:
    def __init__(self, store: SkillsStore, fetcher: GitHubFetcher | None = None) -> None:
        self.store = store
        self.fetcher = fetcher or GitHubFetcher()

    # ------------------------------------------------------------ resolving

    def _commit_for(self, owner: str, repo: str, ref: str) -> str | None:
        sha = self.fetcher.api_text(f"/repos/{owner}/{repo}/commits/{quote(ref, safe='')}",
                                    "application/vnd.github.sha", missing_ok=True)
        if sha is None:
            return None
        if not _SHA.fullmatch(sha):
            raise GitHubError("GitHub sent an unexpected commit id.", 502, "upstream")
        return sha

    def resolve(self, url: str, *, commit: str | None = None) -> Resolved:
        link = parse_link(url)
        owner, repo = link.owner, link.repo
        notes: list[str] = []
        if commit is not None and not _SHA.fullmatch(commit):
            raise GitHubError("ref must be the 40-character commit id the preview returned.")
        if link.kind == "repo":
            info = self.fetcher.api_json(f"/repos/{owner}/{repo}")
            ref = str((info or {}).get("default_branch") or "")
            if not ref:
                raise GitHubError("The repository has no default branch.", 404, "not_found")
            sha = commit or self._commit_for(owner, repo, ref)
            path_parts: list[str] = []
        else:
            rest = list(link.rest)
            keep_file = link.kind in ("blob", "raw")
            limit = len(rest) - (1 if keep_file else 0)
            sha = None
            ref = ""
            path_parts = []
            # A branch name may contain "/": try the shortest prefix first.
            for cut in range(1, min(limit, 6) + 1):
                candidate = "/".join(rest[:cut])
                found_sha = self._commit_for(owner, repo, candidate)
                if found_sha:
                    ref, sha, path_parts = candidate, found_sha, rest[cut:]
                    break
            if not sha:
                raise GitHubError("The branch, tag or commit in the link was not found.", 404, "not_found")
            if commit:
                sha = commit
        if not sha:
            raise GitHubError("The branch was not found.", 404, "not_found")
        single = link.kind in ("blob", "raw")
        base = "/".join(path_parts[:-1] if single else path_parts)
        tree = self.fetcher.api_json(f"/repos/{owner}/{repo}/git/trees/{sha}?recursive=1",
                                     max_bytes=MAX_TREE_BYTES)
        if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
            raise GitHubError("GitHub sent an unreadable tree.", 502, "upstream")
        if tree.get("truncated"):
            notes.append("The repository is too large for GitHub to list in full; "
                         "some skills may be missing. Link the folder instead.")
        entries = {str(e.get("path")): e for e in tree["tree"] if isinstance(e, dict) and e.get("path")}
        if base and (entries.get(base) or {}).get("type") != "tree":
            raise GitHubError(f"The folder {base!r} is not in this commit.", 404, "not_found")
        found, total = self._discover(entries, base, single)
        if not found:
            raise GitHubError("No SKILL.md was found at this link.", 404, "no_skills")
        if total > MAX_SKILLS:
            notes.append(f"This link holds {total} skills; only the first {MAX_SKILLS} are listed. "
                         "Link a folder to see the others.")
        return Resolved(link, ref, sha, base, found, total, notes)

    def _discover(self, entries: dict[str, dict], base: str, single: bool) -> tuple[list[Found], int]:
        prefix = base + "/" if base else ""

        def is_skill_file(path: str, entry: dict) -> bool:
            return (entry.get("type") == "blob" and str(entry.get("mode")) in ("100644", "100755")
                    and path.split("/")[-1] == "SKILL.md")

        if single:
            path = _join(base, "SKILL.md")
            entry = entries.get(path)
            if not entry or not is_skill_file(path, entry):
                raise GitHubError("That SKILL.md is not in this commit (or is a link).", 404, "not_found")
            dirs = [base]
        else:
            dirs = sorted(path.rsplit("/", 1)[0] if "/" in path else ""
                          for path, entry in entries.items()
                          if (path.startswith(prefix) or not base) and is_skill_file(path, entry))
        total = len(dirs)
        dirs = dirs[:MAX_SKILLS]
        all_dirs = set(path.rsplit("/", 1)[0] if "/" in path else ""
                       for path, entry in entries.items() if is_skill_file(path, entry))
        found: list[Found] = []
        for folder in dirs:
            skill_file = entries[_join(folder, "SKILL.md")]
            item = Found(path=folder, file_size=int(skill_file.get("size") or 0))
            inner = folder + "/" if folder else ""
            nested = [d for d in all_dirs if d != folder and (d.startswith(inner) or not folder)]
            for path in sorted(entries):
                entry = entries[path]
                if folder and not path.startswith(inner):
                    continue
                if path == _join(folder, "SKILL.md") or entry.get("type") == "tree":
                    continue
                if any(path.startswith(d + "/") for d in nested if d):
                    continue  # belongs to a nested skill
                relative = path[len(inner):]
                reason = self._skip_reason(relative, entry, len(item.resources))
                if reason:
                    if len(item.skipped) < MAX_SKIPPED_LISTED:
                        item.skipped.append({"path": relative, "reason": reason})
                    else:
                        item.skipped_more += 1
                    continue
                item.resources.append({"path": relative, "size": int(entry.get("size") or 0),
                                       "repo_path": path})
            found.append(item)
        return found, total

    @staticmethod
    def _skip_reason(relative: str, entry: dict, kept: int) -> str:
        kind, mode = entry.get("type"), str(entry.get("mode"))
        if kind == "commit":
            return "submodule"
        if mode == "120000":
            return "symbolic link"
        if not relative.lower().endswith((".md", ".txt")):
            return "not plain text (only .md and .txt are imported)"
        if mode == "100755":
            return "executable file"
        if safe_resource_path(relative) is None:
            return "unsafe or too deep a path"
        if int(entry.get("size") or 0) > MAX_BODY:
            return f"larger than {MAX_BODY:,} bytes"
        if kept >= MAX_RESOURCES:
            return f"over the {MAX_RESOURCES}-file limit per skill"
        return ""

    # ------------------------------------------------------------ fetching

    def _fetch_file(self, resolved: Resolved, repo_path: str) -> str:
        owner, repo = resolved.link.owner, resolved.link.repo
        encoded = "/".join(quote(part, safe="") for part in repo_path.split("/"))
        if self.fetcher.token():
            status, headers, body = self.fetcher.get(
                f"{self.fetcher.bases.api}/repos/{owner}/{repo}/contents/{encoded}?ref={resolved.commit}",
                accept="application/vnd.github.raw", max_bytes=MAX_BODY * 4)
        else:
            status, headers, body = self.fetcher.get(
                f"{self.fetcher.bases.raw}/{owner}/{repo}/{resolved.commit}/{encoded}",
                accept="text/plain", max_bytes=MAX_BODY * 4, auth=False)
        if status != 200:
            raise self.fetcher._failure(status, headers, body)
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            raise GitHubError(f"{repo_path} is not UTF-8 text.", 422, "not_text") from None

    def _describe(self, resolved: Resolved, item: Found, text: str, seen: set[str]) -> dict:
        meta, body = parse_skill_file(text)
        owner, repo = resolved.link.owner, resolved.link.repo
        folder_name = item.path.rsplit("/", 1)[-1] if item.path else repo
        raw_name = _field(meta.get("name", "")) or folder_name
        candidate = raw_name.lower()
        skill_id = candidate if SKILL_ID.fullmatch(candidate) and len(candidate) <= 60 else ""
        if not skill_id:
            skill_id = slugify(folder_name, max_words=8, max_len=60)
            if not SKILL_ID.fullmatch(skill_id) or skill_id == "session":
                skill_id = slugify(raw_name, max_words=8, max_len=60)
        description = _field(meta.get("description", ""))
        warnings: list[str] = []
        status = "ok"
        if not description:
            description = f"Imported from {owner}/{repo}"
            warnings.append("No description in its frontmatter; a placeholder is used.")
        if len(raw_name) > MAX_NAME:
            warnings.append(f"Name shortened to {MAX_NAME} characters.")
        if len(description) > MAX_DESCRIPTION:
            warnings.append(f"Description shortened to {MAX_DESCRIPTION} characters.")
        blocker = ""
        if not body:
            status = "invalid"
            blocker = "SKILL.md has no instructions."
            warnings.append(blocker)
        elif len(body) > MAX_BODY:
            status = "too_large"
            blocker = f"Instructions exceed {MAX_BODY:,} characters; it cannot be installed."
            warnings.append(blocker)
        elif len(body) > MAX_SKILL_CHARS:
            warnings.append(f"Only the first {MAX_SKILL_CHARS:,} characters reach the model in a turn.")
        if item.resources:
            warnings.append("Resource files are stored with the skill; the model reads only SKILL.md.")
        if status == "ok" and skill_id in seen:
            status = "duplicate"
            warnings.append(f"Another skill in this link is also /{skill_id}.")
        elif status == "ok" and self.store.exists(skill_id):
            status = "exists"
            warnings.append(f"/{skill_id} is already installed; it will not be overwritten.")
        seen.add(skill_id)
        return {
            "path": item.path, "id": skill_id, "invoke": f"/{skill_id}",
            "name": raw_name[:MAX_NAME], "description": description[:MAX_DESCRIPTION],
            "size": len(body.encode("utf-8")), "status": status,
            "installable": status == "ok",
            "files": [{"path": r["path"], "size": r["size"]} for r in item.resources],
            "skipped": item.skipped, "skipped_more": item.skipped_more,
            "warnings": warnings, "_body": body, "_blocker": blocker,
        }

    def _scan(self, resolved: Resolved, only: set[str] | None = None) -> list[dict]:
        seen: set[str] = set()
        total = 0
        out: list[dict] = []
        for item in resolved.found:
            if only is not None and item.path not in only:
                continue
            entry_path = _join(item.path, "SKILL.md")
            if item.file_size > MAX_BODY * 4:
                out.append(self._failed(item, f"SKILL.md is larger than {MAX_BODY * 4:,} bytes.", "too_large"))
                continue
            if total + item.file_size > MAX_TOTAL_BYTES:
                out.append(self._failed(item, f"Over the {MAX_TOTAL_BYTES:,}-byte limit for one import.",
                                        "too_large"))
                continue
            try:
                text = self._fetch_file(resolved, entry_path)
            except GitHubError as exc:
                if exc.code in ("rate_limited", "host_refused", "timeout", "unreachable"):
                    raise
                out.append(self._failed(item, str(exc), "failed"))
                continue
            total += len(text.encode("utf-8"))
            out.append(self._describe(resolved, item, text, seen))
        return out

    @staticmethod
    def _failed(item: Found, why: str, status: str) -> dict:
        return {"path": item.path, "id": "", "invoke": "", "name": item.path or "(root)",
                "description": "", "size": item.file_size, "status": status, "installable": False,
                "files": [], "skipped": item.skipped, "skipped_more": item.skipped_more,
                "warnings": [why], "_body": "", "_blocker": why}

    # ------------------------------------------------------------ public

    def preview(self, url: str) -> dict:
        resolved = self.resolve(url)
        skills = self._scan(resolved)
        for skill in skills:
            skill.pop("_body", None)
            skill.pop("_blocker", None)
        return {
            "owner": resolved.link.owner, "repo": resolved.link.repo, "ref": resolved.ref,
            "commit": resolved.commit, "path": resolved.base,
            "repo_url": f"{WEB_BASE}/{resolved.link.owner}/{resolved.link.repo}",
            "skills": skills, "total": resolved.total_found,
            "truncated": resolved.total_found > len(resolved.found), "notes": resolved.notes,
            "authenticated": bool(self.fetcher.token()),
        }

    def install(self, url: str, paths: list, commit: str | None = None) -> dict:
        if not isinstance(paths, list) or not paths:
            raise GitHubError("Choose at least one skill to install.")
        if len(paths) > MAX_SKILLS:
            raise GitHubError(f"At most {MAX_SKILLS} skills can be installed at once.")
        wanted: list[str] = []
        for path in paths:
            if not isinstance(path, str) or len(path) > 400:
                raise GitHubError("Each path must be a string.")
            clean = path.strip().strip("/")
            if "\\" in clean or "\x00" in clean or (
                    clean and any(part in ("", ".", "..") for part in clean.split("/"))):
                raise GitHubError(f"Unsafe path: {path!r}.")
            if clean not in wanted:
                wanted.append(clean)
        resolved = self.resolve(url, commit=commit)
        known = {item.path for item in resolved.found}
        results: list[dict] = []
        for path in wanted:
            if path not in known:
                results.append({"path": path, "id": "", "status": "failed",
                                "error": "Not a skill at this link."})
        described = {d["path"]: d for d in self._scan(resolved, set(wanted) & known)}
        by_path = {item.path: item for item in resolved.found}
        total = sum(d["size"] for d in described.values())
        for path in [p for p in wanted if p in described]:
            info = described[path]
            result = {"path": path, "id": info["id"], "name": info["name"]}
            if info["status"] in ("exists", "duplicate"):
                results.append({**result, "status": "exists", "error": info["warnings"][-1]})
                continue
            if not info["installable"]:
                results.append({**result, "status": "failed", "error": info["_blocker"]})
                continue
            try:
                files, total = self._fetch_resources(resolved, by_path[path], total)
                skill = self._write(resolved, path, info, files, replace=False)
            except FileExistsError:
                results.append({**result, "status": "exists",
                                "error": f"/{info['id']} is already installed."})
                continue
            except GitHubError as exc:
                if exc.code in ("rate_limited", "host_refused"):
                    results.append({**result, "status": "failed", "error": str(exc)})
                    break
                results.append({**result, "status": "failed", "error": str(exc)})
                continue
            except (ValueError, OSError) as exc:
                results.append({**result, "status": "failed", "error": str(exc)})
                continue
            results.append({**result, "status": "installed", "source": skill.source,
                            "files": sorted(files)})
        return {"owner": resolved.link.owner, "repo": resolved.link.repo, "commit": resolved.commit,
                "results": results}

    def _fetch_resources(self, resolved: Resolved, item: Found, total: int) -> tuple[dict[str, str], int]:
        files: dict[str, str] = {}
        for resource in item.resources:
            if total + resource["size"] > MAX_TOTAL_BYTES:
                raise GitHubError(f"Over the {MAX_TOTAL_BYTES:,}-byte limit for one import.", 413, "too_large")
            try:
                text = self._fetch_file(resolved, resource["repo_path"])
            except GitHubError as exc:
                if exc.code == "not_text":
                    continue  # a .txt that is not UTF-8 is left out
                raise
            if len(text) > MAX_BODY:
                continue
            total += len(text.encode("utf-8"))
            files[resource["path"]] = text
        return files, total

    def _write(self, resolved: Resolved, path: str, info: dict, files: dict[str, str], *, replace: bool,
               skill_id: str | None = None):
        owner, repo = resolved.link.owner, resolved.link.repo
        source = f"{owner}/{repo}@{resolved.commit}:{path}"
        manifest = {"provider": "github", "owner": owner, "repo": repo, "ref": resolved.ref,
                    "commit": resolved.commit, "path": path,
                    "url": f"{WEB_BASE}/{owner}/{repo}/tree/{resolved.commit}/{path}".rstrip("/"),
                    "installed_at": int(time.time())}
        return self.store.install(info["name"], info["description"], info["_body"],
                                  skill_id=skill_id or info["id"], origin="github", source=source,
                                  files=files, manifest=manifest, replace=replace)

    def update(self, skill_id: str) -> dict:
        """Re-install a GitHub skill from the newest commit of the ref it came from."""
        manifest = self.store.source_manifest(skill_id)
        if manifest.get("provider") != "github":
            raise GitHubError("This skill was not installed from GitHub.", 400, "not_github")
        owner, repo = str(manifest.get("owner", "")), str(manifest.get("repo", ""))
        ref, path = str(manifest.get("ref", "")), str(manifest.get("path", ""))
        _check_repo(owner, repo)
        if not ref or ".." in ref or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", ref):
            raise GitHubError("The recorded branch is not valid.", 400, "bad_source")
        if path and (any(p in ("", ".", "..") for p in path.split("/")) or "\\" in path):
            raise GitHubError("The recorded path is not valid.", 400, "bad_source")
        url = f"{WEB_BASE}/{owner}/{repo}/tree/{ref}/{path}".rstrip("/")
        if path:
            url = f"{WEB_BASE}/{owner}/{repo}/blob/{ref}/{path}/SKILL.md"
        resolved = self.resolve(url)
        if resolved.commit == manifest.get("commit"):
            return {"id": skill_id, "status": "current", "commit": resolved.commit}
        if not resolved.found:
            raise GitHubError("The skill is no longer at its source.", 404, "not_found")
        item = resolved.found[0]
        described = self._scan(resolved, {item.path})
        info = described[0] if described else None
        if not info or info["status"] not in ("ok", "exists"):
            raise GitHubError("; ".join((info or {}).get("warnings") or ["The new version is not valid."]),
                              422, "invalid")
        files, _ = self._fetch_resources(resolved, item, 0)
        skill = self._write(resolved, item.path, info, files, replace=True, skill_id=skill_id)
        return {"id": skill.id, "status": "updated", "commit": resolved.commit, "source": skill.source}


def source_view(source: str) -> dict:
    """``owner/repo@commit:path`` -> ``{repo, commit, path, url}`` for the API, or ``{}``."""
    match = re.fullmatch(r"([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})@([0-9a-f]{40}):(.*)", source or "")
    if not match:
        return {}
    owner, repo, commit, path = match.groups()
    url = f"{WEB_BASE}/{owner}/{repo}"
    if path:
        url += f"/tree/{commit}/" + "/".join(quote(p, safe="") for p in path.split("/"))
    return {"repo": f"{owner}/{repo}", "commit": commit, "path": path, "url": url}
