"""Live weather.

Open-Meteo serves current conditions and a short forecast without an API
key, so the tool works on a fresh install with no account. The place name is
geocoded first; when the user gives coordinates they are used directly.

WMO weather codes are mapped to plain English so the model never has to
guess what "code 61" means.
"""
from __future__ import annotations

from typing import Any

from ..base import Tool, ToolResult
from ..http import HttpError, build_url, request_json
from ..network import OfflineError

WMO = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Heavy freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light rain showers",
    81: "Rain showers",
    82: "Violent rain showers",
    85: "Light snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with heavy hail",
}


def describe_code(code: Any) -> str:
    """Turn a WMO weather code into words. Unknown codes stay unknown."""
    try:
        return WMO.get(int(code), f"Unknown conditions (code {code})")
    except (TypeError, ValueError):
        return "Unknown conditions"


def geocode(place: str) -> dict[str, Any] | None:
    """Find a place by name. Returns None when it cannot be resolved."""
    url = build_url(
        "https://geocoding-api.open-meteo.com/v1/search",
        {"name": place, "count": 1, "language": "en", "format": "json"},
    )
    payload = request_json(url)
    results = payload.get("results") or []
    if not results:
        return None
    first = results[0]
    return {
        "name": first.get("name"),
        "region": first.get("admin1"),
        "country": first.get("country"),
        "latitude": first.get("latitude"),
        "longitude": first.get("longitude"),
        "timezone": first.get("timezone"),
    }


class WeatherTool(Tool):
    name = "weather"
    description = (
        "Current weather and a three day forecast for a place. Use it for "
        "questions about temperature, rain, wind, humidity or the forecast, "
        "today or in the next few days."
    )
    parameters = {
        "type": "object",
        "properties": {
            "place": {
                "type": "string",
                "description": "City or place name, for example 'Berlin'.",
            },
            "latitude": {"type": "number", "description": "Latitude, if known."},
            "longitude": {"type": "number", "description": "Longitude, if known."},
        },
    }
    requires_network = True
    category = "network"

    def available(self) -> bool:
        from ..network import network_available

        return network_available()

    def run(self, args: dict[str, Any]) -> ToolResult:
        place = str(args.get("place") or "").strip()
        latitude = args.get("latitude")
        longitude = args.get("longitude")
        location = None

        try:
            if latitude is not None and longitude is not None:
                location = {
                    "name": place or "the given coordinates",
                    "region": None,
                    "country": None,
                    "latitude": float(latitude),
                    "longitude": float(longitude),
                    "timezone": None,
                }
            elif place:
                location = geocode(place)
                if location is None:
                    return ToolResult(
                        ok=False,
                        error=f'"{place}" could not be found.',
                        content=f'"{place}" could not be found, so there is no weather to report.',
                        source="open-meteo",
                    )
            else:
                return ToolResult(
                    ok=False,
                    error="No place was given.",
                    content="No place was given, so the weather cannot be looked up.",
                    source="open-meteo",
                )

            url = build_url(
                "https://api.open-meteo.com/v1/forecast",
                {
                    "latitude": location["latitude"],
                    "longitude": location["longitude"],
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
                    "timezone": "auto",
                    "forecast_days": 3,
                },
            )
            payload = request_json(url)
        except OfflineError:
            return ToolResult(
                ok=False,
                error="No network access right now.",
                content="No network access right now, so the weather cannot be looked up.",
                source="open-meteo",
                offline=True,
            )
        except HttpError as exc:
            return ToolResult(
                ok=False,
                error=str(exc),
                content=f"The weather service could not be reached: {exc}",
                source="open-meteo",
            )
        current = payload.get("current") or {}
        daily = payload.get("daily") or {}

        code = current.get("weather_code")
        conditions = describe_code(code)
        temperature = current.get("temperature_2m")
        feels = current.get("apparent_temperature")
        humidity = current.get("relative_humidity_2m")
        wind = current.get("wind_speed_10m")

        days = []
        dates = daily.get("time") or []
        for index, day in enumerate(dates[:3]):
            days.append(
                {
                    "date": day,
                    "max_c": (daily.get("temperature_2m_max") or [None] * 3)[index]
                    if index < len(daily.get("temperature_2m_max") or [])
                    else None,
                    "min_c": (daily.get("temperature_2m_min") or [None] * 3)[index]
                    if index < len(daily.get("temperature_2m_min") or [])
                    else None,
                    "precipitation_mm": (daily.get("precipitation_sum") or [None] * 3)[index]
                    if index < len(daily.get("precipitation_sum") or [])
                    else None,
                    "conditions": describe_code(
                        (daily.get("weather_code") or [None] * 3)[index]
                        if index < len(daily.get("weather_code") or [])
                        else None
                    ),
                }
            )

        where = ", ".join(
            part for part in (location.get("name"), location.get("region"), location.get("country")) if part
        )
        lines = [f"{where}: {conditions.lower()}, {temperature} C" if temperature is not None else f"{where}: {conditions.lower()}."]
        if feels is not None:
            lines.append(f"Feels like {feels} C.")
        if humidity is not None:
            lines.append(f"Humidity {humidity}%.")
        if wind is not None:
            lines.append(f"Wind {wind} km/h.")
        for day in days:
            span = f"{day['min_c']} to {day['max_c']} C" if day["max_c"] is not None else "-"
            lines.append(f"{day['date']}: {span}, {day['conditions'].lower()}.")

        data = {
            "place": location,
            "current": {
                "temperature_c": temperature,
                "apparent_temperature_c": feels,
                "humidity_percent": humidity,
                "wind_kmh": wind,
                "conditions": conditions,
                "weather_code": code,
            },
            "forecast": days,
        }
        return ToolResult(
            ok=True,
            content=" ".join(lines),
            data=data,
            source="open-meteo",
        )
