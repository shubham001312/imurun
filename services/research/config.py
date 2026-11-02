"""Research engine settings (environment variables, read at call time).

Every number here is a budget: the research runs *before* the model can
start answering, so each step has a deadline and the whole run has one.
Values are read lazily so a test (or the Settings screen) can change them
without re-importing the package.
"""
from __future__ import annotations

import os


def _float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(os.getenv(name, "") or default))
    except ValueError:
        value = default
    return max(low, min(high, value))


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def deadline() -> float:
    """Wall-clock budget of one research run, seconds (default 12)."""
    return _float("IMURUN_RESEARCH_DEADLINE", 12.0, 2.0, 60.0)


def search_share() -> float:
    """Part of the deadline the search phase may use before fetching starts."""
    return 0.5


def search_timeout() -> float:
    """Per-request timeout of one search source, seconds."""
    return _float("IMURUN_RESEARCH_SEARCH_TIMEOUT", 6.0, 1.0, 20.0)


def fetch_timeout() -> float:
    """Per-page timeout, seconds."""
    return _float("IMURUN_RESEARCH_FETCH_TIMEOUT", 8.0, 1.0, 30.0)


def max_pages() -> int:
    """Result pages fetched and cleaned per run."""
    return _int("IMURUN_RESEARCH_PAGES", 4, 0, 10)


def max_page_bytes() -> int:
    return 2 * 1024 * 1024


def budget_tokens() -> int:
    """Token budget of the Context block (estimated as characters / 3.5)."""
    return _int("IMURUN_RESEARCH_TOKENS", 900, 200, 6000)


def planner_enabled() -> bool:
    return _flag("IMURUN_RESEARCH_PLANNER", True)


def planner_timeout() -> float:
    """The routing generation is cut off after this many seconds."""
    return _float("IMURUN_RESEARCH_PLANNER_TIMEOUT", 8.0, 1.0, 60.0)


def planner_max_tokens() -> int:
    return _int("IMURUN_RESEARCH_PLANNER_TOKENS", 60, 16, 200)


def browser_search_enabled() -> bool:
    """Search through the embedded browser helper. Off unless asked for (see
    docs/research/BROWSER-SEARCH.md for why)."""
    return _flag("IMURUN_BROWSER_SEARCH", False)


def searxng_public_enabled() -> bool:
    return _flag("IMURUN_RESEARCH_SEARXNG_PUBLIC", True)


def news_locale() -> tuple[str, str, str]:
    """(hl, gl, ceid) for Google News RSS, from IMURUN_RESEARCH_LOCALE (e.g. en-IN)."""
    raw = os.getenv("IMURUN_RESEARCH_LOCALE", "").strip() or "en-US"
    lang, _, country = raw.partition("-")
    lang = (lang or "en").lower()
    country = (country or "US").upper()
    return f"{lang}-{country}", country, f"{country}:{lang}"
