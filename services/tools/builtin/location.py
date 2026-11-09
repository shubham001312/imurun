"""Where the user is.

Three sources, in this order:

1. what the shell knows - the browser shares its position, or the user
   typed a place;
2. an IP lookup, which is approximate and is labelled as such;
3. nothing, in which case the tool says so. It never guesses.

The browser location lives in memory for the life of the process and can be
set through ``POST /api/tools/location``.
"""
from __future__ import annotations

import threading
from typing import Any

from ..base import Tool, ToolResult
from ..http import request_json

_lock = threading.Lock()
_client_location: dict[str, Any] | None = None


def set_client_location(location: dict[str, Any] | None) -> None:
    """Store the location the shell reported, or clear it with ``None``."""
    global _client_location
    with _lock:
        _client_location = dict(location) if location else None


def get_client_location() -> dict[str, Any] | None:
    with _lock:
        return dict(_client_location) if _client_location else None


def _ip_lookup() -> dict[str, Any] | None:
    payload = request_json("https://ipapi.co/json/")
    if not isinstance(payload, dict):
        return None
    return {
        "name": payload.get("city"),
        "region": payload.get("region"),
        "country": payload.get("country_name"),
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "timezone": payload.get("timezone"),
    }


class LocationTool(Tool):
    name = "location"
    description = (
        "The user's own locality: city, region, country and timezone. Use it "
        "for questions like 'where am I', 'what time is it for me' or when the "
        "answer depends on the user's area."
    )
    parameters = {
        "type": "object",
        "properties": {
            "place": {
                "type": "string",
                "description": "Optional place name that overrides the stored location.",
            }
        },
    }
    requires_network = False

    def available(self) -> bool:
        return get_client_location() is not None

    def unavailable_reason(self) -> str:
        if self.available():
            return ""
        return "The shell has not shared a location yet."

    def run(self, args: dict[str, Any]) -> ToolResult:
        place = str(args.get("place") or "").strip()
        client = get_client_location()
        approximate = False

        if place:
            location = {
                "name": place,
                "region": None,
                "country": None,
                "latitude": client.get("latitude") if client else None,
                "longitude": client.get("longitude") if client else None,
                "timezone": client.get("timezone") if client else None,
            }
            approximate = True
        elif client:
            location = client
        else:
            try:
                found = _ip_lookup()
            except Exception:  # noqa: BLE001 - offline or blocked
                found = None
            if not found:
                return ToolResult(
                    ok=False,
                    error="The location is unknown.",
                    content="The location is unknown, so a local answer cannot be given.",
                    source="location",
                )
            location = found
            approximate = True

        parts = [
            part
            for part in (location.get("name"), location.get("region"), location.get("country"))
            if part
        ]
        where = ", ".join(parts) if parts else "an unknown place"
        content = f"The user is in {where}."
        if location.get("timezone"):
            content += f" The local timezone is {location['timezone']}."
        if approximate:
            content += " This is an approximate location."
        return ToolResult(
            ok=True,
            content=content,
            data={"location": location, "approximate": approximate},
            source="location",
        )
