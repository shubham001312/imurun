"""Direct lookups at authoritative sites the plan points to.

Search engines are a detour when the question names the place that holds
the answer. This source runs once per plan and produces hits for:

1. **addresses in the conversation** - a URL or a bare domain
   ("check python.org") in the user's message or the planned queries;
2. **package registries** (JSON APIs, no key): PyPI (``pypi.org/pypi/<pkg>/json``),
   npm (``registry.npmjs.org/<pkg>/latest``) and GitHub releases
   (``api.github.com/repos/<owner>/<repo>/releases/latest``, 60 requests/hour
   unauthenticated) - "latest version of requests on pypi",
   "github.com/psf/requests latest release";
3. **a small curated map** of official pages for questions that come up all
   the time (Python / Node.js releases, India's PM, the UK PM). Each entry is
   an official site; it is a starting page that the extractor reads, not an
   answer typed in here.

Registry hits carry clean text already; site hits are fetched and cleaned by
the engine like any other result.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..types import Hit, Plan, domain_of
from . import Source, SourceError

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_DOMAIN = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:com|org|net|io|dev|gov|edu|in|uk|co|ai|app|info|"
    r"de|fr|jp|au|ca|eu|me|tv|news))(/[^\s]*)?\b",
    re.IGNORECASE,
)
_PYPI = re.compile(r"\b(?:pypi|pip(?: package)?|python package)\s+([A-Za-z0-9][\w.\-]{0,60})|"
                   r"\b([A-Za-z0-9][\w.\-]{0,60})\s+(?:on|from|in)\s+pypi\b", re.IGNORECASE)
_NPM = re.compile(r"\bnpm(?: package)?\s+(@?[a-z0-9][\w.\-/]{0,80})|"
                  r"\b(@?[a-z0-9][\w.\-/]{0,80})\s+(?:on|from|in)\s+npm\b", re.IGNORECASE)
_GITHUB = re.compile(r"github\.com/([\w.\-]+)/([\w.\-]+)", re.IGNORECASE)
_SKIP_WORDS = {"package", "the", "latest", "version", "release", "a", "an", "of"}

# (pattern over the plan's queries + entities, url, title). Official sites only.
CURATED: tuple[tuple[str, str, str], ...] = (
    (r"\bpython\b.*\b(?:release|version|download|latest)\b|\b(?:latest|current)\b.*\bpython\b",
     "https://www.python.org/downloads/", "Download Python | Python.org"),
    (r"\bnode(?:\.?js)?\b.*\b(?:release|version|lts|latest)\b",
     "https://nodejs.org/en/about/previous-releases", "Node.js Releases"),
    (r"\b(?:prime minister|pm)\b.*\bindia\b|\bindia\b.*\b(?:prime minister|pm)\b",
     "https://www.pmindia.gov.in/en/", "Prime Minister of India (official site)"),
    (r"\b(?:prime minister|pm)\b.*\b(?:uk|united kingdom|britain)\b|\b(?:uk|british)\b.*\bprime minister\b",
     "https://www.gov.uk/government/ministers/prime-minister", "Prime Minister - GOV.UK"),
)


def _clean_name(name: str) -> str:
    name = (name or "").strip(" .,;:!?'\"").rstrip("/")
    return "" if name.lower() in _SKIP_WORDS else name


class DirectSource(Source):
    name = "direct"
    per_plan = True

    def targets(self, plan: Plan, extra_text: str = "") -> list[tuple[str, str, str]]:
        """(kind, url, title) to look up for this plan."""
        text = " ".join([extra_text, *plan.queries, *plan.entities])
        out: list[tuple[str, str, str]] = []
        seen: set[str] = set()

        def add(kind: str, url: str, title: str) -> None:
            if url not in seen:
                seen.add(url)
                out.append((kind, url, title))

        for match in _URL.finditer(text):
            url = match.group(0).rstrip(".,;:")
            github = _GITHUB.search(url)
            if github:
                add("github", f"https://api.github.com/repos/{github.group(1)}/{github.group(2)}/releases/latest",
                    f"{github.group(1)}/{github.group(2)} latest release")
            add("page", url, url)
        for match in _DOMAIN.finditer(re.sub(_URL, " ", text)):
            host = match.group(1).lower()
            if host.endswith((".js", ".py")) or host.count(".") > 4:
                continue
            path = match.group(2) or "/"
            github = _GITHUB.search(host + path)
            if github:
                add("github", f"https://api.github.com/repos/{github.group(1)}/{github.group(2)}/releases/latest",
                    f"{github.group(1)}/{github.group(2)} latest release")
                continue
            add("page", f"https://{host}{path}", host)
        for match in _PYPI.finditer(text):
            name = _clean_name(match.group(1) or match.group(2))
            if name:
                add("pypi", f"https://pypi.org/pypi/{name}/json", f"{name} on PyPI")
        for match in _NPM.finditer(text):
            name = _clean_name(match.group(1) or match.group(2))
            if name:
                add("npm", f"https://registry.npmjs.org/{name}/latest", f"{name} on npm")
        lowered = text.lower()
        for pattern, url, title in CURATED:
            if re.search(pattern, lowered):
                add("page", url, title)
        return out[:4]

    def wanted(self, plan: Plan) -> bool:
        return bool(self.targets(plan, plan.prompt))

    def _registry(self, kind: str, url: str, title: str, fetcher: Any, timeout: float) -> Hit | None:
        response = fetcher.get(url, headers={"Accept": "application/json"}, timeout=timeout)
        if not response.ok:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        if kind == "pypi":
            info = data.get("info") or {}
            version = str(info.get("version") or "")
            released = ""
            for item in (data.get("urls") or []):
                released = str(item.get("upload_time_iso_8601") or item.get("upload_time") or "")[:10]
                if released:
                    break
            text = (f"{info.get('name') or title}: latest version on PyPI is {version}"
                    + (f", uploaded {released}" if released else "") + ". "
                    + " ".join(str(info.get("summary") or "").split()))
            page = f"https://pypi.org/project/{info.get('name') or title.split()[0]}/"
            return Hit(title=title, url=page, snippet=text[:500], published=released, source="pypi",
                       text=text, fetchable=False)
        if kind == "npm":
            version = str(data.get("version") or "")
            name = str(data.get("name") or title.split()[0])
            text = f"{name}: latest version on npm is {version}. " + " ".join(str(data.get("description") or "").split())
            return Hit(title=title, url=f"https://www.npmjs.com/package/{name}", snippet=text[:500],
                       source="npm", text=text, fetchable=False)
        if kind == "github":
            tag = str(data.get("tag_name") or "")
            published = str(data.get("published_at") or "")[:10]
            body = " ".join(str(data.get("body") or "").split())[:1500]
            text = (f"{title.replace(' latest release', '')}: latest release {data.get('name') or tag} (tag {tag})"
                    + (f", published {published}" if published else "") + ". " + body)
            return Hit(title=title, url=str(data.get("html_url") or url), snippet=text[:500],
                       published=published, source="github", text=text, fetchable=False)
        return None

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        hits: list[Hit] = []
        targets = self.targets(plan, plan.prompt)
        for index, (kind, url, title) in enumerate(targets):
            if kind == "page":
                hits.append(Hit(title=title, url=url, snippet="", source="direct", rank=index,
                                query=plan.queries[0] if plan.queries else query))
                continue
            try:
                hit = self._registry(kind, url, title, fetcher, timeout)
            except (ValueError, json.JSONDecodeError, AttributeError):
                hit = None
            if hit is not None:
                hit.rank = index
                hit.query = query
                hits.append(hit)
        if not hits and targets:
            raise SourceError("direct: the named sites did not answer")
        for hit in hits:
            if not hit.site:
                hit.site = domain_of(hit.url)
        return hits
