"""Environment configuration for the Discord agent."""
import os


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Discord
DISCORD_TOKEN = _get("DISCORD_TOKEN")
DISCORD_GUILD_ID = _get("DISCORD_GUILD_ID")  # optional — speeds up slash registration
DISCORD_BRIEFING_CHANNEL_ID = _get("DISCORD_BRIEFING_CHANNEL_ID")
DISCORD_ALERT_CHANNEL_ID = _get("DISCORD_ALERT_CHANNEL_ID")

# Anthropic (intelligence engine)
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
INTELLIGENCE_MODEL = _get("INTELLIGENCE_MODEL", "claude-opus-4-6")

# Backend (where the existing FastAPI app runs — used to pull live data)
BACKEND_URL = _get("BACKEND_URL", "http://127.0.0.1:8000")

# Scheduling
PREMARKET_HOUR_ET = int(_get("PREMARKET_HOUR_ET", "7"))   # 7am ET briefing
WRAP_HOUR_ET = int(_get("WRAP_HOUR_ET", "16"))             # 4pm ET wrap
WRAP_MINUTE_ET = int(_get("WRAP_MINUTE_ET", "30"))


def assert_configured() -> None:
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        raise RuntimeError(
            "Missing required env vars: " + ", ".join(missing) +
            ".  See discord_bot/README.md for setup."
        )
