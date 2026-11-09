"""Web Search Tool - Searches the web for unknown facts."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# DuckDuckGo HTML endpoint (no API key needed)
DUCKDUCKGO_HTML = "https://html.duckduckgo.com/html/"


@dataclass
class SearchResult:
    title: str
    snippet: str
    url: str


@dataclass
class WebSearchResult:
    query: str
    results: list[SearchResult]
    timestamp: float


async def search_web(query: str, max_results: int = 5) -> WebSearchResult:
    """Search the web using DuckDuckGo HTML scraping."""
    encoded_query = urllib.parse.quote_plus(query)
    url = f"{DUCKDUCKGO_HTML}?q={encoded_query}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    
    req = urllib.request.Request(url, headers=headers)
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"Web search failed: {e}")
        return WebSearchResult(query=query, results=[], timestamp=time.time())
    
    # Parse results from HTML
    results = []
    # DuckDuckGo result pattern
    pattern = r'class="result__title">\s*<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?class="result__snippet">(.*?)</a>'
    matches = re.findall(pattern, html, re.DOTALL)
    
    for match in matches[:max_results]:
        url_raw, title_raw, snippet_raw = match
        # Clean up
        title = re.sub(r'<[^>]+>', '', title_raw).strip()
        snippet = re.sub(r'<[^>]+>', '', snippet_raw).strip()
        # Fix URL if it's a redirect
        if url_raw.startswith("//duckduckgo.com/l/?uddg="):
            # Extract actual URL
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url_raw).query)
            actual_url = parsed.get("uddg", [url_raw])[0]
        else:
            actual_url = url_raw
        
        if title and snippet:
            results.append(SearchResult(title=title, snippet=snippet, url=actual_url))
    
    return WebSearchResult(query=query, results=results, timestamp=time.time())


def search_web_sync(query: str, max_results: int = 5) -> WebSearchResult:
    """Synchronous wrapper for web search."""
    return asyncio.run(search_web(query, max_results))


def is_web_search_needed(query: str) -> bool:
    """Determine if a query needs web search."""
    # Keywords that indicate need for current/live information
    web_keywords = [
        "current", "latest", "recent", "news", "today", "yesterday", "this week",
        "price", "stock", "weather", "score", "result", "release", "update",
        "version", "2024", "2025", "who is", "what is", "how to", "where is",
        "when did", "why is", "live", "ongoing", "breaking"
    ]
    query_lower = query.lower()
    return any(kw in query_lower for kw in web_keywords)