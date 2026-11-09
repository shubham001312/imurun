"""The clock.

This tool never touches the network: the machine knows what time it is.
It returns the local time, the UTC time, the timezone and the date, so a
question like "what time is it" or "what is today's date" is answered from
a fact rather than from the model's memory.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..base import Tool, ToolResult

WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


class TimeNowTool(Tool):
    name = "time_now"
    description = (
        "The current date and time on this machine. Use it whenever the user "
        "asks what time it is, what today's date is, what day of the week it "
        "is, or anything that depends on 'now'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "Optional IANA timezone name, for example 'Asia/Kolkata'.",
            }
        },
    }
    requires_network = False

    def run(self, args: dict[str, Any]) -> ToolResult:
        name = str(args.get("timezone") or "").strip()
        utc_now = datetime.now(timezone.utc)
        if name:
            try:
                from zoneinfo import ZoneInfo

                now = utc_now.astimezone(ZoneInfo(name))
                zone_name = name
            except Exception:  # noqa: BLE001 - an unknown zone falls back to local
                now = utc_now.astimezone()
                zone_name = now.tzname() or "local"
        else:
            # The machine's own zone. (An earlier version reported UTC here
            # while labelling it with the local zone name.)
            now = utc_now.astimezone()
            zone_name = now.tzname() or "local"

        data = {
            "local_time": now.strftime("%H:%M"),
            "local_date": now.strftime("%Y-%m-%d"),
            "weekday": WEEKDAYS[now.weekday()],
            "timezone": zone_name,
            "utc_offset": now.strftime("%z"),
            "utc_time": utc_now.strftime("%H:%M"),
            "utc_date": utc_now.strftime("%Y-%m-%d"),
            "iso": now.isoformat(),
            "epoch": int(now.timestamp()),
        }
        content = (
            f"It is {data['local_time']} on {data['weekday']} {data['local_date']} "
            f"({zone_name}, UTC{data['utc_offset'][:3]}:{data['utc_offset'][3:]})."
        )
        return ToolResult(ok=True, content=content, data=data, source="system clock")
