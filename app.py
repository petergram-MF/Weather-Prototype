import os
import threading
import webbrowser


from weather_agent import register_agent_routes
from flask import Flask, render_template, jsonify, request
from weather_client import WeatherClient, DailyForecast
from travel import get_travel_recommendations, fetch_destination_weather
from events import get_events_with_weather, get_happy_hour_windows

app = Flask(__name__)
register_agent_routes(app)
client = WeatherClient()

# ── Weather emoji map ──────────────────────────────────────────────────────────

EMOJI = {
    1000: "☀️", 1100: "🌤️", 1101: "⛅", 1102: "🌥️", 1001: "☁️",
    2000: "🌫️", 2100: "🌫️",
    4000: "🌦️", 4001: "🌧️", 4200: "🌦️", 4201: "🌧️",
    5000: "❄️", 5001: "🌨️", 5100: "🌨️", 5101: "❄️",
    6000: "🌧️", 6001: "🌧️", 6200: "🌧️", 6201: "🌧️",
    8000: "⛈️",
}

def emoji(code: int) -> str:
    return EMOJI.get(code, "🌡️")


# ── Activity scoring ───────────────────────────────────────────────────────────
# Each function returns 0–100; higher = better day for that activity.

def score_day(day: DailyForecast) -> dict:
    rain  = day.precipitation_probability_avg
    temp  = day.temperature_avg
    tmax  = day.temperature_max
    wind  = day.wind_speed_avg
    uv    = day.uv_index_max
    snow  = day.snow_accumulation

    def clamp(v: float) -> int:
        return max(0, min(100, round(v)))

    return {
        # Warm, dry, calm – perfect for hosting guests outside
        "outdoor_event": clamp(
            100 - max(0, rain - 15) * 3
                - abs(temp - 18) * 2.5
                - max(0, wind - 5) * 5
                - (30 if tmax < 12 else 0)
        ),
        # Cool-comfortable, not too wet or gusty
        "jogging": clamp(
            100 - max(0, rain - 25) * 2.5
                - abs(temp - 13) * 2
                - max(0, wind - 10) * 3
                - (40 if temp < 0 else 0)
        ),
        # Hot, bone-dry, near-still air
        "bbq": clamp(
            100 - max(0, rain - 10) * 5
                - abs(temp - 22) * 3
                - max(0, wind - 5) * 6
                - (50 if tmax < 16 else 0)
        ),
        # Comfortable pace, moderate wind/rain tolerance
        "cycling": clamp(
            100 - max(0, rain - 20) * 3
                - abs(temp - 16) * 2
                - max(0, wind - 12) * 3
                - (30 if temp < 5 else 0)
        ),
        # High rain, extreme cold, storms push people indoors
        "stay_inside": clamp(
            rain * 0.7
            + max(0, wind - 15) * 2
            + (20 if tmax < 2 else 0)
            + (20 if snow > 5 else 0)
        ),
        # Warm, sunny, very calm – ideal for sitting outside at a café
        "terrace": clamp(
            100 - max(0, rain - 10) * 5
                - abs(temp - 20) * 2
                - max(0, wind - 6) * 5
                - (20 if uv > 8 else 0)
        ),
    }


# Display metadata for each activity
ACTIVITIES = {
    "outdoor_event": ("🎉", "Outdoor Event"),
    "jogging":       ("🏃", "Jogging"),
    "bbq":           ("🔥", "BBQ"),
    "cycling":       ("🚴", "Cycling"),
    "stay_inside":   ("🏠", "Stay Inside"),
    "terrace":       ("☕", "Terrace / Café"),
}


# ── Hourly activity suggestions (for week view) ────────────────────────────────

# (label, color, slot_start, slot_end, min_temp, max_rain, max_wind)
HOUR_ACTS = [
    ("🏃 Morning Run",    "#28a745",  5,  9,  2, 35, 14),
    ("🚴 Cycling",        "#17a2b8",  7, 10,  8, 20, 10),
    ("🥾 Hiking",         "#6f42c1",  9, 13,  8, 20, 15),
    ("☕ Outdoor Café",   "#fd7e14", 10, 13, 14, 15,  8),
    ("🍽️ Lunch Outside",  "#e6a817", 11, 14, 16, 12,  6),
    ("🧺 Picnic",         "#20c997", 12, 16, 18, 10,  6),
    ("⚽ Team Sports",    "#007bff", 14, 18, 10, 30, 12),
    ("🔥 BBQ",            "#dc3545", 15, 20, 16, 10,  6),
    ("🏃 Evening Run",    "#28a745", 17, 21,  5, 35, 14),
    ("🌅 Sunset Walk",    "#e83e8c", 18, 21, 10, 25, 10),
]


def hour_outdoor_score(h) -> int:
    score = 100 - max(0, h.precipitation_probability - 20) * 2 \
                - abs(h.temperature - 16) * 2 \
                - max(0, h.wind_speed - 10) * 4
    return max(0, min(100, round(score)))


def hour_color(h) -> str:
    s = hour_outdoor_score(h)
    if s >= 65:
        return "#d4edda"
    if s >= 35:
        return "#fff3cd"
    return "#f8d7da"


def suggest_hour_activities(h) -> list[dict]:
    hour = h.time.hour
    results = []
    for label, color, start, end, min_t, max_r, max_w in HOUR_ACTS:
        if (start <= hour < end
                and h.temperature >= min_t
                and h.precipitation_probability <= max_r
                and h.wind_speed <= max_w):
            results.append({"title": label, "color": color})
    return results[:2]  # cap at 2 per hour


def health_alerts(day: DailyForecast) -> dict:
    month = day.time.month
    pollen = (
        10 <= day.temperature_avg <= 25
        and day.precipitation_probability_avg < 30
        and 2 <= day.wind_speed_avg <= 18
        and 3 <= month <= 9
    )
    return {
        "pollen":   pollen,
        "uv_high":  day.uv_index_max >= 6,
        "humidity": day.humidity_avg >= 70,
    }


def day_color(scores: dict) -> str:
    """Traffic-light background for the calendar cell."""
    avg = (scores["outdoor_event"] + scores["jogging"] + scores["cycling"] + scores["terrace"]) / 4
    if avg >= 65:
        return "#d4edda"   # green
    if avg >= 35:
        return "#fff3cd"   # amber
    return "#f8d7da"        # red


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("calendar.html")


@app.route("/api/forecast")
def api_forecast():
    """Return current conditions + per-day weather & activity scores."""
    location = request.args.get("location", "Oslo")
    try:
        data = client.get_forecast(location, days=5)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    days = {}
    for d in data.daily:
        scores = score_day(d)
        days[d.time.strftime("%Y-%m-%d")] = {
            "emoji":     emoji(d.weather_code_max),
            "condition": d.condition,
            "temp_min":  round(d.temperature_min, 1),
            "temp_max":  round(d.temperature_max, 1),
            "rain_prob": round(d.precipitation_probability_avg),
            "wind":      round(d.wind_speed_avg, 1),
            "uv":        round(d.uv_index_max),
            "humidity":  round(d.humidity_avg),
            "snow":      round(d.snow_accumulation, 1),
            "color":     day_color(scores),
            "scores":    scores,
            "alerts":    health_alerts(d),
        }

    # Extended forecast: Open-Meteo days beyond Tomorrow.io's 5-day window
    extended = {}
    if data.latitude and data.longitude:
        try:
            om = fetch_destination_weather(data.latitude, data.longitude, days=14)
            for date_str, wx in om.items():
                if date_str not in days:
                    rain = wx["rain_prob"]
                    temp = wx["temp_max"]
                    # Lighter palette signals lower forecast confidence
                    score = max(0, 100 - max(0, rain - 15) * 2 - abs(temp - 18) * 1.5)
                    if score >= 65:
                        color = "#edf7ee"
                    elif score >= 35:
                        color = "#fffef0"
                    else:
                        color = "#fdf0f1"
                    extended[date_str] = {
                        "emoji":     wx["emoji"],
                        "condition": wx["condition"],
                        "temp_min":  wx["temp_min"],
                        "temp_max":  wx["temp_max"],
                        "rain_prob": wx["rain_prob"],
                        "color":     color,
                        "scores":    {},
                        "extended":  True,
                    }
        except Exception:
            pass

    return jsonify({
        "location": data.location_name,
        "current": {
            "emoji":     emoji(data.current.weather_code),
            "condition": data.current.condition,
            "temp":      round(data.current.temperature, 1),
            "rain_prob": round(data.current.precipitation_probability),
            "wind":      round(data.current.wind_speed, 1),
            "humidity":  round(data.current.humidity),
            "uv":        round(data.current.uv_index),
        },
        "days": {**days, **extended},
    })


@app.route("/api/day-detail")
def api_day_detail():
    """Hourly breakdown + activity scores for a single day."""
    location = request.args.get("location", "Oslo")
    date_str = request.args.get("date", "")
    try:
        data = client.get_forecast(location, days=5)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    from datetime import date as date_t
    target = date_t.fromisoformat(date_str) if date_str else None

    hours = [
        {
            "time":      h.time.strftime("%H:%M"),
            "emoji":     emoji(h.weather_code),
            "condition": h.condition,
            "temp":      round(h.temperature, 1),
            "rain_prob": round(h.precipitation_probability),
            "wind":      round(h.wind_speed, 1),
            "good":      h.is_good_outdoor(),
        }
        for h in data.hourly
        if target is None or h.time.date() == target
    ]

    day = next((d for d in data.daily if target and d.time.date() == target), None)
    scores = score_day(day) if day else {}

    activities = sorted(
        [
            {"key": k, "emoji": em, "label": lbl, "score": scores.get(k, 0)}
            for k, (em, lbl) in ACTIVITIES.items()
        ],
        key=lambda x: -x["score"],
    )

    return jsonify({"hours": hours, "activities": activities})


@app.route("/api/best-days")
def api_best_days():
    """Return forecast days ranked by suitability for a given activity."""
    location = request.args.get("location", "Oslo")
    activity = request.args.get("activity", "outdoor_event")
    try:
        data = client.get_forecast(location, days=5)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    days = sorted(
        [
            {
                "date":       d.time.strftime("%Y-%m-%d"),
                "date_label": f"{d.time.strftime('%a')} {d.time.day} {d.time.strftime('%b')}",
                "score":      score_day(d).get(activity, 0),
                "emoji":      emoji(d.weather_code_max),
                "condition":  d.condition,
                "temp_min":   round(d.temperature_min),
                "temp_max":   round(d.temperature_max),
                "rain_prob":  round(d.precipitation_probability_avg),
            }
            for d in data.daily
        ],
        key=lambda x: -x["score"],
    )

    em, lbl = ACTIVITIES.get(activity, ("📅", activity))
    return jsonify({"days": days, "activity_emoji": em, "activity_label": lbl})


@app.route("/api/calendar-events")
def api_calendar_events():
    """
    Returns FullCalendar-format events for week/day views:
    - background events per hour (colored by outdoor quality)
    - activity suggestion events (max 3 per day, one per morning/afternoon/evening slot)
    """
    from datetime import timedelta
    from collections import defaultdict

    location = request.args.get("location", "Oslo")
    try:
        data = client.get_forecast(location, days=5)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    background = []
    activities  = []
    # Limit to one activity suggestion per time-of-day period per day
    period_used: dict[str, set] = defaultdict(set)

    for h in data.hourly:
        start_str = h.time.isoformat()
        end_str   = (h.time + timedelta(hours=1)).isoformat()
        date_str  = h.time.date().isoformat()
        hour      = h.time.hour

        background.append({
            "start": start_str, "end": end_str,
            "display": "background",
            "backgroundColor": hour_color(h),
        })

        # Time-of-day buckets: morning / afternoon / evening
        if 5 <= hour < 11:
            period = "morning"
        elif 11 <= hour < 16:
            period = "afternoon"
        elif 16 <= hour < 22:
            period = "evening"
        else:
            continue

        if period in period_used[date_str]:
            continue

        for act in suggest_hour_activities(h):
            activities.append({
                "title":           act["title"],
                "start":           start_str,
                "end":             end_str,
                "backgroundColor": act["color"],
                "borderColor":     act["color"],
                "textColor":       "#fff",
                "classNames":      ["act-event"],
            })
            period_used[date_str].add(period)
            break  # one activity per period

    return jsonify({"background": background, "activities": activities})


@app.route("/api/travel")
def api_travel():
    """Return weekend escape recommendations — bad local weather vs sunny destinations."""
    location = request.args.get("location", "Oslo")
    try:
        data = client.get_forecast(location, days=5)
        local_daily = {
            d.time.strftime("%Y-%m-%d"): {
                "condition": d.condition,
                "emoji":     emoji(d.weather_code_max),
                "temp_max":  round(d.temperature_max, 1),
                "temp_min":  round(d.temperature_min, 1),
                "rain_prob": round(d.precipitation_probability_avg),
            }
            for d in data.daily
        }
        recs = get_travel_recommendations(local_daily)
        return jsonify({"recommendations": recs, "location": data.location_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/events")
def api_events():
    location = request.args.get("location", "Oslo")
    try:
        data = client.get_forecast(location, days=7)
        events      = get_events_with_weather(data.hourly, days=7)
        happy_hours = get_happy_hour_windows(data.hourly, days=7)
        return jsonify({
            "location":    data.location_name,
            "events":      events,
            "happy_hours": happy_hours,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    
if __name__ == "__main__":
    # Open the browser once the server is ready.
    # Guard against the Werkzeug reloader spawning a second browser window.
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        def _open():
            import time
            time.sleep(1.2)
            webbrowser.open("http://localhost:5000")
        threading.Thread(target=_open, daemon=True).start()

    app.run(debug=True, port=5000)
