import os

TOMORROW_API_KEY = os.environ.get("TOMORROW_API_KEY", "ZM2vE4vcJObGcYh8PSRXWA9l2J7F0q5i")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "") # Insert Claude API key here

# Cache TTL in seconds — avoids burning API quota during development
CACHE_TTL_CURRENT = 5 * 60       # 5 min for realtime
CACHE_TTL_FORECAST = 30 * 60     # 30 min for forecasts