"""
Reality-check context sources for prediction-market pairs.

Pulls supporting context from external sources so each card can show users
*why* a market might be mispriced rather than just two competing prices.

All fetches are wrapped to NEVER throw — if a source is down, the card just
omits that block. Results are cached aggressively (TTL).
"""
import os
import re
import time
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional

UA = {"User-Agent": "conviction-capital/1.0"}
TIMEOUT = 10

# ── In-memory caches ─────────────────────────────────────────────────────────
# Keyed by source; values: (fetched_at_epoch, payload)
_CACHE: dict = {}
_TTL = {
    "manifold":   30 * 60,        # 30 min
    "fred":       6  * 3600,      # 6 h
    "open_meteo": 60 * 60,        # 1 h
    "noaa_storms": 30 * 60,       # 30 min
}


def _cache_get(key: str):
    e = _CACHE.get(key)
    if not e:
        return None
    ts, payload = e
    ttl = _TTL.get(key.split(":")[0], 1800)
    if time.time() - ts > ttl:
        return None
    return payload


def _cache_put(key: str, payload):
    _CACHE[key] = (time.time(), payload)


# ════════════════════════════════════════════════════════════════════════════
# 1. MANIFOLD MARKETS — third prediction market for cross-validation
# ════════════════════════════════════════════════════════════════════════════
MANIFOLD_BASE = "https://api.manifold.markets/v0"


def fetch_manifold_markets(limit: int = 1000) -> list[dict]:
    """
    Pull active binary markets from Manifold for three-way comparison.
    Manifold is play-money but very well-calibrated due to active community.
    """
    cached = _cache_get("manifold:all")
    if cached:
        return cached

    out = []
    before = None
    pages = 0
    while pages < 5 and len(out) < limit:
        params = {"limit": 200}
        if before:
            params["before"] = before
        try:
            r = requests.get(f"{MANIFOLD_BASE}/markets", params=params,
                             headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"[manifold] fetch error: {e}")
            break
        if not batch:
            break
        for m in batch:
            if m.get("isResolved"):
                continue
            if m.get("outcomeType") != "BINARY":
                continue
            prob = m.get("probability")
            if prob is None or not (0.01 <= prob <= 0.99):
                continue
            out.append({
                "id":       m.get("id", ""),
                "title":    m.get("question", ""),
                "yes_prob": float(prob),
                "volume":   float(m.get("volume", 0) or 0),
                "url":      m.get("url", ""),
                "close":    _ms_to_iso(m.get("closeTime")),
            })
        before = batch[-1].get("id") if batch else None
        pages += 1
        time.sleep(0.15)

    _cache_put("manifold:all", out)
    return out


def _ms_to_iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def find_manifold_match(title: str, manifold: list[dict],
                        min_score: float = 0.45) -> Optional[dict]:
    """Find the best-matching Manifold market for a given title."""
    if not title or not manifold:
        return None
    best = None
    best_score = 0.0
    for mf in manifold:
        s = _title_similarity(title, mf["title"])
        if s > best_score:
            best_score = s
            best = mf
    if best and best_score >= min_score:
        return {**best, "match_score": round(best_score, 2)}
    return None


def _title_similarity(a: str, b: str) -> float:
    """Lightweight Jaccard for matching against Manifold."""
    norm = lambda s: set(re.findall(r"[a-z0-9]{3,}", s.lower())) - {
        "the", "and", "for", "from", "this", "that", "will", "what", "when",
        "with", "have", "has", "are", "was", "yes", "win", "win?",
    }
    ta, tb = norm(a), norm(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ════════════════════════════════════════════════════════════════════════════
# 2. OPEN-METEO — free weather forecasts for weather markets
# ════════════════════════════════════════════════════════════════════════════
# We pull a global hurricane/storm reference from NOAA's NHC active storms feed
# and per-location forecasts from Open-Meteo.

NHC_ACTIVE = "https://www.nhc.noaa.gov/CurrentStorms.json"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

# Rough geocode for the locations most weather markets reference
_LOC_GEO = {
    "miami":      (25.76, -80.19),
    "florida":    (27.76, -81.69),
    "texas":      (31.97, -99.90),
    "houston":    (29.76, -95.37),
    "new york":   (40.71, -74.01),
    "los angeles":(34.05,-118.24),
    "chicago":    (41.88, -87.63),
    "atlanta":    (33.75, -84.39),
    "boston":     (42.36, -71.06),
    "dallas":     (32.78, -96.80),
    "phoenix":    (33.45,-112.07),
    "denver":     (39.74,-104.99),
    "seattle":    (47.61,-122.33),
    "london":     (51.51,  -0.13),
    "tokyo":      (35.68, 139.76),
    "paris":      (48.86,   2.35),
}


def get_active_hurricanes() -> list[dict]:
    """Pull active named storms from the National Hurricane Center."""
    cached = _cache_get("noaa_storms:active")
    if cached is not None:
        return cached
    try:
        r = requests.get(NHC_ACTIVE, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        storms = []
        for s in (data.get("activeStorms") or []):
            storms.append({
                "name":     s.get("name", ""),
                "type":     s.get("classification", ""),
                "basin":    s.get("binNumber", ""),
                "wind_mph": s.get("intensity", ""),
                "lat":      s.get("latitudeNumeric"),
                "lon":      s.get("longitudeNumeric"),
            })
        _cache_put("noaa_storms:active", storms)
        return storms
    except Exception as e:
        print(f"[noaa] storms error: {e}")
        _cache_put("noaa_storms:active", [])
        return []


def get_weather_forecast(location_token: str) -> Optional[dict]:
    """
    Hit Open-Meteo with the nearest known location for a market title token.
    Returns a 7-day high/low + precip summary, or None.
    """
    loc = location_token.strip().lower()
    if loc not in _LOC_GEO:
        return None
    cache_key = f"open_meteo:{loc}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    lat, lon = _LOC_GEO[loc]
    try:
        r = requests.get(OPEN_METEO, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "forecast_days": 7,
        }, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json().get("daily", {})
        out = {
            "location":    loc.title(),
            "dates":       d.get("time", []),
            "high_f":      d.get("temperature_2m_max", []),
            "low_f":       d.get("temperature_2m_min", []),
            "precip_in":   [round((mm or 0) / 25.4, 2) for mm in d.get("precipitation_sum", [])],
        }
        _cache_put(cache_key, out)
        return out
    except Exception as e:
        print(f"[open_meteo] {loc} error: {e}")
        return None


def detect_weather_location(title: str) -> Optional[str]:
    """Pull the first known location token from a weather market title."""
    tl = title.lower()
    for loc in _LOC_GEO:
        if re.search(rf"\b{loc}\b", tl):
            return loc
    return None


# ════════════════════════════════════════════════════════════════════════════
# 3. FRED — Federal Reserve Economic Data (macro grounding)
# ════════════════════════════════════════════════════════════════════════════
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# Most-referenced macro series, mapped from market keywords
_FRED_MAP = [
    (re.compile(r"\bcpi\b|\binflation\b|consumer price"), "CPIAUCSL", "CPI (Consumer Price Index)"),
    (re.compile(r"\bunemploy"),                            "UNRATE",   "Unemployment Rate"),
    (re.compile(r"\bfed funds\b|federal funds"),           "FEDFUNDS", "Fed Funds Rate"),
    (re.compile(r"\b10[-\s]?year\b|10y|treasury"),         "DGS10",    "10-Year Treasury Yield"),
    (re.compile(r"\bm2\b|money supply"),                   "M2SL",     "M2 Money Supply"),
    (re.compile(r"\bgdp\b"),                               "GDPC1",    "Real GDP"),
    (re.compile(r"\bnonfarm\b|payroll"),                   "PAYEMS",   "Nonfarm Payrolls"),
]


def fred_lookup_for_title(title: str) -> Optional[dict]:
    """If FRED_API_KEY is set and the market mentions a macro stat, return latest value."""
    if not FRED_KEY:
        return None
    tl = title.lower()
    for rx, sid, label in _FRED_MAP:
        if rx.search(tl):
            return _fred_latest(sid, label)
    return None


def _fred_latest(series_id: str, label: str) -> Optional[dict]:
    cache_key = f"fred:{series_id}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    try:
        r = requests.get(FRED_BASE, params={
            "series_id":      series_id,
            "api_key":        FRED_KEY,
            "file_type":      "json",
            "sort_order":     "desc",
            "limit":          5,
        }, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        obs = r.json().get("observations", [])
        obs = [o for o in obs if o.get("value") not in (".", None, "")]
        if not obs:
            return None
        latest = obs[0]
        prior  = obs[1] if len(obs) > 1 else None
        out = {
            "series_id": series_id,
            "label":     label,
            "value":     latest.get("value"),
            "date":      latest.get("date"),
            "prior":     prior.get("value") if prior else None,
            "prior_date": prior.get("date") if prior else None,
        }
        _cache_put(cache_key, out)
        return out
    except Exception as e:
        print(f"[fred] {series_id} error: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# 4. GEOPOLITICS BASE RATES — historical frequency tables
# ════════════════════════════════════════════════════════════════════════════
# Hand-curated historical base rates for question patterns we see frequently.
# Each entry: regex match → (base_rate_pct, source/note)
_BASE_RATES = [
    (re.compile(r"ceasefire|truce", re.I),
        (8,   "Active conflict ceasefires within 60 days resolve YES historically ~8% of the time")),
    (re.compile(r"recession", re.I),
        (15,  "Probability of recession in any forward 12-month window since 1950: ~15%")),
    (re.compile(r"impeach", re.I),
        (3,   "US Presidential impeachment-AND-removal historical rate: <3%")),
    (re.compile(r"government shutdown", re.I),
        (38,  "US government shutdown in any given fiscal year (1976-2025): ~38%")),
    (re.compile(r"world war|wwiii", re.I),
        (1,   "Sub-1% historical base rate; markets routinely overprice tail risk")),
    (re.compile(r"nuclear (weapon|attack|strike)", re.I),
        (0.5, "Combat use of a nuclear weapon since 1945: zero events")),
    (re.compile(r"hurricane.*(florida|gulf)", re.I),
        (28,  "Cat 3+ hurricane making FL/Gulf landfall in any given year: ~28%")),
    (re.compile(r"incumbent.*win|reelection", re.I),
        (66,  "US incumbent reelection rate (House): ~93%; (Senate): ~84%; (President): ~66%")),
]


def get_base_rate(title: str) -> Optional[dict]:
    for rx, (rate, note) in _BASE_RATES:
        if rx.search(title):
            return {"rate_pct": rate, "note": note}
    return None


# ════════════════════════════════════════════════════════════════════════════
# 5. REALITY CHECK ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════════════
def build_reality_check(pair: dict, manifold_markets: list[dict]) -> dict:
    """
    Compose a reality-check block for a single matched pair.
    Returns: {
       sources: [ { kind, title, value, ... } ],
       verdict: "Polymarket likely UNDERPRICED" | None,
    }
    """
    title_for_match = pair["kalshi"]["title"] or pair["polymarket"]["title"]
    cat = pair.get("category", "other")
    sources: list[dict] = []
    verdict = None

    # ── Manifold third-source ─────────────────────────────────────────────
    mf = find_manifold_match(title_for_match, manifold_markets)
    if mf:
        sources.append({
            "kind":    "manifold",
            "label":   "Manifold (3rd market)",
            "value":   f"{mf['yes_prob']*100:.1f}% YES",
            "title":   mf["title"][:120],
            "url":     mf.get("url", ""),
            "match":   mf["match_score"],
        })
        # Verdict logic: which of Kalshi/Polymarket is closer to Manifold?
        k_dist = abs(pair["kalshi"]["yes_prob"]    - mf["yes_prob"])
        p_dist = abs(pair["polymarket"]["yes_prob"] - mf["yes_prob"])
        if abs(k_dist - p_dist) > 0.05:
            outlier = "Kalshi" if k_dist > p_dist else "Polymarket"
            verdict = (
                f"Manifold agrees with {'Polymarket' if outlier=='Kalshi' else 'Kalshi'} — "
                f"{outlier} is the outlier."
            )

    # ── Weather context ───────────────────────────────────────────────────
    if cat == "weather":
        loc = detect_weather_location(title_for_match)
        if loc:
            wx = get_weather_forecast(loc)
            if wx and wx.get("dates"):
                sources.append({
                    "kind":  "weather",
                    "label": f"7-day forecast — {wx['location']}",
                    "value": (
                        f"Highs {min(wx['high_f']):.0f}°–{max(wx['high_f']):.0f}°F, "
                        f"precip total {sum(wx['precip_in']):.1f}\""
                    ),
                    "title": "via Open-Meteo (free)",
                })
        storms = get_active_hurricanes()
        if storms:
            sources.append({
                "kind":  "storms",
                "label": "Active storms (NOAA)",
                "value": ", ".join(f"{s['name']} ({s['type']})" for s in storms[:3])
                        or "None active",
                "title": "via National Hurricane Center",
            })

    # ── FRED macro grounding ──────────────────────────────────────────────
    if cat in ("economics", "finance"):
        fr = fred_lookup_for_title(title_for_match)
        if fr:
            sources.append({
                "kind":  "fred",
                "label": f"{fr['label']} (latest)",
                "value": (
                    f"{fr['value']} as of {fr['date']}"
                    + (f" (prior {fr['prior']})" if fr.get("prior") else "")
                ),
                "title": "via Federal Reserve (FRED)",
            })

    # ── Geopolitics base rate ─────────────────────────────────────────────
    if cat in ("geopolitics", "other", "economics"):
        br = get_base_rate(title_for_match)
        if br:
            sources.append({
                "kind":  "base_rate",
                "label": f"Historical base rate: {br['rate_pct']}%",
                "value": br["note"],
                "title": "Hand-curated from historical data",
            })
            # If both markets are far above the base rate, flag overpricing
            avg_market = (pair["kalshi"]["yes_prob"] +
                          pair["polymarket"]["yes_prob"]) / 2 * 100
            if avg_market > br["rate_pct"] * 2 and avg_market - br["rate_pct"] > 15:
                verdict = (verdict or "") + (
                    " " if verdict else ""
                ) + (
                    f"Both markets price this ~{avg_market:.0f}%, well above "
                    f"the {br['rate_pct']}% historical base rate — likely OVERPRICED."
                )

    return {
        "sources": sources,
        "verdict": verdict.strip() if verdict else None,
    }
