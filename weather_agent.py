"""
weather_agent.py — Agentic weather assistant powered by Claude.

get_agent_suggestions(location)
    Fast one-shot briefing: pre-fetches weather data, single Claude call, ~2s.

get_chat_response(location, user_message, history)
    Tool-use loop for follow-up chat questions.

Flask wiring in app.py:
    from weather_agent import register_agent_routes
    register_agent_routes(app)
"""

import json
import sys

import anthropic
from config import ANTHROPIC_API_KEY
from weather_client import WeatherClient

# ── Scoring helpers ────────────────────────────────────────────────────────────

_EMOJI_MAP = {
    1000:"☀️", 1100:"🌤️", 1101:"⛅", 1102:"🌥️", 1001:"☁️",
    2000:"🌫️", 2100:"🌫️",
    4000:"🌦️", 4001:"🌧️", 4200:"🌦️", 4201:"🌧️",
    5000:"❄️",  5001:"🌨️", 5100:"🌨️", 5101:"❄️",
    6000:"🌧️", 6001:"🌧️", 6200:"🌧️", 6201:"🌧️",
    8000:"⛈️",
}
_WEATHER_CODES = {
    1000:"Clear", 1100:"Mostly Clear", 1101:"Partly Cloudy", 1102:"Mostly Cloudy",
    1001:"Cloudy", 2000:"Fog", 2100:"Light Fog", 4000:"Drizzle", 4001:"Rain",
    4200:"Light Rain", 4201:"Heavy Rain", 5000:"Snow", 5001:"Flurries",
    5100:"Light Snow", 5101:"Heavy Snow", 6000:"Freezing Drizzle",
    6001:"Freezing Rain", 6200:"Light Freezing Rain", 6201:"Heavy Freezing Rain",
    8000:"Thunderstorm",
}

def _emoji(code):    return _EMOJI_MAP.get(code, "🌡️")
def _condition(code): return _WEATHER_CODES.get(code, f"Code {code}")
def _clamp(v):       return max(0, min(100, round(v)))

def _score_day(d):
    rain, temp, tmax, wind, uv, snow = (
        d.precipitation_probability_avg, d.temperature_avg, d.temperature_max,
        d.wind_speed_avg, d.uv_index_max, d.snow_accumulation,
    )
    return {
        "outdoor_event": _clamp(100 - max(0,rain-15)*3   - abs(temp-18)*2.5 - max(0,wind-5)*5  - (30 if tmax<12 else 0)),
        "jogging":       _clamp(100 - max(0,rain-25)*2.5 - abs(temp-13)*2   - max(0,wind-10)*3 - (40 if temp<0 else 0)),
        "bbq":           _clamp(100 - max(0,rain-10)*5   - abs(temp-22)*3   - max(0,wind-5)*6  - (50 if tmax<16 else 0)),
        "cycling":       _clamp(100 - max(0,rain-20)*3   - abs(temp-16)*2   - max(0,wind-12)*3 - (30 if temp<5 else 0)),
        "stay_inside":   _clamp(rain*0.7 + max(0,wind-15)*2 + (20 if tmax<2 else 0) + (20 if snow>5 else 0)),
        "terrace":       _clamp(100 - max(0,rain-10)*5   - abs(temp-20)*2   - max(0,wind-6)*5  - (20 if uv>8 else 0)),
    }

def _health_alerts(d):
    m = d.time.month
    return {
        "pollen":   10<=d.temperature_avg<=25 and d.precipitation_probability_avg<30 and 2<=d.wind_speed_avg<=18 and 3<=m<=9,
        "uv_high":  d.uv_index_max >= 6,
        "humidity": d.humidity_avg >= 70,
    }


# ── Clients ────────────────────────────────────────────────────────────────────

_weather_client   = WeatherClient()
_anthropic_client = None
MODEL = "claude-sonnet-4-6"

def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


# ── Tool schemas (for chat) ────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_current_weather",
        "description": "Fetch live current conditions: temperature, feels-like, humidity, rain probability, wind, UV index.",
        "input_schema": {"type":"object","properties":{"location":{"type":"string"}},"required":["location"]},
    },
    {
        "name": "get_daily_forecast",
        "description": "Fetch daily forecast for 1–5 days with activity scores (0–100) for outdoor_event, jogging, bbq, cycling, terrace, stay_inside.",
        "input_schema": {"type":"object","properties":{"location":{"type":"string"},"days":{"type":"integer","minimum":1,"maximum":5}},"required":["location"]},
    },
    {
        "name": "get_hourly_forecast",
        "description": "Fetch hour-by-hour weather for the next 24 hours.",
        "input_schema": {"type":"object","properties":{"location":{"type":"string"}},"required":["location"]},
    },
    {
        "name": "get_travel_recommendations",
        "description": "Find best travel escape destinations with weather scores, date ranges, and mock flight prices in GBP. Use when user asks about weekend trips or escaping bad weather.",
        "input_schema": {"type":"object","properties":{"location":{"type":"string"},"max_results":{"type":"integer","minimum":1,"maximum":8}},"required":["location"]},
    },
    {
        "name": "get_local_events",
        "description": "Get weather-scored local events (sport, music, festivals) and pub happy hour windows. Use when user asks about things to do, events, or happy hours.",
        "input_schema": {"type":"object","properties":{"location":{"type":"string"}},"required":["location"]},
    },
]


# ── Tool implementations ───────────────────────────────────────────────────────

def _tool_current_weather(location):
    data = _weather_client.get_forecast(location, days=1)
    c = data.current
    return {"location":data.location_name,"emoji":_emoji(c.weather_code),"condition":c.condition,
            "temperature_c":round(c.temperature,1),"feels_like_c":round(c.temperature_apparent,1),
            "humidity_pct":round(c.humidity),"rain_prob_pct":round(c.precipitation_probability),
            "wind_speed_ms":round(c.wind_speed,1),"uv_index":round(c.uv_index)}

def _tool_daily_forecast(location, days=5):
    data = _weather_client.get_forecast(location, days=days)
    result = []
    for d in data.daily:
        scores = _score_day(d)
        alerts = _health_alerts(d)
        best   = max((k for k in scores if k!="stay_inside"), key=lambda k: scores[k])
        result.append({"date":d.time.strftime("%Y-%m-%d"),"day_name":d.time.strftime("%A"),
                        "emoji":_emoji(d.weather_code_max),"condition":_condition(d.weather_code_max),
                        "temp_min_c":round(d.temperature_min,1),"temp_max_c":round(d.temperature_max,1),
                        "rain_prob_pct":round(d.precipitation_probability_avg),"wind_avg_ms":round(d.wind_speed_avg,1),
                        "uv_index_max":round(d.uv_index_max),"activity_scores":scores,
                        "best_activity":best,"health_alerts":alerts})
    return {"location":data.location_name,"days":result}

def _tool_hourly_forecast(location):
    data = _weather_client.get_forecast(location, days=1)
    return {"location":data.location_name,"hours":[
        {"time":h.time.strftime("%H:%M"),"emoji":_emoji(h.weather_code),"condition":h.condition,
         "temp_c":round(h.temperature,1),"rain_prob_pct":round(h.precipitation_probability),
         "wind_ms":round(h.wind_speed,1),"good_outdoor":h.is_good_outdoor()}
        for h in data.hourly[:24]
    ]}

def _tool_travel_recommendations(location, max_results=4):
    from travel import get_travel_recommendations
    data = _weather_client.get_forecast(location, days=5)
    local_daily = {
        d.time.strftime("%Y-%m-%d"): {
            "condition":d.condition,"emoji":_emoji(d.weather_code_max),
            "temp_max":round(d.temperature_max,1),"temp_min":round(d.temperature_min,1),
            "rain_prob":round(d.precipitation_probability_avg),
        }
        for d in data.daily
    }
    recs = get_travel_recommendations(local_daily)
    results = []
    for r in recs[:max_results]:
        f = r["flights_out"][0] if r.get("flights_out") else None
        results.append({
            "destination":r["destination"],"country":r["country"],"duration":r["duration_label"],
            "dates":r["date_range"],"score":r["score"],"bad_local":r["bad_local"],
            "dest_weather":r["dest_weather"],
            "cheapest_flight":{"airline":f["airline"],"price_gbp":f["price"],"duration":f["duration"],"stops":f["stops"]} if f else None,
        })
    return {"home_location":data.location_name,"recommendations":results}

def _tool_local_events(location):
    from events import get_events_with_weather, get_happy_hour_windows
    data = _weather_client.get_forecast(location, days=5)
    return {
        "events":       get_events_with_weather(data.hourly, days=5)[:8],
        "happy_hours":  get_happy_hour_windows(data.hourly, days=5)[:6],
    }

def _execute_tool(name, inputs):
    try:
        if   name == "get_current_weather":        result = _tool_current_weather(inputs["location"])
        elif name == "get_daily_forecast":         result = _tool_daily_forecast(inputs["location"], inputs.get("days",5))
        elif name == "get_hourly_forecast":        result = _tool_hourly_forecast(inputs["location"])
        elif name == "get_travel_recommendations": result = _tool_travel_recommendations(inputs["location"], inputs.get("max_results",4))
        elif name == "get_local_events":           result = _tool_local_events(inputs["location"])
        else:                                      result = {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        result = {"error": str(exc)}
    return json.dumps(result)


# ── System prompts ─────────────────────────────────────────────────────────────

# Used by get_agent_suggestions — data is pre-fetched and passed directly,
# so NO tools needed. Prompt must NOT instruct Claude to call tools.
_BRIEFING_SYSTEM = """You are the voice of a personalised weather news service — warm, sharp, and useful.

The user will send you pre-fetched weather data. Write a brief based on it — do NOT attempt to call any tools.

## Output — write a news brief, not a document

**{weather emoji} {One-line headline about today — max 12 words, specific and vivid}**

{2–3 sentences of body. Cover: what it feels like right now, the key thing to wear or carry today, and one health note if any alert is flagged. Prose only — no bullet points. Warm and direct.}

**Best this week:** {Day} — {one sentence on why and what it's ideal for.} {Omit if no day scores above 55.}

**Avoid:** {Day} — {one line.} {Omit if nothing is notably bad.}

**✈️ Escape this weekend?** {One sentence pointing to the Travel tab.} {Only if both weekend days have rain > 55% or temp_max < 10°C.}

## Rules
- Lead with today — the headline is the most important line.
- Specific beats vague: "18°C and breezy" beats "mild".
- No bullet points anywhere in your response.
- Never use em dashes (—). Use a comma, colon, or regular hyphen instead.
- Never use markdown tables or pipe/dash formatting.
- Total length: 60–100 words."""

# Used by get_chat_response — full tool-use loop.
_CHAT_SYSTEM = """You are a friendly weather assistant in a weather app chat panel.
The user has already seen a weather briefing for their location.
Use the weather tools whenever you need data. Use get_travel_recommendations when asked
about escaping or weekend trips. Use get_local_events when asked about things to do,
events, pub happy hours, or weekend plans.
Keep replies concise — 2–6 sentences unless a list genuinely helps.
Never use em dashes (—) or markdown tables."""


# ── Agentic loop (chat only) ───────────────────────────────────────────────────

def _run_agent(system, messages):
    resolved = ""
    while True:
        response = _get_anthropic().messages.create(
            model=MODEL, max_tokens=1024, system=system,
            tools=TOOLS, messages=messages,
        )
        messages.append({"role":"assistant","content":response.content})

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b,"text")), "")
            return text, resolved

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                raw = _execute_tool(block.name, block.input)
                try:
                    parsed = json.loads(raw)
                    loc = parsed.get("location") or parsed.get("home_location","")
                    if loc:
                        resolved = loc
                except Exception:
                    pass
                tool_results.append({"type":"tool_result","tool_use_id":block.id,"content":raw})
            messages.append({"role":"user","content":tool_results})
        else:
            break
    return "Unable to generate a response.", resolved


# ── Public API ─────────────────────────────────────────────────────────────────

def stream_agent_suggestions(location: str):
    """Generator yielding JSON-encoded chunks for SSE streaming."""
    data = _weather_client.get_forecast(location, days=5)
    c    = data.current
    lines = [
        f"LOCATION: {data.location_name}",
        f"NOW: {c.condition} | {c.temperature:.1f}C (feels {c.temperature_apparent:.1f}C) | "
        f"Rain {c.precipitation_probability:.0f}% | Wind {c.wind_speed:.1f} m/s | "
        f"UV {c.uv_index:.0f} | Humidity {c.humidity:.0f}%",
        "",
        "5-DAY FORECAST:",
    ]
    for d in data.daily:
        scores = _score_day(d)
        alerts = _health_alerts(d)
        flags  = [k for k, v in alerts.items() if v]
        best   = max((k for k in scores if k != "stay_inside"), key=lambda k: scores[k])
        lines.append(
            f"{d.time.strftime('%A')} {d.time.day} {d.time.strftime('%b')}: "
            f"{d.condition} | {d.temperature_min:.0f}-{d.temperature_max:.0f}C | "
            f"Rain {d.precipitation_probability_avg:.0f}% | Wind {d.wind_speed_avg:.1f} m/s | UV {d.uv_index_max:.0f}"
        )
        lines.append(
            f"  Scores: outdoor={scores['outdoor_event']} jog={scores['jogging']} "
            f"bbq={scores['bbq']} cycle={scores['cycling']} terrace={scores['terrace']} "
            f"inside={scores['stay_inside']} | Best: {best}"
            + (f" | Alerts: {', '.join(flags)}" if flags else "")
        )
    context = "\n".join(lines)
    yield json.dumps({"location": data.location_name})
    with _get_anthropic().messages.stream(
        model=MODEL,
        max_tokens=400,
        system=_BRIEFING_SYSTEM,
        messages=[{"role": "user", "content": f"Here is the weather data:\n\n{context}\n\nWrite today\'s brief."}],
    ) as stream:
        for chunk in stream.text_stream:
            yield json.dumps({"text": chunk})


def get_agent_suggestions(location: str) -> dict:
    """
    Fast briefing: pre-fetch data → single Claude call (no tool round-trips) → ~2s.
    """
    data = _weather_client.get_forecast(location, days=5)
    c    = data.current

    lines = [
        f"LOCATION: {data.location_name}",
        f"NOW: {c.condition} | {c.temperature:.1f}°C (feels {c.temperature_apparent:.1f}°C) | "
        f"Rain {c.precipitation_probability:.0f}% | Wind {c.wind_speed:.1f} m/s | "
        f"UV {c.uv_index:.0f} | Humidity {c.humidity:.0f}%",
        "",
        "5-DAY FORECAST:",
    ]
    for d in data.daily:
        scores = _score_day(d)
        alerts = _health_alerts(d)
        flags  = [k for k, v in alerts.items() if v]
        best   = max((k for k in scores if k != "stay_inside"), key=lambda k: scores[k])
        lines.append(
            f"{d.time.strftime('%A')} {d.time.day} {d.time.strftime('%b')}: "
            f"{d.condition} | {d.temperature_min:.0f}–{d.temperature_max:.0f}°C | "
            f"Rain {d.precipitation_probability_avg:.0f}% | Wind {d.wind_speed_avg:.1f} m/s | UV {d.uv_index_max:.0f}"
        )
        lines.append(
            f"  Scores: outdoor={scores['outdoor_event']} jog={scores['jogging']} "
            f"bbq={scores['bbq']} cycle={scores['cycling']} terrace={scores['terrace']} "
            f"inside={scores['stay_inside']} | Best: {best}"
            + (f" | Alerts: {', '.join(flags)}" if flags else "")
        )

    context = "\n".join(lines)

    response = _get_anthropic().messages.create(
        model=MODEL,
        max_tokens=400,
        system=_BRIEFING_SYSTEM,
        messages=[{"role":"user","content":f"Here is the weather data:\n\n{context}\n\nWrite today's brief."}],
    )
    text = next((b.text for b in response.content if hasattr(b,"text")), "No brief generated.")
    return {"text": text, "location": data.location_name}


def get_chat_response(location: str, user_message: str, history: list) -> dict:
    """Continue a conversation. Full tool-use loop."""
    messages = [{"role":"user","content":f"I'm looking at the weather for {location}."}]
    for item in history:
        messages.append({"role":item["role"],"content":item["text"]})
    messages.append({"role":"user","content":user_message})
    text, resolved = _run_agent(_CHAT_SYSTEM, messages)
    return {"text": text, "location": resolved or location}


# ── Flask integration ──────────────────────────────────────────────────────────

def register_agent_routes(app):
    from flask import request, jsonify

    @app.route("/api/agent-suggestions")
    def api_agent_suggestions():
        location = request.args.get("location", "Oslo")
        try:
            return jsonify(get_agent_suggestions(location))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/agent-stream")
    def api_agent_stream():
        from flask import Response, stream_with_context
        location = request.args.get("location", "Oslo")
        def generate():
            try:
                for chunk in stream_agent_suggestions(location):
                    yield f"data: {chunk}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            yield "data: [DONE]\n\n"
        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/api/agent-chat", methods=["POST"])
    def api_agent_chat():
        body     = request.get_json(force=True) or {}
        location = body.get("location","Oslo")
        message  = body.get("message","")
        history  = body.get("history",[])
        if not message:
            return jsonify({"error":"message is required"}), 400
        try:
            return jsonify(get_chat_response(location, message, history))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    loc = " ".join(sys.argv[1:]) or "Oslo"
    print(f"\nFetching briefing for '{loc}' …\n")
    result = get_agent_suggestions(loc)
    print(f"📍 {result['location']}\n{result['text']}")