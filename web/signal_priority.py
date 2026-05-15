"""
Signal Priority Engine — the brain that decides what matters.

Every piece of incoming data (news, price move, macro print, earnings, filing)
gets classified as HIGH / MEDIUM / NOISE and ranked within its tier.

This is not a keyword filter. It uses a multi-factor scoring model:
  - Market impact: does this move prices? How much? How many assets?
  - Novelty: is this new information or a repeat of known narrative?
  - Causality: does this EXPLAIN something or just describe it?
  - Relevance: does this affect the assets/regime we're tracking?
  - Time sensitivity: does this need to be acted on in minutes or days?

Output feeds:
  - War room feed (only HIGH/MEDIUM shown)
  - Jarvis brief trigger (HIGH signals trigger immediate synthesis)
  - Discord alert (HIGH signals ping the channel)
  - Content engine (HIGH signals become video scripts)
"""
from __future__ import annotations
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from web.llm_router import gemini_call

# ── Tier definitions ──────────────────────────────────────────────────────────

PRIORITY_HIGH   = "HIGH"
PRIORITY_MEDIUM = "MEDIUM"
PRIORITY_NOISE  = "NOISE"


@dataclass
class Signal:
    id: str
    ts: float
    source: str           # "news" | "price" | "macro" | "earnings" | "fed" | "insider"
    headline: str
    body: str = ""
    priority: str = PRIORITY_NOISE
    score: float = 0.0    # 0-100
    why_it_matters: str = ""
    affected_assets: list = field(default_factory=list)
    regime_relevance: str = ""  # which regime type this matters in
    time_sensitivity: str = "days"  # "minutes" | "hours" | "days"
    raw: dict = field(default_factory=dict)


# ── Fast rule-based pre-scorer (no LLM cost) ─────────────────────────────────

# Words that strongly predict HIGH priority
_HIGH_SIGNALS = [
    # Fed / rates
    r"\bfomc\b", r"\bfederal reserve\b", r"\bpowell\b", r"\brate (hike|cut|decision|pause)\b",
    r"\bemergency (rate|meeting|cut)\b", r"\byield curve\b", r"\b(2s10s|inverted)\b",
    # Macro prints
    r"\bcpi\b", r"\bpce\b", r"\bnonfarm payroll\b", r"\bunemployment rate\b",
    r"\bgdp (print|miss|beat|contraction|revision)\b", r"\bcore inflation\b",
    # Systemic / crisis
    r"\bbank (run|failure|collapse|bailout)\b", r"\bliquidity crisis\b",
    r"\bsystemic risk\b", r"\bcredit (crunch|event|downgrade)\b",
    r"\bdefault\b", r"\bcontagion\b", r"\bcounterparty\b",
    # Geopolitical
    r"\bwar\b", r"\bmilitary (strike|attack|escalation)\b", r"\bsanction\b",
    r"\boil (embargo|supply shock)\b", r"\bnuclear\b",
    # Market structure
    r"\bcircuit breaker\b", r"\bmarket (halt|closure|crash)\b",
    r"\bflash (crash|rally)\b", r"\bmargin call\b", r"\bdelisting\b",
    # Earnings beats/misses on key names
    r"\b(nvda|nvidia|apple|microsoft|meta|amazon|alphabet|tesla|jpmorgan)\b.*\b(beat|miss|guidance|revenue)\b",
    # BTC/crypto systemic
    r"\bbtc (etf|spot|approval|rejection)\b", r"\bstablecoin (depeg|collapse)\b",
    r"\bexchange (hack|collapse|bankruptcy)\b",
]

_NOISE_SIGNALS = [
    r"\banalyst (initiat|reiterat|maintain)\b",
    r"\bprice target\b",
    r"\bupgrad(e|ing)\b.*\bbuy\b",
    r"\bdowngrad(e|ing)\b.*\bhold\b",
    r"\bsay(s|ing)\b.*\boptimistic\b",
    r"\bmarket (participants|observers|watchers)\b",
    r"\bsome (analysts|experts|investors)\b",
    r"\baccording to (sources|reports)\b",
    r"\bin (early|premarket|after-hours) trading\b",
]

_HIGH_CATEGORIES = {"geopolitical", "macro", "fed"}
_NOISE_CATEGORIES = {"general", "forex_minor"}

# Assets we care about — moves here matter
_WATCHED_ASSETS = {
    "SPY", "QQQ", "SPX", "S&P", "NASDAQ", "DOW",
    "BTC", "ETH", "SOL", "BITCOIN", "ETHEREUM",
    "NVDA", "NVIDIA", "AAPL", "APPLE", "MSFT", "MICROSOFT",
    "TSLA", "TESLA", "META", "AMZN", "AMAZON", "JPM",
    "GOLD", "GLD", "OIL", "USO", "VIX", "DXY",
    "FED", "FOMC", "POWELL", "CPI", "PCE", "NFP",
}


def _rule_score(headline: str, category: str = "") -> tuple[float, list[str]]:
    """Fast heuristic score 0-100. Returns (score, matched_rules)."""
    text = headline.lower()
    score = 30.0  # baseline
    matched = []

    # High-impact patterns
    for pat in _HIGH_SIGNALS:
        if re.search(pat, text):
            score += 25
            matched.append(pat[:30])
            break  # one match is enough to boost

    # Noise patterns
    noise_hits = sum(1 for pat in _NOISE_SIGNALS if re.search(pat, text))
    score -= noise_hits * 12

    # Category bonus
    if category.lower() in _HIGH_CATEGORIES:
        score += 15
    elif category.lower() in _NOISE_CATEGORIES:
        score -= 10

    # Watched asset mention
    words = set(re.findall(r'\b[A-Z]{2,5}\b', headline.upper()))
    asset_hits = len(words & _WATCHED_ASSETS)
    score += asset_hits * 8

    # Urgency words
    if any(w in text for w in ["breaking", "just in", "alert", "flash", "urgent", "emergency"]):
        score += 20

    # Numbers / specifics (more concrete = more signal)
    if re.search(r'\d+(\.\d+)?%', text):
        score += 8
    if re.search(r'\$\d+', text):
        score += 5

    return max(0, min(100, score)), matched


def classify_news_item(item: dict) -> Signal:
    """Classify a single news item from /api/news."""
    headline = item.get("headline", "")
    category = item.get("category", "general")
    ts = item.get("datetime", time.time())
    sid = str(item.get("id", hash(headline)))

    score, rules = _rule_score(headline, category)

    if score >= 65:
        priority = PRIORITY_HIGH
    elif score >= 38:
        priority = PRIORITY_MEDIUM
    else:
        priority = PRIORITY_NOISE

    # Extract affected assets
    assets = list(set(re.findall(r'\b[A-Z]{2,5}\b', headline.upper())) & _WATCHED_ASSETS)

    return Signal(
        id=sid, ts=ts, source="news",
        headline=headline, body="",
        priority=priority, score=round(score, 1),
        affected_assets=assets,
        raw=item,
    )


def classify_batch(items: list[dict]) -> list[Signal]:
    """Classify a list of news items, sorted by score descending."""
    signals = [classify_news_item(i) for i in items]
    return sorted(signals, key=lambda s: s.score, reverse=True)


def get_high_signals(items: list[dict]) -> list[Signal]:
    return [s for s in classify_batch(items) if s.priority == PRIORITY_HIGH]


def get_top_signals(items: list[dict], n: int = 10) -> list[Signal]:
    """Top N signals regardless of tier."""
    return classify_batch(items)[:n]


# ── LLM enrichment (only for HIGH signals) ────────────────────────────────────

def enrich_signal(signal: Signal, regime: str = "", mood: str = "") -> Signal:
    """
    Use Gemini to add: why_it_matters, time_sensitivity, regime_relevance.
    Only called for HIGH priority signals to keep costs minimal.
    """
    prompt = (
        f"Market regime: {regime or 'unknown'}. Market mood: {mood or 'unknown'}.\n"
        f"News headline: {signal.headline}\n\n"
        "Answer in exactly 2 sentences:\n"
        "1. WHY does this matter to markets right now — name the specific mechanism, "
        "not the event itself. Be causal, not descriptive.\n"
        "2. What should a trader watch in the next 2-4 hours as a result of this?\n"
        "No fluff. No 'this could potentially'. Be direct."
    )
    result = gemini_call(prompt, max_tokens=150, label="signal:enrich")
    if result:
        lines = result.strip().split("\n")
        signal.why_it_matters = " ".join(lines).strip()
        # Classify time sensitivity
        if any(w in result.lower() for w in ["minutes", "immediate", "right now", "today's session"]):
            signal.time_sensitivity = "minutes"
        elif any(w in result.lower() for w in ["hours", "rest of the day", "today"]):
            signal.time_sensitivity = "hours"
        else:
            signal.time_sensitivity = "days"
    return signal


# ── Regime detector ───────────────────────────────────────────────────────────

REGIMES = {
    "RISK_ON":        "Stocks bid, vol compressed, yields stable, dollar flat/weak, BTC up",
    "RISK_OFF":       "Equity selling, VIX spiking, flight to bonds/gold/cash",
    "STAGFLATION":    "Inflation high, growth slowing, rates rising, commodities up",
    "DEFLATION_FEAR": "Growth collapsing, rates falling fast, commodities dumping",
    "FED_PIVOT":      "Market front-running a Fed policy shift, rates market moving",
    "GEOPOLITICAL":   "Risk driven by political/military events, safe havens bid",
    "EUPHORIA":       "Extreme greed, vol crushed, meme stocks, leverage building",
    "PANIC":          "Circuit-breaker risk, margin calls, liquidity evaporating",
    "CONSOLIDATION":  "Range-bound, low vol, no clear trend, chop",
}


def detect_regime(snap: dict, analysis: dict, fear_greed: int = 50) -> dict:
    """
    Classify the current market regime from live data.
    Returns {regime, confidence, description, key_signals}
    """
    scores = {r: 0 for r in REGIMES}
    key_signals = []

    # VIX
    vix = 0
    if "VIX" in snap:
        vix = snap["VIX"].get("current", 0) or snap["VIX"].get("price", 0)
    if vix > 30:
        scores["PANIC"] += 3; scores["RISK_OFF"] += 2
        key_signals.append(f"VIX {vix:.1f} — elevated fear")
    elif vix > 20:
        scores["RISK_OFF"] += 1
        key_signals.append(f"VIX {vix:.1f} — caution zone")
    elif vix < 14:
        scores["RISK_ON"] += 2; scores["EUPHORIA"] += 1
        key_signals.append(f"VIX {vix:.1f} — complacency")

    # Equity direction
    es_chg = snap.get("ES", {}).get("change_pct", 0) or snap.get("ES", {}).get("chg_pct", 0)
    if es_chg > 1.5:
        scores["RISK_ON"] += 2; scores["EUPHORIA"] += 1
        key_signals.append(f"S&P +{es_chg:.1f}% — equity bid")
    elif es_chg < -1.5:
        scores["RISK_OFF"] += 2; scores["PANIC"] += 1
        key_signals.append(f"S&P {es_chg:.1f}% — equity selling")

    # Gold
    gc_chg = snap.get("GC", {}).get("change_pct", 0)
    if gc_chg > 1.0:
        scores["RISK_OFF"] += 1; scores["GEOPOLITICAL"] += 1
        key_signals.append(f"Gold +{gc_chg:.1f}% — safe haven bid")
    elif gc_chg < -1.0:
        scores["RISK_ON"] += 1

    # DXY
    dxy_chg = snap.get("DXY", {}).get("change_pct", 0)
    if dxy_chg > 0.5:
        scores["RISK_OFF"] += 1; scores["STAGFLATION"] += 1
        key_signals.append(f"DXY +{dxy_chg:.1f}% — dollar strength")
    elif dxy_chg < -0.5:
        scores["RISK_ON"] += 1

    # BTC
    btc_chg = snap.get("BTC", {}).get("change_pct", 0)
    if btc_chg > 3:
        scores["RISK_ON"] += 1; scores["EUPHORIA"] += 1
    elif btc_chg < -4:
        scores["RISK_OFF"] += 1; scores["PANIC"] += 1

    # Fear & Greed
    if fear_greed < 20:
        scores["PANIC"] += 2; scores["RISK_OFF"] += 1
        key_signals.append(f"Fear & Greed {fear_greed} — extreme fear")
    elif fear_greed < 35:
        scores["RISK_OFF"] += 1
    elif fear_greed > 75:
        scores["EUPHORIA"] += 2; scores["RISK_ON"] += 1
        key_signals.append(f"Fear & Greed {fear_greed} — extreme greed")
    elif fear_greed > 60:
        scores["RISK_ON"] += 1

    # Mood override
    mood = analysis.get("mood", "").upper()
    if "RISK ON" in mood or "BULLISH" in mood:
        scores["RISK_ON"] += 1
    elif "RISK OFF" in mood or "BEARISH" in mood:
        scores["RISK_OFF"] += 1

    # Pick winner
    best = max(scores, key=lambda r: scores[r])
    total = sum(scores.values()) or 1
    confidence = round(scores[best] / total * 100)

    return {
        "regime": best,
        "confidence": confidence,
        "description": REGIMES[best],
        "key_signals": key_signals[:5],
        "all_scores": scores,
    }
