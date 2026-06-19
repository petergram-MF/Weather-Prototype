"""
Tomorrow.io weather client — foundation for all POCs.

Provides typed dataclasses for current conditions, hourly and daily forecasts,
plus a lightweight file-based cache to protect API quota during development.
"""

import json
import hashlib
import time
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from config import TOMORROW_API_KEY, CACHE_TTL_CURRENT, CACHE_TTL_FORECAST

BASE_URL = "https://api.tomorrow.io/v4"
CACHE_DIR = Path(__file__).parent / ".cache"

# ── Weather condition codes ────────────────────────────────────────────────────

WEATHER_CODES: dict[int, str] = {
    0:    "Unknown",
    1000: "Clear",
    1100: "Mostly Clear",
    1101: "Partly Cloudy",
    1102: "Mostly Cloudy",
    1001: "Cloudy",
    2000: "Fog",
    2100: "Light Fog",
    4000: "Drizzle",
    4001: "Rain",
    4200: "Light Rain",
    4201: "Heavy Rain",
    5000: "Snow",
    5001: "Flurries",
    5100: "Light Snow",
    5101: "Heavy Snow",
    6000: "Freezing Drizzle",
    6001: "Freezing Rain",
    6200: "Light Freezing Rain",
    6201: "Heavy Freezing Rain",
    7000: "Ice Pellets",
    7101: "Heavy Ice Pellets",
    7102: "Light Ice Pellets",
    8000: "Thunderstorm",
}

def condition_label(code: int) -> str:
    return WEATHER_CODES.get(code, f"Code {code}")

# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class CurrentConditions:
    time: datetime
    temperature: float          # °C
    temperature_apparent: float # °C — feels like
    humidity: float             # %
    dew_point: float            # °C
    precipitation_probability: float  # %
    precipitation_intensity: float    # mm/hr
    wind_speed: float           # m/s
    wind_direction: float       # degrees
    wind_gust: float            # m/s
    uv_index: float
    weather_code: int
    visibility: float           # km
    cloud_cover: float          # %
    pressure: float             # hPa

    @property
    def condition(self) -> str:
        return condition_label(self.weather_code)

    def is_raining(self) -> bool:
        return self.weather_code in {4000, 4001, 4200, 4201, 6000, 6001, 6200, 6201}

    def is_snowing(self) -> bool:
        return self.weather_code in {5000, 5001, 5100, 5101}

    def summary(self) -> str:
        return (
            f"{self.condition} | {self.temperature:.1f}°C "
            f"(feels {self.temperature_apparent:.1f}°C) | "
            f"Humidity {self.humidity:.0f}% | Wind {self.wind_speed:.1f} m/s "
            f"(gusts {self.wind_gust:.1f}) | "
            f"Rain {self.precipitation_probability:.0f}% | UV {self.uv_index:.0f}"
        )


@dataclass
class HourlyForecast:
    time: datetime
    temperature: float
    temperature_apparent: float
    humidity: float
    dew_point: float
    precipitation_probability: float
    precipitation_intensity: float
    snow_intensity: float       # mm/hr
    wind_speed: float
    wind_direction: float
    wind_gust: float
    uv_index: float
    weather_code: int
    visibility: float
    cloud_cover: float
    pressure: float

    @property
    def condition(self) -> str:
        return condition_label(self.weather_code)

    def is_good_outdoor(
        self,
        max_rain_prob: float = 30,
        min_temp: float = 5,
        max_wind: float = 10,
    ) -> bool:
        return (
            self.precipitation_probability <= max_rain_prob
            and self.temperature >= min_temp
            and self.wind_speed <= max_wind
        )

    def summary(self) -> str:
        return (
            f"{self.time.strftime('%a %H:%M')} | {self.condition} | "
            f"{self.temperature:.1f}°C | "
            f"Rain {self.precipitation_probability:.0f}% | "
            f"Wind {self.wind_speed:.1f} m/s"
        )


@dataclass
class DailyForecast:
    time: datetime
    temperature_min: float
    temperature_max: float
    temperature_avg: float
    humidity_avg: float
    precipitation_probability_avg: float
    precipitation_probability_max: float
    precipitation_intensity_avg: float
    wind_speed_avg: float
    wind_gust_max: float
    uv_index_max: float
    uv_index_avg: float
    weather_code_max: int
    sunrise_time: datetime
    sunset_time: datetime
    moon_phase: float           # 0–1
    snow_accumulation: float    # mm
    snow_depth: float           # mm
    visibility_avg: float
    cloud_cover_avg: float

    @property
    def condition(self) -> str:
        return condition_label(self.weather_code_max)

    @property
    def daylight_hours(self) -> float:
        delta = self.sunset_time - self.sunrise_time
        return delta.total_seconds() / 3600

    def is_good_day(
        self,
        max_rain_prob: float = 30,
        min_temp: float = 5,
        max_wind: float = 10,
    ) -> bool:
        return (
            self.precipitation_probability_avg <= max_rain_prob
            and self.temperature_max >= min_temp
            and self.wind_speed_avg <= max_wind
        )

    def summary(self) -> str:
        return (
            f"{self.time.strftime('%A')} {self.time.day} {self.time.strftime('%b')} | "
            f"{self.condition} | "
            f"{self.temperature_min:.1f}-{self.temperature_max:.1f}degC | "
            f"Rain {self.precipitation_probability_avg:.0f}% | "
            f"UV {self.uv_index_max:.0f} | "
            f"Snow {self.snow_accumulation:.1f}mm | "
            f"Daylight {self.daylight_hours:.1f}h"
        )


@dataclass
class WeatherData:
    location_name: str
    latitude: float
    longitude: float
    current: CurrentConditions
    hourly: list[HourlyForecast] = field(default_factory=list)
    daily: list[DailyForecast] = field(default_factory=list)

    def best_outdoor_hours(
        self,
        max_rain_prob: float = 30,
        min_temp: float = 5,
        max_wind: float = 10,
        limit: int = 5,
    ) -> list[HourlyForecast]:
        return [
            h for h in self.hourly
            if h.is_good_outdoor(max_rain_prob, min_temp, max_wind)
        ][:limit]

    def best_days(
        self,
        max_rain_prob: float = 30,
        min_temp: float = 5,
        max_wind: float = 10,
    ) -> list[DailyForecast]:
        return [d for d in self.daily if d.is_good_day(max_rain_prob, min_temp, max_wind)]


# ── Cache ──────────────────────────────────────────────────────────────────────

def _cache_key(endpoint: str, params: dict) -> str:
    payload = json.dumps({"endpoint": endpoint, **params}, sort_keys=True)
    return hashlib.md5(payload.encode()).hexdigest()


def _read_cache(key: str, ttl: int) -> Optional[dict]:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl:
        return None
    with path.open() as f:
        return json.load(f)


def _write_cache(key: str, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    with path.open("w") as f:
        json.dump(data, f)


# ── Client ─────────────────────────────────────────────────────────────────────

class WeatherClient:
    def __init__(self, api_key: str = TOMORROW_API_KEY):
        self.api_key = api_key
        self._session = requests.Session()
        self._session.headers.update({
            "accept-encoding": "deflate, gzip, br",
            "accept": "application/json",
        })

    def _get(self, endpoint: str, params: dict, cache_ttl: int) -> dict:
        key = _cache_key(endpoint, params)
        cached = _read_cache(key, cache_ttl)
        if cached is not None:
            return cached

        params = {**params, "apikey": self.api_key}
        response = self._session.get(f"{BASE_URL}/{endpoint}", params=params)
        response.raise_for_status()
        data = response.json()
        _write_cache(key, data)
        return data

    @staticmethod
    def _dt(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    def _parse_current(self, raw: dict) -> CurrentConditions:
        v = raw["data"]["values"]
        return CurrentConditions(
            time=self._dt(raw["data"]["time"]),
            temperature=v.get("temperature", 0),
            temperature_apparent=v.get("temperatureApparent", 0),
            humidity=v.get("humidity", 0),
            dew_point=v.get("dewPoint", 0),
            precipitation_probability=v.get("precipitationProbability", 0),
            precipitation_intensity=v.get("precipitationIntensity", 0),
            wind_speed=v.get("windSpeed", 0),
            wind_direction=v.get("windDirection", 0),
            wind_gust=v.get("windGust", 0),
            uv_index=v.get("uvIndex", 0),
            weather_code=v.get("weatherCode", 0),
            visibility=v.get("visibility", 0),
            cloud_cover=v.get("cloudCover", 0),
            pressure=v.get("pressureSurfaceLevel", 0),
        )

    def _parse_hourly(self, entries: list) -> list[HourlyForecast]:
        result = []
        for h in entries:
            v = h["values"]
            result.append(HourlyForecast(
                time=self._dt(h["time"]),
                temperature=v.get("temperature", 0),
                temperature_apparent=v.get("temperatureApparent", 0),
                humidity=v.get("humidity", 0),
                dew_point=v.get("dewPoint", 0),
                precipitation_probability=v.get("precipitationProbability", 0),
                precipitation_intensity=v.get("precipitationIntensity", 0),
                snow_intensity=v.get("snowIntensity", 0),
                wind_speed=v.get("windSpeed", 0),
                wind_direction=v.get("windDirection", 0),
                wind_gust=v.get("windGust", 0),
                uv_index=v.get("uvIndex", 0),
                weather_code=v.get("weatherCode", 0),
                visibility=v.get("visibility", 0),
                cloud_cover=v.get("cloudCover", 0),
                pressure=v.get("pressureSurfaceLevel", 0),
            ))
        return result

    def _parse_daily(self, entries: list) -> list[DailyForecast]:
        result = []
        for d in entries:
            v = d["values"]
            # Sunrise/sunset may be absent — fall back to the day's timestamp
            sunrise_raw = v.get("sunriseTime", d["time"])
            sunset_raw = v.get("sunsetTime", d["time"])
            result.append(DailyForecast(
                time=self._dt(d["time"]),
                temperature_min=v.get("temperatureMin", 0),
                temperature_max=v.get("temperatureMax", 0),
                temperature_avg=v.get("temperatureAvg", 0),
                humidity_avg=v.get("humidityAvg", 0),
                precipitation_probability_avg=v.get("precipitationProbabilityAvg", 0),
                precipitation_probability_max=v.get("precipitationProbabilityMax", 0),
                precipitation_intensity_avg=v.get("precipitationIntensityAvg", 0),
                wind_speed_avg=v.get("windSpeedAvg", 0),
                wind_gust_max=v.get("windGustMax", 0),
                uv_index_max=v.get("uvIndexMax", 0),
                uv_index_avg=v.get("uvIndexAvg", 0),
                weather_code_max=v.get("weatherCodeMax", 0),
                sunrise_time=self._dt(sunrise_raw),
                sunset_time=self._dt(sunset_raw),
                moon_phase=v.get("moonPhase", 0),
                snow_accumulation=v.get("snowAccumulationSum", 0),
                snow_depth=v.get("snowDepthAvg", 0),
                visibility_avg=v.get("visibilityAvg", 0),
                cloud_cover_avg=v.get("cloudCoverAvg", 0),
            ))
        return result

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_current(self, location: str) -> CurrentConditions:
        """Current conditions for a location (city name, 'lat,lon', or zip)."""
        raw = self._get("weather/realtime", {"location": location}, CACHE_TTL_CURRENT)
        return self._parse_current(raw)

    def get_forecast(self, location: str, days: int = 5) -> WeatherData:
        """Full WeatherData: current + hourly (5 days) + daily (up to `days` days)."""
        current_raw = self._get(
            "weather/realtime", {"location": location}, CACHE_TTL_CURRENT
        )
        forecast_raw = self._get(
            "weather/forecast",
            {"location": location, "timesteps": "1h,1d"},
            CACHE_TTL_FORECAST,
        )

        loc = forecast_raw.get("location", {})
        timelines = forecast_raw["timelines"]

        return WeatherData(
            location_name=loc.get("name", location),
            latitude=loc.get("lat", 0.0),
            longitude=loc.get("lon", 0.0),
            current=self._parse_current(current_raw),
            hourly=self._parse_hourly(timelines.get("hourly", [])),
            daily=self._parse_daily(timelines.get("daily", [])[:days]),
        )


# ── Quick demo ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    client = WeatherClient()

    # Use city name only, or "lat,lon" — commas in city+country confuse the lat/lon parser
    location = "Oslo"
    print(f"\nWeather for {location}\n{'=' * 50}")

    data = client.get_forecast(location)

    print(f"\nNow:  {data.current.summary()}")

    print(f"\nNext 6 hours:")
    for h in data.hourly[:6]:
        print(f"  {h.summary()}")

    print(f"\n{len(data.daily)}-day outlook:")
    for d in data.daily:
        print(f"  {d.summary()}")

    best = data.best_outdoor_hours()
    print(f"\nBest outdoor hours (next 5 good windows):")
    for h in best:
        print(f"  {h.summary()}")
