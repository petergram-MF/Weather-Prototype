"""
Travel POC — finds weekend escape opportunities when local weather is bad
and exotic destinations look great, with mock flight suggestions.

Uses Open-Meteo (free, no API key) for destination forecasts.
Airline data is mocked but deterministic (same destination+date → same prices).
"""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests

DESTINATIONS = [
    {"name": "Barcelona",  "country": "Spain",      "emoji": "🏖️",  "lat":  41.39, "lon":   2.15, "flight_h":  3.5, "price_range": (80,  280)},
    {"name": "Lisbon",     "country": "Portugal",   "emoji": "🌊",  "lat":  38.72, "lon":  -9.14, "flight_h":  4.0, "price_range": (90,  300)},
    {"name": "Tenerife",   "country": "Spain",      "emoji": "🌞",  "lat":  28.29, "lon": -16.63, "flight_h":  5.5, "price_range": (110, 340)},
    {"name": "Malta",      "country": "Malta",      "emoji": "⛵",  "lat":  35.90, "lon":  14.51, "flight_h":  4.5, "price_range": (120, 360)},
    {"name": "Dubrovnik",  "country": "Croatia",    "emoji": "🏰",  "lat":  42.65, "lon":  18.09, "flight_h":  3.5, "price_range": (95,  290)},
    {"name": "Athens",     "country": "Greece",     "emoji": "🏛️",  "lat":  37.98, "lon":  23.73, "flight_h":  4.0, "price_range": (100, 310)},
    {"name": "Marrakech",  "country": "Morocco",    "emoji": "🕌",  "lat":  31.63, "lon":  -8.01, "flight_h":  5.0, "price_range": (120, 320)},
    {"name": "Dubai",      "country": "UAE",        "emoji": "🏙️",  "lat":  25.20, "lon":  55.27, "flight_h":  6.5, "price_range": (180, 500)},
    {"name": "Bangkok",    "country": "Thailand",   "emoji": "🛕",  "lat":  13.75, "lon": 100.52, "flight_h": 10.5, "price_range": (280, 650)},
    {"name": "Bali",       "country": "Indonesia",  "emoji": "🌴",  "lat":  -8.34, "lon": 115.09, "flight_h": 15.0, "price_range": (420, 980)},
    {"name": "Miami",      "country": "USA",        "emoji": "🌅",  "lat":  25.78, "lon": -80.21, "flight_h":  9.5, "price_range": (320, 820)},
    {"name": "Zanzibar",   "country": "Tanzania",   "emoji": "🐚",  "lat":  -6.16, "lon":  39.20, "flight_h": 10.0, "price_range": (360, 900)},
]

# ── Open-Meteo weather client ──────────────────────────────────────────────────
# Free, no API key required. Used for destination forecasts.

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_DIR = Path(__file__).parent / ".cache"

# WMO weather code → (condition label, emoji)
WMO_CODES: list[tuple[int, str, str]] = [
    (99, "Thunderstorm",  "⛈️"),
    (95, "Thunderstorm",  "⛈️"),
    (82, "Heavy Showers", "🌧️"),
    (80, "Showers",       "🌦️"),
    (77, "Snow Grains",   "❄️"),
    (75, "Heavy Snow",    "❄️"),
    (71, "Light Snow",    "🌨️"),
    (67, "Heavy Rain",    "🌧️"),
    (61, "Light Rain",    "🌧️"),
    (55, "Drizzle",       "🌦️"),
    (51, "Light Drizzle", "🌦️"),
    (48, "Fog",           "🌫️"),
    (45, "Fog",           "🌫️"),
    ( 3, "Overcast",      "☁️"),
    ( 2, "Partly Cloudy", "⛅"),
    ( 1, "Mainly Clear",  "🌤️"),
    ( 0, "Clear",         "☀️"),
]

def _wmo_label(code: int) -> tuple[str, str]:
    for threshold, label, em in WMO_CODES:
        if code >= threshold:
            return label, em
    return "Unknown", "🌡️"


def _cache_read(key: str, ttl: int) -> Optional[dict]:
    path = CACHE_DIR / f"{key}.json"
    if path.exists() and (time.time() - path.stat().st_mtime) < ttl:
        with path.open() as f:
            return json.load(f)
    return None


def _cache_write(key: str, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    with (CACHE_DIR / f"{key}.json").open("w") as f:
        json.dump(data, f)


def fetch_destination_weather(lat: float, lon: float, days: int = 14) -> dict[str, dict]:
    """
    Returns daily weather keyed by 'YYYY-MM-DD' for the next `days` days.
    Cached for 3 hours. Also used for local extended forecast.
    """
    key = f"om_{lat:.3f}_{lon:.3f}_d{days}"
    cached = _cache_read(key, ttl=3 * 3600)
    if cached:
        return cached

    resp = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude":  lat,
            "longitude": lon,
            "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": days,
            "timezone": "auto",
        },
        timeout=10,
    )
    resp.raise_for_status()
    raw = resp.json()["daily"]

    result = {}
    for i, date_str in enumerate(raw["time"]):
        code = raw["weathercode"][i] or 0
        label, em = _wmo_label(code)
        result[date_str] = {
            "condition": label,
            "emoji":     em,
            "temp_max":  round(raw["temperature_2m_max"][i] or 0, 1),
            "temp_min":  round(raw["temperature_2m_min"][i] or 0, 1),
            "rain_prob": round(raw["precipitation_probability_max"][i] or 0),
        }

    _cache_write(key, result)
    return result


# ── Mock airline API ───────────────────────────────────────────────────────────

_AIRLINES = [
    {"name": "Norwegian",        "code": "DY", "tier": "budget"},
    {"name": "Ryanair",          "code": "FR", "tier": "budget"},
    {"name": "Wizz Air",         "code": "W6", "tier": "budget"},
    {"name": "SAS",              "code": "SK", "tier": "mid"},
    {"name": "KLM",              "code": "KL", "tier": "mid",  "hub": "Amsterdam"},
    {"name": "Turkish Airlines", "code": "TK", "tier": "mid",  "hub": "Istanbul"},
    {"name": "Lufthansa",        "code": "LH", "tier": "full", "hub": "Frankfurt"},
    {"name": "British Airways",  "code": "BA", "tier": "full", "hub": "London"},
    {"name": "Emirates",         "code": "EK", "tier": "full", "hub": "Dubai"},
]

_DEP_TIMES = ["06:10", "06:45", "07:20", "08:55", "10:30", "12:15", "14:40", "16:55", "18:20", "21:30"]


def _det(seed: str, lo: int, hi: int) -> int:
    """Deterministic integer in [lo, hi] derived from a string seed."""
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    return lo + (h % (hi - lo + 1))


def _fmt_duration(hours: float) -> str:
    h = int(hours)
    m = round((hours - h) * 60 / 5) * 5
    if m >= 60:
        h += 1; m = 0
    return f"{h}h {m:02d}m"


def _add_mins(t: str, mins: int) -> str:
    hh, mm = map(int, t.split(":"))
    total = hh * 60 + mm + mins
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def mock_flights(dest: dict, dep_date: date) -> list[dict]:
    """
    Generate 3 deterministic mock flights (budget / mid / full-service)
    for a destination on a given departure date.
    """
    seed = f"{dest['name']}_{dep_date.isoformat()}"
    lo, hi = dest["price_range"]
    fh = dest["flight_h"]
    flights = []

    for i, tier in enumerate(["budget", "mid", "full"]):
        pool = [a for a in _AIRLINES if a["tier"] == tier]
        airline = pool[_det(seed + tier, 0, len(pool) - 1)]

        # Price scales: budget ~20%, mid ~55%, full ~90% of range
        centre = lo + (hi - lo) * [0.20, 0.55, 0.90][i]
        price = round(_det(seed + tier + "p", int(centre * 0.88), int(centre * 1.12)) / 50) * 50

        dep = _DEP_TIMES[_det(seed + tier + "t", 0, len(_DEP_TIMES) - 1)]

        # Stops: short routes non-stop for mid/full, always 1 for long-haul budget
        stops = 0 if (fh <= 5 and tier in ("mid", "full")) else (1 if fh <= 11 else 2)
        via = airline.get("hub") if stops else None
        travel_h = fh + stops * 1.75
        arr = _add_mins(dep, round(travel_h * 60))

        flights.append({
            "airline":  airline["name"],
            "code":     airline["code"],
            "tier":     tier,
            "dep_time": dep,
            "arr_time": arr,
            "duration": _fmt_duration(travel_h),
            "stops":    stops,
            "via":      via,
            "price":    price,
            "currency": "GBP",
        })

    return sorted(flights, key=lambda x: x["price"])


# ── Scoring & recommendation logic ────────────────────────────────────────────

def _fmt_date_range(start: date, end: date) -> str:
    if start.month == end.month:
        return f"{start.strftime('%a')} {start.day}–{end.day} {start.strftime('%b')}"
    return f"{start.strftime('%a')} {start.day} {start.strftime('%b')} – {end.strftime('%a')} {end.day} {end.strftime('%b')}"


def find_best_window(wx_all: dict, num_days: int) -> Optional[dict]:
    """
    Slide a `num_days`-wide window over the available forecast dates
    and return the highest-scoring consecutive stretch.

    Scoring:
    - Average destination weather quality across all days
    - Consistency penalty: a single rainy day sharply reduces the score
      because it can ruin an otherwise perfect trip.
    """
    today = date.today()
    # Only consider dates from tomorrow onward (not today, so flights make sense)
    dates = sorted(d for d in wx_all if d > today.isoformat())

    if len(dates) < num_days:
        return None

    best: Optional[dict] = None
    best_score = -1

    for i in range(len(dates) - num_days + 1):
        window = dates[i:i + num_days]
        day_scores = [_dest_score(wx_all[d]) for d in window]
        avg = sum(day_scores) / num_days
        worst = min(day_scores)

        # Heavy penalty for the worst day — beach trips live or die by consistency
        consistency_penalty = max(0, 55 - worst) * 0.45
        final = max(0, round(avg - consistency_penalty))

        if final > best_score:
            best_score = final
            best = {
                "start":       window[0],
                "end":         window[-1],
                "score":       final,
                "avg_temp":    round(sum(wx_all[d]["temp_max"] for d in window) / num_days, 1),
                "avg_rain":    round(sum(wx_all[d]["rain_prob"] for d in window) / num_days),
                "day_previews": [
                    {"date": d, "emoji": wx_all[d]["emoji"],
                     "temp_max": wx_all[d]["temp_max"],
                     "rain_prob": wx_all[d]["rain_prob"]}
                    for d in window
                ],
            }

    return best


def _dest_score(wx: dict) -> int:
    score = 100 - max(0, wx["rain_prob"] - 10) * 3 - abs(wx["temp_max"] - 26) * 2
    return max(0, min(100, round(score)))


def _is_bad_local(wx: Optional[dict]) -> bool:
    if wx is None:
        return True  # outside 5-day window → assume typical Scandinavian weather
    return wx["rain_prob"] > 50 or wx["temp_max"] < 8


def _fmt_weekend(sat: date) -> str:
    sun = sat + timedelta(days=1)
    return f"{sat.strftime('%a')} {sat.day} {sat.strftime('%b')} – {sun.day} {sun.strftime('%b')}"


def get_travel_recommendations(local_daily: dict) -> list[dict]:
    """
    local_daily: dict keyed 'YYYY-MM-DD' → {condition, emoji, temp_max, rain_prob}

    Fetches 14-day Open-Meteo forecasts for all destinations in parallel,
    finds the best consecutive 3-day, 5-day and 7-day window at each,
    then scores and sorts all options.
    """
    # Fetch all destination forecasts in parallel
    dest_weather: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {
            ex.submit(fetch_destination_weather, d["lat"], d["lon"]): d["name"]
            for d in DESTINATIONS
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                dest_weather[name] = fut.result()
            except Exception:
                dest_weather[name] = {}

    results = []

    for dest in DESTINATIONS:
        wx_all = dest_weather.get(dest["name"], {})
        if not wx_all:
            continue

        for num_days in [3, 5, 7]:
            win = find_best_window(wx_all, num_days)
            if not win or win["score"] < 20:
                continue

            start_date = date.fromisoformat(win["start"])
            end_date   = date.fromisoformat(win["end"])

            # Check local weather during this window (Tomorrow.io covers ~5 days)
            local_during = [
                local_daily.get((start_date + timedelta(days=i)).isoformat())
                for i in range(num_days)
            ]
            local_during = [d for d in local_during if d]
            bad_local    = any(_is_bad_local(d) for d in local_during) if local_during else True

            # Worst local day for the "vs home" display
            if local_during:
                worst_local = max(local_during, key=lambda d: d.get("rain_prob", 0))
            else:
                worst_local = {"condition": "Typical", "emoji": "🌥️", "temp_max": None, "rain_prob": None}

            score = win["score"]
            if bad_local:
                score = min(100, round(score * 1.25))

            results.append({
                "destination":    dest["name"],
                "country":        dest["country"],
                "emoji":          dest["emoji"],
                "duration":       num_days,
                "duration_label": f"{num_days} days",
                "date_range":     _fmt_date_range(start_date, end_date),
                "start_date":     win["start"],
                "end_date":       win["end"],
                "dest_weather": {
                    "emoji":     wx_all[win["start"]]["emoji"],
                    "condition": wx_all[win["start"]]["condition"],
                    "avg_temp":  win["avg_temp"],
                    "avg_rain":  win["avg_rain"],
                },
                "local_weather": worst_local,
                "bad_local":     bad_local,
                "score":         score,
                "day_previews":  win["day_previews"],
                "flights_out":   mock_flights(dest, start_date),
                "flights_ret":   mock_flights(dest, end_date),
            })

    results.sort(key=lambda x: -x["score"])
    return results
