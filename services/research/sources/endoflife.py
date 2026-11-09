"""endoflife.date (keyless JSON): release cycles of ~400 products.

For "latest Python release", "is Ubuntu 20.04 still supported", "Node LTS
version" a search engine is a detour: ``https://endoflife.date/api/<product>.json``
lists every cycle with its release date, latest patch version and its date,
LTS flag, end of support and end of life. The product list
(``/api/all.json``) is downloaded once a day; a product is matched when the
question names it. Measured 2026-11-09: 200 JSON in 0.5-2.6 s; Python 3.15
released 2026-10-09, latest 3.15.2 on 2026-10-25.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

from ..routing import plan_text
from ..types import Hit, Plan
from . import ApiSource, SourceError

API = "https://endoflife.date/api"
LIST_TTL = 86400.0
CYCLES = 4
MAX_PRODUCTS = 2
ALIASES = {"node": "nodejs", "node.js": "nodejs", "postgres": "postgresql", "k8s": "kubernetes",
           ".net": "dotnet", "golang": "go", "go language": "go", "ror": "rails", "ruby on rails": "rails",
           "windows 11": "windows", "windows 10": "windows", "mac os": "macos", "os x": "macos",
           "rhel": "rhel", "red hat enterprise linux": "rhel", "java": "oracle-jdk", "jdk": "oracle-jdk"}
_WORD = re.compile(r"[a-z0-9.+#]+")


def match_products(text: str, products: list[str]) -> list[str]:
    lowered = " " + " ".join(_WORD.findall((text or "").lower())) + " "
    found: list[str] = []
    for alias, slug in ALIASES.items():
        if f" {alias} " in lowered and slug in products and slug not in found:
            found.append(slug)
    words = set(lowered.split())
    for slug in products:
        if slug in found or len(slug) <= 2:
            continue  # "go", "qt": too ambiguous without an alias
        parts = slug.split("-")
        if all(p in words for p in parts):
            found.append(slug)
    return found[:MAX_PRODUCTS]


def describe(slug: str, cycles: Any) -> Hit | None:
    if not isinstance(cycles, list) or not cycles:
        return None
    lines = []
    first_date = ""
    for cycle in cycles[:CYCLES]:
        if not isinstance(cycle, dict):
            continue
        parts = [f"{slug} {cycle.get('cycle')}"]
        if cycle.get("releaseDate"):
            parts.append(f"released {cycle['releaseDate']}")
        if cycle.get("latest"):
            parts.append(f"latest version {cycle['latest']}"
                         + (f" ({cycle['latestReleaseDate']})" if cycle.get("latestReleaseDate") else ""))
        if cycle.get("lts") not in (None, False):
            parts.append("LTS")
        for key, label in (("support", "active support until"), ("eol", "end of life")):
            value = cycle.get(key)
            if isinstance(value, str):
                parts.append(f"{label} {value}")
            elif value is True:
                parts.append(f"{label}: reached")
        lines.append(", ".join(parts))
        first_date = first_date or str(cycle.get("latestReleaseDate") or cycle.get("releaseDate") or "")
    if not lines:
        return None
    text = f"Release cycles of {slug} (newest first, endoflife.date):\n" + "\n".join(lines)
    return Hit(title=f"{slug} release cycles - endoflife.date", url=f"https://endoflife.date/{slug}",
               snippet=lines[0][:300], published=first_date, source="endoflife", text=text, fetchable=False)


class EndOfLifeSource(ApiSource):
    name = "endoflife"
    per_plan = True
    topics = frozenset({"release"})
    timeout_cap = 5.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._products: tuple[float, list[str]] | None = None
        self._lock = threading.Lock()

    def products(self, fetcher: Any, timeout: float) -> list[str]:
        with self._lock:
            if self._products and time.monotonic() - self._products[0] < LIST_TTL:
                return self._products[1]
        data = self.get_json(fetcher, f"{API}/all.json", timeout)
        if not isinstance(data, list):
            raise SourceError("endoflife: unexpected product list")
        names = [str(p) for p in data if isinstance(p, str)]
        with self._lock:
            self._products = (time.monotonic(), names)
        return names

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        slugs = match_products(plan_text(plan), self.products(fetcher, timeout))
        hits: list[Hit] = []
        for slug in slugs:
            hit = describe(slug, self.get_json(fetcher, f"{API}/{slug}.json", timeout))
            if hit is not None:
                hit.query = query
                hit.rank = len(hits)
                hits.append(hit)
        return hits
