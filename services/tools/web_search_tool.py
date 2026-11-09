"""Web Search Tool - searches the web for unknown facts."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from .base import Tool, ToolResult
from .web_search import search_web_sync, WebSearchResult


class WebSearchTool(Tool):
    """Search the web for current information."""

    name = "web_search"
    description = "Search the web for current information, news, facts, prices, etc."
    requires_network = True

    def __init__(self, max_results: int = 5):
        self.max_results = max_results

    def available(self) -> bool:
        return True  # Always available if network is allowed

    def unavailable_reason(self) -> str:
        return "Network access required"

    def run(self, args: dict[str, Any]) -> ToolResult:
        query = args.get("query", "").strip()
        if not query:
            return ToolResult(
                ok=False,
                error="query is required",
                content="Please provide a search query.",
            )

        max_results = args.get("max_results", self.max_results)

        try:
            result: WebSearchResult = asyncio.run(self._search_async(query, max_results))
        except Exception as exc:
            return ToolResult(
                ok=False,
                error=f"Search failed: {exc}",
                content=f"Web search failed: {exc}",
            )

        if not result.results:
            return ToolResult(
                ok=True,
                content=f"No results found for '{result.query}'.",
                data={"query": result.query, "results": []},
            )

        # Format results for display
        lines = [f"Web search results for: {result.query}"]
        for i, r in enumerate(result.results, 1):
            lines.append(f"{i}. {r.title}")
            lines.append(f"   {r.snippet}")
            lines.append(f"   Source: {r.url}")
            lines.append("")

        content = "\n".join(lines)
        data = {
            "query": result.query,
            "results": [
                {"title": r.title, "snippet": r.snippet, "url": r.url}
                for r in result.results
            ],
        }
        return ToolResult(ok=True, content=content, data=data)

    async def _search_async(self, query: str, max_results: int):
        from .web_search import search_web
        return await search_web(query, max_results)