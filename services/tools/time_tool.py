"""Time and Date Tool - Returns current time, date, timezone info."""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Any

from .base import Tool, ToolResult


class TimeNowTool(Tool):
    """Returns current time, date, and timezone information."""

    name = "time_now"
    description = "Get current date, time, and timezone information."
    parameters = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA timezone name (e.g., 'America/New_York', 'Asia/Tokyo', 'UTC'). Optional, defaults to local time.",
            },
            "format": {
                "type": "string",
                "description": "Output format: 'iso', 'human', 'unix', or 'full'. Defaults to 'full'.",
                "enum": ["iso", "human", "unix", "full"],
            },
        },
    }
    requires_network = False

    def available(self) -> bool:
        return True

    def run(self, args: dict[str, Any]) -> ToolResult:
        tz_name = args.get("timezone")
        fmt = args.get("format", "full")

        try:
            if tz_name:
                # Try to use zoneinfo (Python 3.9+)
                try:
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo(tz_name)
                except Exception:
                    # Fallback: parse offset like "+09:00" or "-05:00"
                    if tz_name.startswith(("+", "-")):
                        sign = 1 if tz_name[0] == "+" else -1
                        parts = tz_name[1:].split(":")
                        hours = int(parts[0])
                        minutes = int(parts[1]) if len(parts) > 1 else 0
                        offset = sign * (hours * 3600 + minutes * 60)
                        tz = timezone(timedelta(seconds=offset))
                    else:
                        return ToolResult(
                            ok=False,
                            error=f"Unknown timezone: {tz_name}",
                            content=f"Unknown timezone: {tz_name}. Use IANA name like 'Asia/Tokyo' or offset like '+09:00'.",
                        )
                else:
                    tz = None  # Local time

                now = datetime.now(tz)

                if fmt == "iso":
                    content = now.isoformat()
                elif fmt == "unix":
                    content = str(int(now.timestamp()))
                elif fmt == "human":
                    content = now.strftime("%Y-%m-%d %H:%M:%S %Z%z")
                else:  # full
                    lines = [
                        f"Current time: {now.strftime('%H:%M:%S')}",
                        f"Date: {now.strftime('%A, %B %d, %Y')}",
                        f"Timezone: {now.tzname() or 'local'} ({now.strftime('%z')})",
                        f"Unix timestamp: {int(now.timestamp())}",
                        f"ISO 8601: {now.isoformat()}",
                    ]
                    content = "\n".join(lines)

                data = {
                    "timezone": str(now.tzinfo) if now.tzinfo else "local",
                    "iso": now.isoformat(),
                    "unix": int(now.timestamp()),
                    "human": now.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
                }

                return ToolResult(ok=True, content=content, data=data)

        except Exception as e:
            return ToolResult(
                ok=False,
                error=str(e),
                content=f"Error getting time: {e}",
            )


class TimeZoneListTool(Tool):
    """List common timezones."""

    name = "timezone_list"
    description = "List common IANA timezone names."
    parameters = {
        "type": "object",
        "properties": {
            "region": {
                "type": "string",
                "description": "Filter by region (e.g., 'America', 'Europe', 'Asia', 'Pacific').",
            },
        },
    }
    requires_network = False

    def available(self) -> bool:
        return True

    def run(self, args: dict[str, Any]) -> ToolResult:
        common_timezones = {
            "America": [
                "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
                "America/Anchorage", "America/Phoenix", "America/Toronto", "America/Vancouver",
            ],
            "Europe": [
                "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Rome",
                "Europe/Madrid", "Europe/Amsterdam", "Europe/Stockholm", "Europe/Warsaw",
            ],
            "Asia": [
                "Asia/Tokyo", "Asia/Shanghai", "Asia/Singapore", "Asia/Dubai",
                "Asia/Hong_Kong", "Asia/Seoul", "Asia/Taipei", "Asia/Bangkok",
            ],
            "Pacific": [
                "Pacific/Auckland", "Pacific/Sydney", "Pacific/Honolulu", "Pacific/Fiji",
            ],
            "UTC": ["UTC"],
        }

        region = args.get("region")
        if region:
            if region in common_timezones:
                zones = common_timezones[region]
            else:
                zones = [tz for tzs in common_timezones.values() for tz in tzs if region.lower() in tz.lower()]
        else:
            zones = [tz for tzs in common_timezones.values() for tz in tzs]

        content = "Available timezones:\n" + "\n".join(f"  {tz}" for tz in sorted(zones))
        return ToolResult(ok=True, content=content, data={"timezones": sorted(zones)})