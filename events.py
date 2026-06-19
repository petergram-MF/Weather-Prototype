"""
events.py — Local event discovery with weather-suitability scoring.

Generates realistic mock events based on day-of-week patterns, scores them
against the Tomorrow.io hourly forecast, and finds optimal beer garden /
happy hour windows for each pub in the area.
"""

import hashlib
from datetime import date, timedelta, datetime

# ── Weather code → emoji (reused from main app) ────────────────────────────────

_EMOJI_MAP = {
    1000: "☀️",  1100: "🌤️", 1101: "⛅",  1102: "🌥️", 1001: "☁️",
    2000: "🌫️", 2100: "🌫️",
    4000: "🌦️", 4001: "🌧️", 4200: "🌦️", 4201: "🌧️",
    5000: "❄️",  5001: "🌨️", 5100: "🌨️", 5101: "❄️",
    6000: "🌧️", 6001: "🌧️", 6200: "🌧️", 6201: "🌧️",
    8000: "⛈️",
}
def _wx_emoji(code): return _EMOJI_MAP.get(code, "🌡️")


# ── Recurring event templates ──────────────────────────────────────────────────
# (weekday 0=Mon…6=Sun, start_hour, template_dict)

_TEMPLATES = [
    # ── Football ──
    (5, 15, {"name": "Premier League",          "type": "football",  "emoji": "⚽", "duration_h": 2,   "outdoor": False}),
    (6, 14, {"name": "Premier League",          "type": "football",  "emoji": "⚽", "duration_h": 2,   "outdoor": False}),
    (1, 20, {"name": "Europa League",           "type": "football",  "emoji": "🏆", "duration_h": 2,   "outdoor": False}),
    (2, 20, {"name": "Champions League",        "type": "football",  "emoji": "🏆", "duration_h": 2,   "outdoor": False}),
    (6, 11, {"name": "Sunday League Football",  "type": "football",  "emoji": "⚽", "duration_h": 2,   "outdoor": True}),
    # ── Music ──
    (3, 20, {"name": "Live Jazz Night",         "type": "music",     "emoji": "🎷", "duration_h": 3,   "outdoor": False}),
    (4, 19, {"name": "Acoustic Night",          "type": "music",     "emoji": "🎸", "duration_h": 3,   "outdoor": False}),
    (5, 19, {"name": "Outdoor Concert",         "type": "music",     "emoji": "🎵", "duration_h": 4,   "outdoor": True}),
    (6, 18, {"name": "Summer Concert Series",   "type": "music",     "emoji": "🎤", "duration_h": 3,   "outdoor": True}),
    # ── Festivals & markets ──
    (5, 11, {"name": "Street Food Festival",    "type": "festival",  "emoji": "🍜", "duration_h": 8,   "outdoor": True}),
    (6,  9, {"name": "Farmers Market",          "type": "market",    "emoji": "🥦", "duration_h": 4,   "outdoor": True}),
    (6, 12, {"name": "Craft Beer Festival",     "type": "festival",  "emoji": "🎪", "duration_h": 7,   "outdoor": True}),
    # ── Sport & wellness ──
    (5,  9, {"name": "Park Run 5K",             "type": "sport",     "emoji": "🏃", "duration_h": 1,   "outdoor": True}),
    (6,  9, {"name": "Park Run 5K",             "type": "sport",     "emoji": "🏃", "duration_h": 1,   "outdoor": True}),
    (5, 13, {"name": "Cricket Afternoon",       "type": "sport",     "emoji": "🏏", "duration_h": 5,   "outdoor": True}),
    (6,  8, {"name": "Park Yoga Morning",       "type": "wellness",  "emoji": "🧘", "duration_h": 1.5, "outdoor": True}),
    (0,  7, {"name": "Morning Run Club",        "type": "sport",     "emoji": "🏃", "duration_h": 1,   "outdoor": True}),
]

# ── Pub data ───────────────────────────────────────────────────────────────────

PUBS = [
    {
        "name": "The Anchor", "emoji": "⚓", "beer_garden": True,
        "happy_hours": [
            {"weekdays": [0,1,2,3,4], "start_h": 17, "end_h": 19, "deal": "2-for-1 on draught"},
        ],
    },
    {
        "name": "The Fox & Hound", "emoji": "🦊", "beer_garden": True,
        "happy_hours": [
            {"weekdays": [0,1,2,3],   "start_h": 16, "end_h": 18, "deal": "£1 off all pints"},
            {"weekdays": [6],          "start_h": 13, "end_h": 16, "deal": "Sunday session deals"},
        ],
    },
    {
        "name": "The Tap Room", "emoji": "🍺", "beer_garden": True,
        "happy_hours": [
            {"weekdays": [0,1,2,3,4], "start_h": 17, "end_h": 20, "deal": "£4 craft pints"},
        ],
    },
    {
        "name": "The Crown", "emoji": "👑", "beer_garden": False,
        "happy_hours": [
            {"weekdays": [0,1,2,3,4], "start_h": 17, "end_h": 19, "deal": "£3.50 wine & cocktails"},
        ],
    },
    {
        "name": "The Biergarten", "emoji": "🏡", "beer_garden": True,
        "happy_hours": [
            {"weekdays": [4,5,6],     "start_h": 14, "end_h": 17, "deal": "Steins for £5"},
        ],
    },
]


# ── Scoring ────────────────────────────────────────────────────────────────────

def _score_event(template: dict, rain: float, temp: float, wind: float) -> int:
    if not template["outdoor"]:
        # Indoor events: weather mostly irrelevant, mild bonus for bad weather
        # (people more likely to seek indoor entertainment)
        score = 60 + min(20, rain * 0.3)
        return max(0, min(100, round(score)))

    # Outdoor events: weather is everything
    score = 100.0
    score -= max(0, rain  - 15) * 2.5   # rain is very bad
    score -= max(0, abs(temp - 17) - 4) * 2.5  # comfortable range 13–21°C
    score -= max(0, wind  - 12) * 3     # wind penalty
    if temp < 8:  score -= 30           # too cold
    if rain > 70: score -= 20           # probably cancelled
    return max(0, min(100, round(score)))


def _score_beer_garden(rain: float, temp: float) -> int:
    score = 100.0
    score -= max(0, rain - 10) * 4
    score -= max(0, 15 - temp) * 5
    score -= max(0, temp - 28) * 3      # too hot is bad too
    return max(0, min(100, round(score)))


def _recommendation(score: int, outdoor: bool) -> dict:
    if not outdoor:
        return {"label": "Great night out",    "color": "#d4edda", "text": "#155724"} if score >= 70 \
          else {"label": "Good option",         "color": "#fff3cd", "text": "#856404"}
    if score >= 75:
        return {"label": "Perfect conditions", "color": "#d4edda", "text": "#155724"}
    if score >= 50:
        return {"label": "Bring a jacket",     "color": "#fff3cd", "text": "#856404"}
    if score >= 30:
        return {"label": "Dress for rain",     "color": "#fde8d8", "text": "#9a3412"}
    return     {"label": "Likely cancelled",   "color": "#f8d7da", "text": "#721c24"}


def _hourly_avg(hours, attr):
    return sum(getattr(h, attr) for h in hours) / len(hours) if hours else None


# ── Public API ─────────────────────────────────────────────────────────────────

def get_events_with_weather(hourly_wx: list, days: int = 7) -> list:
    """
    Generate and weather-score events for the next `days` days.

    Args:
        hourly_wx: list of HourlyForecast objects from WeatherClient
        days: number of days ahead to look

    Returns:
        List of event dicts sorted by score (best first).
    """
    today = date.today()
    events = []

    for i in range(1, days + 1):
        event_date = today + timedelta(days=i)
        weekday    = event_date.weekday()
        date_str   = event_date.strftime("%Y-%m-%d")

        matching = [t for t in _TEMPLATES if t[0] == weekday]
        if not matching:
            continue

        # Deterministic selection: max 3 events per day to avoid clutter
        seed = int(hashlib.md5(date_str.encode()).hexdigest(), 16)
        if len(matching) > 3:
            idx = sorted(range(len(matching)), key=lambda x: (seed + x * 97) % 10000)[:3]
            matching = [matching[j] for j in idx]

        for _, start_h, tmpl in matching:
            end_h = start_h + int(tmpl["duration_h"]) + (1 if tmpl["duration_h"] % 1 else 0)
            event_hours = [
                h for h in hourly_wx
                if h.time.date() == event_date and start_h <= h.time.hour < end_h
            ]

            if event_hours:
                rain = _hourly_avg(event_hours, "precipitation_probability")
                temp = _hourly_avg(event_hours, "temperature")
                wind = _hourly_avg(event_hours, "wind_speed")
                wx_emoji = _wx_emoji(event_hours[0].weather_code)
            else:
                # Outside forecast window — use neutral values
                rain, temp, wind, wx_emoji = 30, 14, 5, "🌡️"

            score = _score_event(tmpl, rain, temp, wind)
            rec   = _recommendation(score, tmpl["outdoor"])
            dur_h = tmpl["duration_h"]

            events.append({
                "name":      tmpl["name"],
                "type":      tmpl["type"],
                "emoji":     tmpl["emoji"],
                "outdoor":   tmpl["outdoor"],
                "date":      date_str,
                "day_name":  event_date.strftime("%A"),
                "date_label": f"{event_date.strftime('%a')} {event_date.day} {event_date.strftime('%b')}",
                "time":      f"{start_h:02d}:00",
                "duration":  f"{int(dur_h)}h" if dur_h == int(dur_h) else f"{dur_h}h",
                "weather": {
                    "emoji":    wx_emoji,
                    "rain_prob": round(rain),
                    "temp":     round(temp, 1),
                    "wind":     round(wind, 1),
                },
                "score":          score,
                "recommendation": rec,
            })

    events.sort(key=lambda x: -x["score"])
    return events


def get_happy_hour_windows(hourly_wx: list, days: int = 7) -> list:
    """
    Find the best beer garden / happy hour windows for each pub over the next `days` days.

    Returns:
        List of window dicts sorted by score (best first), capped at 15.
    """
    today   = date.today()
    windows = []

    for pub in PUBS:
        for hh in pub["happy_hours"]:
            for i in range(1, days + 1):
                event_date = today + timedelta(days=i)
                if event_date.weekday() not in hh["weekdays"]:
                    continue

                hh_hours = [
                    h for h in hourly_wx
                    if h.time.date() == event_date
                    and hh["start_h"] <= h.time.hour < hh["end_h"]
                ]

                if hh_hours:
                    rain = _hourly_avg(hh_hours, "precipitation_probability")
                    temp = _hourly_avg(hh_hours, "temperature")
                else:
                    rain, temp = 30, 14     # outside window — neutral

                score          = _score_beer_garden(rain, temp)
                outdoor_viable = score >= 60 and pub["beer_garden"]

                windows.append({
                    "pub":          pub["name"],
                    "emoji":        pub["emoji"],
                    "beer_garden":  pub["beer_garden"],
                    "deal":         hh["deal"],
                    "date":         event_date.strftime("%Y-%m-%d"),
                    "day_name":     event_date.strftime("%A"),
                    "date_label":   f"{event_date.strftime('%a')} {event_date.day} {event_date.strftime('%b')}",
                    "time":         f"{hh['start_h']:02d}:00–{hh['end_h']:02d}:00",
                    "weather": {
                        "rain_prob": round(rain),
                        "temp":      round(temp, 1),
                    },
                    "score":          score,
                    "outdoor_viable": outdoor_viable,
                })

    windows.sort(key=lambda x: -x["score"])
    return windows[:15]