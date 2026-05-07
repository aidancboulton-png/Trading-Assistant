#!/usr/bin/env python3
"""
Prediction Market Discrepancy Scanner
Compares Kalshi vs Polymarket implied probabilities for equivalent markets.
Finds where the two platforms meaningfully disagree — those gaps are the edge.
"""

import re
import time
import requests
from datetime import datetime, timezone
from typing import Optional

try:
    from web.predmarket_context import fetch_manifold_markets, build_reality_check
except ImportError:  # when run as a script from the web/ directory
    from predmarket_context import fetch_manifold_markets, build_reality_check

KALSHI_BASE  = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA   = "https://gamma-api.polymarket.com"

HEADERS = {"User-Agent": "prediction-arb-scanner/1.0", "Accept": "application/json"}
TIMEOUT = 12

# ── Category keyword maps ─────────────────────────────────────────────────────
CATEGORIES = {
    "economics": [
        "gdp", "cpi", "inflation", "fed", "rate", "interest", "unemployment",
        "recession", "treasury", "deficit", "trade", "tariff", "debt", "pce",
        "china gdp", "japan gdp", "euro gdp", "growth rate", "boe", "ecb", "boj",
        "nonfarm", "payroll", "consumer price", "producer price", "fomc",
    ],
    "weather": [
        "hurricane", "temperature", "rainfall", "storm", "tornado", "wildfire",
        "drought", "flood", "climate", "snow", "heat", "typhoon", "cyclone",
        "named storm", "atlantic season", "landfall", "precipitation",
    ],
    "sports": [
        "cricket", "nfl", "nba", "mlb", "nhl", "soccer", "tennis", "formula 1",
        " f1 ", "golf", "olympic", "champion", "league", "cup", "bowl", "series",
        "ipl", "test match", "ashes", "premier league", "world cup", "ufc", "mma",
        "super bowl", "playoffs", "championship", "grand slam", "masters",
    ],
    "finance": [
        "bitcoin", "btc", "ethereum", "eth", "solana", "s&p 500", "nasdaq",
        "gold price", "oil price", "crude", "crypto", "dow jones", "russell",
        "vix", "yield", "sp500", "10-year", "fed funds",
    ],
    "geopolitics": [
        "war", "election", "president", "prime minister", "treaty", "sanctions",
        "nato", "military", "conflict", "ceasefire", "congress", "senate",
        "supreme court", "vote", "referendum", "impeach", "tariff", "diplomacy",
        "nuclear", "missile", "ukraine", "taiwan", "iran", "israel", "gaza",
    ],
    "science_tech": [
        "fda", "approval", "spacex", "launch", "moon", "drug", "vaccine",
        "clinical trial", "gpt", "llm", "artificial intelligence", "openai",
        "anthropic", "google", "apple earnings", "merger", "acquisition",
    ],
}

CATEGORY_LABELS = {
    "economics":    "Economics",
    "weather":      "Weather",
    "sports":       "Sports",
    "finance":      "Finance",
    "geopolitics":  "Geopolitics",
    "science_tech": "Science & Tech",
    "other":        "Other",
}

CATEGORY_COLORS = {
    "economics":    "#3b82f6",
    "weather":      "#06b6d4",
    "sports":       "#f59e0b",
    "finance":      "#10b981",
    "geopolitics":  "#ef4444",
    "science_tech": "#8b5cf6",
    "other":        "#6b7280",
}

STOP_WORDS = {
    "the", "a", "an", "in", "on", "at", "to", "of", "by",
    "for", "is", "are", "was", "this", "that", "or", "and", "if",
    "with", "from", "their", "its", "have", "has", "had", "would",
    "could", "should", "may", "might", "does", "do", "be", "will",
}

# Tokens that uniquely identify a market and should weight matching heavily
def _anchor_tokens(title: str) -> set:
    """Pull out high-signal tokens: numbers, $ amounts, %, years, proper nouns."""
    t = title.lower()
    anchors = set()
    # Years (4-digit), e.g. 2026
    anchors |= set(re.findall(r"\b(19|20)\d{2}\b", t))
    # Dollar amounts: $5k, $1m, $120, $1.5b
    anchors |= set(re.findall(r"\$[\d.,]+\s?[kmb]?", t))
    # Percentages: 50%, 2.5%
    anchors |= set(re.findall(r"\d+\.?\d*%", t))
    # Pure numbers ≥ 3 digits (price targets, counts)
    anchors |= set(re.findall(r"\b\d{3,}\b", t))
    # Proper nouns (capitalized, ≥4 chars) from original title
    anchors |= {w.lower() for w in re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", title)
                if w.lower() not in {"will", "this", "that", "what", "when", "where", "which"}}
    return anchors

# ── Utilities ─────────────────────────────────────────────────────────────────

def _normalize(title: str) -> set:
    """Tokenize a market title into a set of meaningful tokens."""
    t = re.sub(r"[^\w\s$%]", " ", title.lower())
    tokens = set(t.split()) - STOP_WORDS
    # keep dollar amounts and percentages as single tokens
    tokens |= set(re.findall(r"\$[\d,]+[kmb]?|[\d.]+%|[\d]{4}", title.lower()))
    return tokens


def _similarity(a: str, b: str) -> float:
    """
    Hybrid similarity score:
      • Base = Jaccard on cleaned tokens
      • Bonus = anchor tokens shared (years, $ amounts, %, proper nouns)
    Anchors are weighted heavily because matching titles for the same event
    almost always share at least one specific anchor.
    """
    ta, tb = _normalize(a), _normalize(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)

    aa, ab = _anchor_tokens(a), _anchor_tokens(b)
    if aa and ab:
        anchor_overlap = len(aa & ab) / max(len(aa | ab), 1)
        # Each shared anchor bumps similarity meaningfully
        anchor_bonus = min(0.4, len(aa & ab) * 0.15)
        return min(1.0, jaccard * 0.6 + anchor_overlap * 0.4 + anchor_bonus)
    return jaccard


_NEG_PATTERNS = [
    r"\bnot\b", r"\bno\s", r"\bfail\b", r"\bwon[' ]?t\b", r"\bdoesn[' ]?t\b",
    r"\bdidn[' ]?t\b", r"\bisn[' ]?t\b", r"\bwithout\b", r"\bavoid\b",
    r"\bmiss(es|ed)?\b", r"\bunder\b", r"\bbelow\b",
]
_RANGE_PATTERNS = [
    r"\d+\s*-\s*\d+\s*%",          # "0-10%", "20-30 %"
    r"\d+\s*to\s*\d+\s*%",         # "0 to 10%"
    r"between\s+\$?\d+\s+and",      # "between 5 and 10"
    r"\babove\s+\$?[\d,.]+\b",
    r"\bover\s+\$?[\d,.]+\s*(point|run|goal|year|%|\$|percent|million|billion)",
    r"\bmargin\s+of\s+victory\b",  # "Senate margin of victory — X"
    r"\bsucceed\s+\w+\b",          # "Who will succeed X" — multi-candidate sub
]

# Country tokens — if both titles mention different countries, mismatch
_COUNTRY_TOKENS = {
    "israel", "morocco", "iran", "ukraine", "russia", "china", "taiwan",
    "japan", "germany", "france", "uk", "britain", "canada", "mexico",
    "india", "pakistan", "brazil", "argentina", "australia", "egypt",
    "syria", "lebanon", "turkey", "korea", "vietnam", "afghanistan",
    "venezuela", "italy", "spain", "poland", "greece",
}
def _country_mismatch(a: str, b: str) -> bool:
    al = a.lower(); bl = b.lower()
    a_countries = {c for c in _COUNTRY_TOKENS if re.search(rf"\b{c}\b", al)}
    b_countries = {c for c in _COUNTRY_TOKENS if re.search(rf"\b{c}\b", bl)}
    if a_countries and b_countries and not (a_countries & b_countries):
        return True
    return False

# Kalshi superlative/aggregator questions that match too liberally
# ("closest race", "who will win", etc.). These create false pairs.
_AGGREGATOR_PATTERNS = [
    r"\bclosest\b", r"\bbiggest\b", r"\bsmallest\b", r"\bmost\s",
    r"\bleast\s", r"\bwhich\s", r"\bhighest\b", r"\blowest\b",
    r"\bnumber\s+of\b", r"\bhow\s+many\b",
]
def _is_aggregator(title: str) -> bool:
    tl = title.lower()
    return any(re.search(p, tl) for p in _AGGREGATOR_PATTERNS)

def _is_negated(title: str) -> bool:
    """Detect if title is the negative framing of a question."""
    tl = title.lower()
    return any(re.search(p, tl) for p in _NEG_PATTERNS)

def _political_party(title: str) -> Optional[str]:
    """Return 'rep' or 'dem' if title is asking about a specific party winning."""
    tl = title.lower()
    has_rep = bool(re.search(r"\brepublican(s)?\b|\bgop\b", tl))
    has_dem = bool(re.search(r"\bdemocrat(s|ic)?\b", tl))
    if has_rep and not has_dem:
        return "rep"
    if has_dem and not has_rep:
        return "dem"
    return None  # neither, or both

def _is_subrange(title: str) -> bool:
    """Detect Kalshi sub-questions like 'margin 0-10%' that scope a parent event."""
    tl = title.lower()
    return any(re.search(p, tl) for p in _RANGE_PATTERNS)


# ── Question-shape fingerprint ────────────────────────────────────────────────
# Two markets cannot legitimately match unless their question SHAPE is the same.
# A "voter turnout above 960K" question is NOT the same bet as "Will Democrats
# win the senate race" — even if both are about the same election.
def _question_shape(title: str) -> str:
    """
    Classify the *kind* of question being asked. Markets with different shapes
    must never be matched.
        threshold_above   — "will X be above N", "more than N"
        threshold_below   — "below N", "less than N", "under N"
        range_between     — "between A and B"
        winner_party      — "will Republicans/Democrats win"
        winner_named      — "will <PERSON/THING> win"
        binary_event      — "will <event> happen"
    """
    tl = title.lower()
    if re.search(r"\b(above|over|more than|exceed|greater than|higher than|at least)\b", tl):
        return "threshold_above"
    if re.search(r"\b(below|under|less than|fewer than|at most|no more than)\b", tl):
        return "threshold_below"
    if re.search(r"\bbetween\b.*\band\b", tl) or re.search(r"\d+\s*[-–]\s*\d+", tl):
        return "range_between"
    if re.search(r"\b(republican|democrat|gop)\b.*\b(win|winner|control)\b", tl) \
       or re.search(r"\b(win|winner|control)\b.*\b(republican|democrat|gop)\b", tl):
        return "winner_party"
    if re.search(r"\b(win|winner|nominee|elected|chosen|named)\b", tl):
        return "winner_named"
    return "binary_event"


def _shape_compatible(a: str, b: str) -> bool:
    """Two question shapes match if they're identical, OR both are binary events."""
    sa, sb = _question_shape(a), _question_shape(b)
    if sa == sb:
        return True
    # winner_named and winner_party can sometimes legitimately mirror each other
    # IF both titles refer to the same election. We let the numeric/anchor lock
    # downstream decide. Other cross-shape pairings are forbidden.
    if {sa, sb} == {"winner_named", "winner_party"}:
        return True
    return False


def _numeric_tokens(title: str) -> set:
    """
    Extract every numeric anchor a title commits to: thresholds, dollar amounts,
    percentages, vote totals, point spreads. If two titles both commit to numbers
    but they're different numbers, they cannot be the same bet.
    """
    t = title.lower().replace(",", "")
    out = set()
    # $5k, $1m, $1.5b, $50, etc.
    for m in re.findall(r"\$?\d+(?:\.\d+)?\s*[kmb]?\b", t):
        s = m.strip()
        if s and not s.isalpha():
            out.add(s.replace("$", ""))
    # bare integers ≥ 3 digits
    out |= set(re.findall(r"\b\d{3,}\b", t))
    # percentages
    out |= set(re.findall(r"\d+\.?\d*%", t))
    # filter out tiny spurious
    return {x for x in out if len(x) >= 2}


_SPECIFIC_SUBJECT_HINTS = (
    # office / race
    "senate", "president", "presidential", "house", "governor", "mayoral",
    # action types
    "meet", "meeting", "summit", "win", "nominee", "nomination", "election",
    "leader", "ceasefire", "agreement", "treaty",
)


def _proper_nouns(title: str) -> set:
    """Capitalized 4+ char tokens — locations, people, organizations."""
    return {w.lower() for w in re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", title)
            if w.lower() not in {
                "will", "this", "that", "what", "when", "where", "which",
                "who", "kalshi", "polymarket", "the", "and", "for", "from",
            }}


def _race_year(title: str) -> Optional[str]:
    """Extract the election year if present (2024, 2026, 2028 etc.)."""
    m = re.search(r"\b(20\d{2})\b", title)
    return m.group(1) if m else None


_CATEGORICAL_PHRASES = (
    "family member", "any of", "anyone", "either of", "any candidate",
    "any republican", "any democrat", "anyone else", "any other",
)


def _is_categorical(title: str) -> bool:
    """Title asks about a category/group, not a specific person/thing."""
    tl = title.lower()
    return any(p in tl for p in _CATEGORICAL_PHRASES)


def _subjects_compatible(a: str, b: str) -> bool:
    """
    Strict subject lock. If both titles reference specific subjects (proper
    nouns) AND at least one hints at a specific outcome, the proper-noun
    overlap must be ≥ 60% of the smaller set. This stops:
      - "Trump/Putin meet UAE" vs "Trump/Putin meet China" (locations differ)
      - "Trump family member" vs "Donald Trump" (categorical vs specific)
      - "AOC Senate" vs "AOC President" (offices differ — handled separately)
    """
    # Categorical-vs-specific is never the same bet
    if _is_categorical(a) != _is_categorical(b):
        return False

    al, bl = a.lower(), b.lower()
    al_has_hint = any(h in al for h in _SPECIFIC_SUBJECT_HINTS)
    bl_has_hint = any(h in bl for h in _SPECIFIC_SUBJECT_HINTS)
    if not (al_has_hint or bl_has_hint):
        return True

    pa, pb = _proper_nouns(a), _proper_nouns(b)
    if not pa or not pb:
        return True

    overlap = pa & pb
    smaller = min(len(pa), len(pb))
    if smaller == 0:
        return True
    overlap_ratio = len(overlap) / smaller

    # Need ≥ 60% of the smaller proper-noun set overlapping
    if overlap_ratio >= 0.60 and len(overlap) >= 2:
        return True
    # Single-overlap fallback: same race year + that overlap can still be valid
    ya, yb = _race_year(a), _race_year(b)
    if len(overlap) >= 1 and ya and yb and ya == yb and overlap_ratio >= 0.5:
        return True
    return False


# Office / race words that describe the *kind* of contest. If both titles
# carry an office word and they're different, it's a different bet entirely
# (Senate ≠ President ≠ Governor).
_OFFICE_TOKENS = {
    "senate":         "senate",
    "house":          "house",
    "governor":       "governor",
    "gubernatorial":  "governor",
    "president":      "president",
    "presidential":   "president",
    "mayor":          "mayor",
    "mayoral":        "mayor",
    "vice president": "vp",
    "vp":             "vp",
}
def _office_mismatch(a: str, b: str) -> bool:
    al, bl = a.lower(), b.lower()
    a_off = {v for k, v in _OFFICE_TOKENS.items() if re.search(rf"\b{k}\b", al)}
    b_off = {v for k, v in _OFFICE_TOKENS.items() if re.search(rf"\b{k}\b", bl)}
    if a_off and b_off and not (a_off & b_off):
        return True
    return False


def _numbers_compatible(a: str, b: str) -> bool:
    """
    If BOTH titles have numeric anchors, at least one must overlap.
    If only one side has numbers, that's allowed (e.g. "Will Trump win" vs
    "Will Trump win in 2026").
    """
    na, nb = _numeric_tokens(a), _numeric_tokens(b)
    if not na or not nb:
        return True
    return bool(na & nb)


def _close_in_days(close_iso: str) -> Optional[float]:
    """Return days until market close, or None if unknown."""
    if not close_iso:
        return None
    try:
        # Kalshi uses 'Z', Polymarket uses '+00:00' — both work with fromisoformat
        # after stripping 'Z'.
        s = close_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
        return delta
    except Exception:
        return None


def categorize(title: str) -> str:
    title_lower = title.lower()
    scores: dict[str, int] = {cat: 0 for cat in CATEGORIES}
    for cat, keywords in CATEGORIES.items():
        for kw in keywords:
            if kw in title_lower:
                scores[cat] += 1
    best = max(scores, key=scores.get)  # type: ignore
    return best if scores[best] > 0 else "other"


def _mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """Safe mid-price from bid/ask."""
    if bid is None and ask is None:
        return None
    if bid is None:
        return ask
    if ask is None:
        return bid
    return (bid + ask) / 2.0


# ── Kalshi fetcher ────────────────────────────────────────────────────────────

def fetch_kalshi(max_pages: int = 5) -> list[dict]:
    """
    Fetch active Kalshi events with nested markets. Use the EVENT title
    (clean human-readable question) instead of the market title (which is
    often parlay-leg gibberish like 'yes Detroit,yes LeBron James: 15+').
    """
    markets = []
    cursor = None

    def _to_float(v) -> Optional[float]:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for _ in range(max_pages):
        params = {"limit": 200, "status": "open", "with_nested_markets": "true"}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{KALSHI_BASE}/events", params=params,
                             headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[kalshi] fetch error: {e}")
            break

        raw = data.get("events", [])
        for e in raw:
            event_title = e.get("title", "") or ""
            event_ticker = e.get("event_ticker", "")
            sub_markets = e.get("markets", []) or []
            if not event_title or not sub_markets:
                continue

            # Multi-market events (e.g. "Who will be next Pope?") have one market per
            # candidate. We treat each market as its own question by combining the
            # event title with the market's yes_sub_title.
            for m in sub_markets:
                if m.get("status") not in ("active", "open"):
                    continue

                yes_bid = _to_float(m.get("yes_bid_dollars"))
                yes_ask = _to_float(m.get("yes_ask_dollars"))
                last    = _to_float(m.get("last_price_dollars"))

                mid = _mid(yes_bid, yes_ask)
                if mid is None or mid < 0.001:
                    if last and last > 0.001:
                        mid = last
                    else:
                        continue
                if not (0.01 <= mid <= 0.99):
                    continue

                # Build a clean question title.
                if len(sub_markets) == 1:
                    title = event_title
                else:
                    sub = m.get("yes_sub_title") or m.get("subtitle") or ""
                    title = f"{event_title} — {sub}".strip(" —") if sub else event_title

                # Skip remaining parlay-style titles
                tl = title.lower()
                if tl.startswith("yes ") or tl.startswith("no ") or ",yes " in tl or ",no " in tl:
                    continue

                try:
                    volume = int(float(m.get("volume_fp", 0) or 0))
                except Exception:
                    volume = 0

                markets.append({
                    "platform":  "Kalshi",
                    "id":        m.get("ticker", ""),
                    "title":     title,
                    "category":  categorize(title),
                    "yes_prob":  round(mid, 4),
                    "volume":    volume,
                    "close":     m.get("close_time", ""),
                    "url":       f"https://kalshi.com/markets/{event_ticker}",
                })

        cursor = data.get("cursor")
        if not cursor or not raw:
            break
        time.sleep(0.3)

    return markets


# ── Polymarket fetcher ────────────────────────────────────────────────────────

def fetch_polymarket(max_pages: int = 5) -> list[dict]:
    """Fetch active Polymarket markets via Gamma API."""
    markets = []
    offset = 0
    limit = 200

    for _ in range(max_pages):
        try:
            r = requests.get(
                f"{POLY_GAMMA}/markets",
                params={"active": "true", "closed": "false",
                        "limit": limit, "offset": offset},
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            raw = r.json()
        except Exception as e:
            print(f"[polymarket] fetch error: {e}")
            break

        if not raw:
            break

        for m in raw:
            # Binary markets have outcomes ["Yes","No"] + outcomePrices "[0.7, 0.3]"
            if m.get("closed") or not m.get("active"):
                continue
            outcomes = m.get("outcomes", "[]")
            prices   = m.get("outcomePrices", "[]")
            try:
                outcomes = outcomes if isinstance(outcomes, list) else __import__("json").loads(outcomes)
                prices   = prices   if isinstance(prices,   list) else __import__("json").loads(prices)
            except Exception:
                continue
            if len(outcomes) != 2 or len(prices) != 2:
                continue  # skip multi-outcome markets

            # find YES price
            yes_prob = None
            for i, o in enumerate(outcomes):
                if str(o).lower() in ("yes", "true", "1"):
                    try:
                        yes_prob = float(prices[i])
                    except Exception:
                        pass
                    break
            if yes_prob is None:
                try:
                    yes_prob = float(prices[0])  # assume first = YES
                except Exception:
                    continue

            if not (0.01 <= yes_prob <= 0.99):
                continue

            title = m.get("question", "")
            if not title:
                continue

            vol = 0
            try:
                vol = float(m.get("volume", 0) or 0)
            except Exception:
                pass

            markets.append({
                "platform":  "Polymarket",
                "id":        str(m.get("id", "")),
                "title":     title,
                "category":  categorize(title),
                "yes_prob":  round(yes_prob, 4),
                "volume":    int(vol),
                "close":     m.get("endDate", ""),
                "url":       f"https://polymarket.com/event/{m.get('slug', m.get('id', ''))}",
            })

        offset += limit
        if len(raw) < limit:
            break
        time.sleep(0.3)

    return markets


# ── Matcher ───────────────────────────────────────────────────────────────────

MATCH_THRESHOLD  = 0.40       # tightened — was 0.25, far too lenient
MIN_JACCARD      = 0.25       # tightened — require real word overlap
MIN_SHARED_WORDS = 4          # tightened — both titles share ≥4 meaningful tokens
MIN_DAYS_TO_CLOSE = 7.0       # only show markets resolving 7+ days out


def _hedge_math(k_prob: float, p_prob: float) -> dict:
    """
    Pure arbitrage opportunity calculator.
    If you can buy YES cheap on one platform and NO cheap on the other,
    your total cost is < $1 and you collect $1 no matter the outcome.
    """
    # Buy YES on cheaper-YES platform, buy NO on the other (NO = 1 - YES)
    if k_prob < p_prob:
        yes_buy_plat, yes_buy_price = "Kalshi", k_prob
        no_buy_plat,  no_buy_price  = "Polymarket", 1 - p_prob
    else:
        yes_buy_plat, yes_buy_price = "Polymarket", p_prob
        no_buy_plat,  no_buy_price  = "Kalshi", 1 - k_prob

    total_cost = yes_buy_price + no_buy_price
    locked_profit = round((1 - total_cost) * 100, 2)  # ¢ guaranteed per $1 risked
    is_arb = locked_profit > 0
    roi_pct = round(locked_profit / (total_cost * 100) * 100, 2) if total_cost > 0 else 0

    return {
        "is_arb":         is_arb,
        "yes_platform":   yes_buy_plat,
        "yes_price":      round(yes_buy_price * 100, 1),
        "no_platform":    no_buy_plat,
        "no_price":       round(no_buy_price * 100, 1),
        "total_cost":     round(total_cost * 100, 1),
        "locked_profit":  locked_profit,  # ¢ profit per $1 invested
        "roi_pct":        roi_pct,
    }


def match_markets(kalshi: list[dict], polymarket: list[dict]) -> list[dict]:
    """
    Cross-match Kalshi and Polymarket markets across all categories.
    Returns pairs sorted by discrepancy (largest edge first).
    """
    pairs = []
    seen_polys: set = set()  # prevent same polymarket matched to multiple kalshi

    for km in kalshi:
        # Skip sub-range Kalshi questions and superlative aggregators —
        # both classes generate false matches with simple yes/no Polymarket questions
        if _is_subrange(km["title"]) or _is_aggregator(km["title"]):
            continue
        # Drop anything resolving in less than the configured floor
        kdays = _close_in_days(km.get("close", ""))
        if kdays is not None and kdays < MIN_DAYS_TO_CLOSE:
            continue

        # search ALL polymarket markets, not just same-category
        best_score = 0.0
        best_pm = None
        best_jaccard = 0.0
        best_shared = 0
        km_tokens = _normalize(km["title"])
        for pm in polymarket:
            if _is_subrange(pm["title"]) or _is_aggregator(pm["title"]):
                continue
            if _country_mismatch(km["title"], pm["title"]):
                continue
            # Hard locks — must pass before scoring
            if not _shape_compatible(km["title"], pm["title"]):
                continue
            if not _numbers_compatible(km["title"], pm["title"]):
                continue
            if not _subjects_compatible(km["title"], pm["title"]):
                continue
            if _office_mismatch(km["title"], pm["title"]):
                continue
            pdays = _close_in_days(pm.get("close", ""))
            if pdays is not None and pdays < MIN_DAYS_TO_CLOSE:
                continue
            score = _similarity(km["title"], pm["title"])
            if score > best_score:
                pm_tokens = _normalize(pm["title"])
                shared = len(km_tokens & pm_tokens)
                jacc = len(km_tokens & pm_tokens) / max(len(km_tokens | pm_tokens), 1)
                best_score = score
                best_pm = pm
                best_jaccard = jacc
                best_shared = shared

        if best_pm is None or best_score < MATCH_THRESHOLD:
            continue
        if best_jaccard < MIN_JACCARD or best_shared < MIN_SHARED_WORDS:
            continue  # anchor matched but titles don't really overlap

        # Polarity check: if one title is negated and the other isn't,
        # OR if one asks about Republicans winning and the other about Democrats,
        # flip the inverse side's probability to compare equivalently.
        k_neg = _is_negated(km["title"])
        p_neg = _is_negated(best_pm["title"])
        k_party = _political_party(km["title"])
        p_party = _political_party(best_pm["title"])
        k_prob = km["yes_prob"]
        p_prob = best_pm["yes_prob"]
        polarity_flipped = False

        if k_neg != p_neg:
            polarity_flipped = True
            if p_neg:
                p_prob = 1 - p_prob
            else:
                k_prob = 1 - k_prob
        elif k_party and p_party and k_party != p_party:
            # Two-party inversion: P(R wins) = 1 - P(D wins) in a 2-party race
            polarity_flipped = True
            p_prob = 1 - p_prob

        edge = abs(k_prob - p_prob)
        if edge < 0.02:
            continue

        if k_prob > p_prob:
            higher = "Kalshi"
            buy_advice = f"YES (equivalent) cheaper on Polymarket ({p_prob*100:.1f}¢) vs Kalshi ({k_prob*100:.1f}¢)"
        else:
            higher = "Polymarket"
            buy_advice = f"YES (equivalent) cheaper on Kalshi ({k_prob*100:.1f}¢) vs Polymarket ({p_prob*100:.1f}¢)"

        kelly = round(edge / max(1 - min(k_prob, p_prob), 0.01) * 0.25, 3)
        hedge = _hedge_math(k_prob, p_prob)
        if polarity_flipped:
            hedge["polarity_note"] = (
                "One platform frames the question in the negative — "
                "probabilities have been flipped to compare equivalently."
            )

        # Use the kalshi market's category for display since they tend to be more specific
        cat = km["category"]

        pairs.append({
            "category":       cat,
            "category_label": CATEGORY_LABELS.get(cat, cat),
            "category_color": CATEGORY_COLORS.get(cat, "#6b7280"),
            "match_score":    round(best_score, 3),
            "edge":           round(edge, 4),
            "edge_pct":       round(edge * 100, 1),
            "kelly":          kelly,
            "higher_platform": higher,
            "buy_advice":     buy_advice,
            "hedge":          hedge,
            "days_to_close": round(min(
                _close_in_days(km.get("close", "")) or 9999,
                _close_in_days(best_pm.get("close", "")) or 9999,
            ), 1),
            "kalshi": {
                "title":    km["title"],
                "yes_prob": km["yes_prob"],
                "volume":   km["volume"],
                "url":      km["url"],
                "id":       km["id"],
                "close":    km.get("close", ""),
            },
            "polymarket": {
                "title":    best_pm["title"],
                "yes_prob": best_pm["yes_prob"],
                "volume":   best_pm["volume"],
                "url":      best_pm["url"],
                "id":       best_pm["id"],
                "close":    best_pm.get("close", ""),
            },
        })

    # Category preference: simple/objective markets first
    _CAT_PRIORITY = {
        "weather":      0,   # most objective — measurable, clear resolution
        "finance":      1,   # price-mention markets are simple and verifiable
        "sports":       2,
        "economics":    3,
        "science_tech": 4,
        "geopolitics":  5,   # squishiest — last
        "other":        6,
    }
    # Sort: arbitrage first, then by category clarity, then edge
    pairs.sort(key=lambda x: (
        -1 if x["hedge"]["is_arb"] else 0,
        _CAT_PRIORITY.get(x["category"], 9),
        -x["edge"],
    ))
    return pairs


# ── Plain-English edge commentary ─────────────────────────────────────────────

def plain_english(pair: dict) -> str:
    edge = pair["edge_pct"]
    k    = pair["kalshi"]
    p    = pair["polymarket"]
    cat  = pair["category"]

    low_plat  = "Polymarket" if pair["higher_platform"] == "Kalshi" else "Kalshi"
    low_prob  = min(k["yes_prob"], p["yes_prob"]) * 100
    high_prob = max(k["yes_prob"], p["yes_prob"]) * 100

    quality = (
        "Huge discrepancy" if edge >= 15 else
        "Large discrepancy" if edge >= 8 else
        "Notable gap" if edge >= 4 else
        "Slight lean"
    )

    context = {
        "economics":    "Economic data markets are often mispriced early — forecasters update slowly.",
        "weather":      "Weather models are probabilistic — platform data freshness matters a lot here.",
        "sports":       "Sports markets lag when news breaks (injuries, lineup changes). Check recency.",
        "finance":      "Finance markets on prediction platforms often lag spot prices. Verify both.",
        "geopolitics":  "Geopolitical markets are hard to price — high uncertainty is normal.",
        "science_tech": "Regulatory/tech markets can gap when insider information isn't yet priced.",
    }.get(cat, "Cross-check both platforms for data staleness before acting.")

    return (
        f"{quality}: {low_plat} says {low_prob:.1f}% YES, "
        f"the other platform says {high_prob:.1f}% YES — {edge:.1f}pp apart. "
        f"{context}"
    )


# ── Main scanner ──────────────────────────────────────────────────────────────

def run_scan() -> dict:
    """Full scan. Returns structured result dict for API or CLI use."""
    started = datetime.now(timezone.utc).isoformat()

    print("[scanner] Fetching Kalshi markets…")
    kalshi_markets = fetch_kalshi(max_pages=6)
    print(f"[scanner] Got {len(kalshi_markets)} Kalshi markets")

    print("[scanner] Fetching Polymarket markets…")
    poly_markets = fetch_polymarket(max_pages=6)
    print(f"[scanner] Got {len(poly_markets)} Polymarket markets")

    print("[scanner] Matching and scoring…")
    pairs = match_markets(kalshi_markets, poly_markets)

    # Pull Manifold once for cross-validation across all pairs
    print("[scanner] Fetching Manifold for reality-check…")
    try:
        manifold_markets = fetch_manifold_markets(limit=1000)
        print(f"[scanner] Got {len(manifold_markets)} Manifold markets")
    except Exception as e:
        print(f"[scanner] manifold fetch failed: {e}")
        manifold_markets = []

    # Annotate with plain English + reality check (3rd source, weather, FRED, base rates)
    for p in pairs:
        p["plain_english"] = plain_english(p)
        try:
            p["reality_check"] = build_reality_check(p, manifold_markets)
        except Exception as e:
            print(f"[scanner] reality_check failed for pair: {e}")
            p["reality_check"] = {"sources": [], "verdict": None}

    # Category breakdown for matched pairs
    by_cat: dict[str, list] = {}
    for p in pairs:
        by_cat.setdefault(p["category"], []).append(p)

    # Top single-platform picks per category (high-volume, non-extreme probability)
    # These show up even when no cross-platform match exists — gives the user
    # something useful in every category (weather, sports, finance, etc.)
    def _top_in_category(markets, cat, n=8):
        cands = [m for m in markets
                 if m["category"] == cat
                 and not _is_subrange(m["title"])
                 and 0.05 <= m["yes_prob"] <= 0.95
                 and m["volume"] > 0]
        cands.sort(key=lambda m: m["volume"], reverse=True)
        return [{
            "platform":  m["platform"],
            "title":     m["title"],
            "yes_prob":  m["yes_prob"],
            "yes_pct":   round(m["yes_prob"] * 100, 1),
            "volume":    m["volume"],
            "url":       m["url"],
            "category":  cat,
        } for m in cands[:n]]

    all_markets = kalshi_markets + poly_markets
    top_by_category = {}
    for cat in CATEGORY_LABELS:
        picks = _top_in_category(all_markets, cat)
        if picks:
            top_by_category[cat] = {
                "label": CATEGORY_LABELS[cat],
                "color": CATEGORY_COLORS.get(cat, "#6b7280"),
                "picks": picks,
            }

    stats = {
        "total_kalshi":    len(kalshi_markets),
        "total_polymarket":len(poly_markets),
        "matches_found":   len(pairs),
        "large_edges":     sum(1 for p in pairs if p["edge_pct"] >= 5),
        "arb_count":       sum(1 for p in pairs if p.get("hedge", {}).get("is_arb")),
        "by_category":     {cat: len(ms) for cat, ms in by_cat.items()},
        "scan_time":       started,
    }

    print(f"[scanner] Done. {len(pairs)} matched pairs, {stats['large_edges']} with edge ≥5%, {stats['arb_count']} arbs")
    return {"stats": stats, "pairs": pairs, "top_by_category": top_by_category}


# ── CLI pretty-print ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    min_edge = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0

    result = run_scan()
    stats  = result["stats"]
    pairs  = result["pairs"]

    print("\n" + "═" * 90)
    print(f"  PREDICTION MARKET DISCREPANCY SCANNER  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("═" * 90)
    print(f"  Kalshi: {stats['total_kalshi']} markets  |  "
          f"Polymarket: {stats['total_polymarket']} markets  |  "
          f"Matched pairs: {stats['matches_found']}  |  "
          f"Edge ≥5%: {stats['large_edges']}")
    print("═" * 90)

    filtered = [p for p in pairs if p["edge_pct"] >= min_edge]
    if not filtered:
        print(f"\n  No pairs with edge ≥ {min_edge}% found.")
    else:
        print(f"\n  Showing {len(filtered)} pairs with edge ≥ {min_edge}%\n")

    for i, p in enumerate(filtered, 1):
        k = p["kalshi"]
        pm = p["polymarket"]
        print(f"  #{i:02d}  [{p['category_label'].upper()}]  Edge: {p['edge_pct']:.1f}pp")
        print(f"       Kalshi:     {k['yes_prob']*100:.1f}%  —  {k['title'][:70]}")
        print(f"       Polymarket: {pm['yes_prob']*100:.1f}%  —  {pm['title'][:70]}")
        print(f"       → {p['plain_english']}")
        print(f"       Quarter-Kelly size: {p['kelly']*100:.1f}% of bankroll")
        print()
