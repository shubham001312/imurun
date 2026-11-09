"""Weather Tool - Gets current weather for a location."""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from .base import Tool, ToolResult


class WeatherTool(Tool):
    """Get current weather for a location."""

    name = "weather"
    description = "Get current weather conditions for a location."
    parameters = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "City name, or 'lat,lon' coordinates.",
            },
            "units": {
                "type": "string",
                "description": "Units: 'metric' (Celsius), 'imperial' (Fahrenheit), 'standard' (Kelvin).",
                "enum": ["metric", "imperial", "standard"],
            },
        },
        "required": ["location"],
    }
    requires_network = True

    def __init__(self):
        self.api_key = os.getenv("IMURUN_OPENWEATHER_API_KEY", "").strip()

    def available(self) -> bool:
        return bool(self.api_key)

    def unavailable_reason(self) -> str:
        if not self.api_key:
            return "OpenWeatherMap API key not configured (set IMURUN_OPENWEATHER_API_KEY)"
        return ""

    def run(self, args: dict[str, Any]) -> ToolResult:
        location = (args.get("location") or "").strip()
        if not location:
            return ToolResult(
                ok=False,
                error="location is required",
                content="Please provide a location (city name or 'lat,lon').",
            )

        units = args.get("units", "metric")
        if units not in ("metric", "imperial", "standard"):
            units = "metric"

        # Geocode location if it's not lat,lon
        if "," not in location:
            # Use OpenWeatherMap geocoding API
            geo_url = "https://api.openweathermap.org/geo/1.0/direct"
            params = {
                "q": location,
                "limit": 1,
                "appid": self.api_key,
            }
            geo_url = f"https://api.openweathermap.org/geo/1.0/direct?q={urllib.parse.quote(location)}&limit=1&appid={self.api_key}"
            
            try:
                with urllib.request.urlopen(geo_url, timeout=10) as resp:
                    geo_data = json.loads(resp.read().decode())
            except Exception as e:
                return ToolResult(ok=False, error=str(e), content=f"Geocoding failed: {e}")

            if not geo_data:
                return ToolResult(
                    ok=False,
                    error="location not found",
                    content=f"Could not find location: {location}",
                )
            
            lat = geo_data[0]["lat"]
            lon = geo_data[0]["lon"]
            display_name = geo_data[0].get("name", location)
            country = geo_data[0].get("country", "")
            if country:
                display_name += f", {country}"
        else:
            # Parse lat,lon
            try:
                parts = location.split(",")
                lat = float(parts[0].strip())
                lon = float(parts[1].strip())
                display_name = f"{lat:.4f}, {lon:.4f}"
            except Exception:
                return ToolResult(
                    ok=False,
                    error="invalid coordinates",
                    content="Coordinates must be in format 'lat,lon' (e.g., '35.6762,139.6503').",
                )

        # Get weather
        weather_url = (
            f"https://api.openweathermap.org/data/2.5/weather"
            f"?lat={lat}&lon={lon}&units={units}&appid={self.api_key}"
        )
        
        try:
            with urllib.request.urlopen(weather_url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e:
            return ToolResult(ok=False, error=str(e), content=f"Weather request failed: {e}")

        # Parse response
        weather = data.get("weather", [{}])[0]
        main = data.get("main", {})
        wind = data.get("wind", {})
        sys_info = data.get("sys", {})

        temp = main.get("temp")
        feels_like = main.get("feels_like")
        humidity = main.get("humidity")
        pressure = main.get("pressure")
        description = weather.get("description", "").capitalize()
        icon = weather.get("icon", "")
        wind_speed = wind.get("speed")
        wind_deg = wind.get("deg")
        sunrise = sys_info.get("sunrise")
        sunset = sys_info.get("sunset")
        city_name = data.get("name", display_name)

        # Unit labels
        if units == "metric":
            temp_unit = "°C"
            speed_unit = "m/s"
        elif units == "imperial":
            temp_unit = "°F"
            speed_unit = "mph"
        else:
            temp_unit = "K"
            speed_unit = "m/s"

        # Format output
        lines = [
            f"Weather for {city_name}:",
            f"  {description}, {temp}{temp_unit} (feels like {feels_like}{temp_unit})",
            f"  Humidity: {humidity}%, Pressure: {pressure} hPa",
            f"  Wind: {wind_speed} {speed_unit} at {wind_deg}°",
        ]
        if sunrise and sunset:
            from datetime import datetime
            sr = datetime.fromtimestamp(sunrise).strftime("%H:%M")
            ss = datetime.fromtimestamp(sunset).strftime("%H:%M")
            lines.append(f"  Sunrise: {sr}, Sunset: {ss}")

        content = "\n".join(lines)

        data = {
            "location": display_name,
            "temperature": temp,
            "feels_like": feels_like,
            "description": description.lower(),
            "humidity": humidity,
            "pressure": pressure,
            "wind_speed": wind_speed,
            "wind_direction": wind_deg,
            "units": units,
        }

        return ToolResult(ok=True, content=content, data=data)