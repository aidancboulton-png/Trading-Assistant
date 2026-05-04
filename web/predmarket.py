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
    "the", "a", "an", "in", "on", "at", "will", "be", "to", "of", "by",
    "for", "is", "are", "was", "above", "below", "end", "year", "before",
    "after", "this", "that", "or", "and", "if", "than", "more", "less",
    "over", "under", "between", "within", "reach", "hit", "exceed", "go",
    "up", "down", "out", "into", "with", "from", "their", "its", "have",
    "has", "had", "would", "could", "should", "may", "might", "does", "do",
}

# ── Utilities ─────────────────────────────────────────────────────────────────

def _normalize(title: str) -> set:
    """Tokenize a market title into a set of meaningful tokens."""
    t = re.sub(r"[^\w\s$%]", " ", title.lower())
    tokens = set(t.split()) - STOP_WORDS
    # keep dollar amounts and percentages as single tokens
    tokens |= set(re.findall(r"\$[\d,]+[kmb]?|[\d.]+%|[\d]{4}", title.lower()))
    return tokens


def _similarity(a: str, b: str) -> float:
    """Jaccard similarity on cleaned tokens."""
    ta, tb = _normalize(a), _normalize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


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
    """Fetch active Kalshi markets. Returns normalised list."""
    markets = []
    cursor = None

    for _ in range(max_pages):
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{KALSHI_BASE}/markets", params=params,
                             headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[kalshi] fetch error: {e}")
            break

        raw = data.get("markets", [])
        for m in raw:
            # Skip non-active or settled markets
            if m.get("status") not in ("active", "open"):
                continue
            # Skip multi-leg combo markets (title is a CSV of legs)
            title = m.get("title", "") or m.get("yes_sub_title", "")
            if not title or title.count(",") > 2:
                continue

            # Prices are dollar-denominated floats already in [0, 1]
            def _to_float(v) -> Optional[float]:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            yes_bid = _to_float(m.get("yes_bid_dollars"))
            yes_ask = _to_float(m.get("yes_ask_dollars"))
            last    = _to_float(m.get("last_price_dollars"))

            mid = _mid(yes_bid, yes_ask)
            # Fall back to last traded price if bid/ask are both zero or missing
            if mid is None or mid < 0.001:
                if last and last > 0.001:
                    mid = last
                else:
                    continue

            if not (0.01 <= mid <= 0.99):
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
                "url":       f"https://kalshi.com/markets/{m.get('event_ticker', m.get('ticker', ''))}",
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

MATCH_THRESHOLD = 0.30  # Jaccard similarity floor for a pair to be considered


def match_markets(kalshi: list[dict], polymarket: list[dict]) -> list[dict]:
    """
    Cross-match Kalshi and Polymarket markets.
    Returns pairs sorted by discrepancy (largest edge first).
    """
    pairs = []
    poly_by_cat: dict[str, list[dict]] = {}
    for m in polymarket:
        poly_by_cat.setdefault(m["category"], []).append(m)

    for km in kalshi:
        cat = km["category"]
        # search same category first, then "other" as fallback
        candidates = poly_by_cat.get(cat, []) + poly_by_cat.get("other", [])
        best_score = 0.0
        best_pm = None
        for pm in candidates:
            score = _similarity(km["title"], pm["title"])
            if score > best_score:
                best_score = score
                best_pm = pm

        if best_pm is None or best_score < MATCH_THRESHOLD:
            continue

        k_prob = km["yes_prob"]
        p_prob = best_pm["yes_prob"]
        edge   = abs(k_prob - p_prob)

        if edge < 0.02:
            continue  # skip trivial differences

        # who's higher?
        if k_prob > p_prob:
            higher, lower = "Kalshi", "Polymarket"
            buy_advice = f"YES cheaper on Polymarket ({p_prob*100:.1f}¢) — Kalshi prices it at {k_prob*100:.1f}¢"
        else:
            higher, lower = "Polymarket", "Kalshi"
            buy_advice = f"YES cheaper on Kalshi ({k_prob*100:.1f}¢) — Polymarket prices it at {p_prob*100:.1f}¢"

        # rough kelly (quarter kelly, conservative)
        kelly = round(edge / max(1 - min(k_prob, p_prob), 0.01) * 0.25, 3)

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
            "kalshi": {
                "title":    km["title"],
                "yes_prob": km["yes_prob"],
                "volume":   km["volume"],
                "url":      km["url"],
                "id":       km["id"],
            },
            "polymarket": {
                "title":    best_pm["title"],
                "yes_prob": best_pm["yes_prob"],
                "volume":   best_pm["volume"],
                "url":      best_pm["url"],
                "id":       best_pm["id"],
            },
        })

    pairs.sort(key=lambda x: x["edge"], reverse=True)
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

    # Annotate with plain English
    for p in pairs:
        p["plain_english"] = plain_english(p)

    # Category breakdown
    by_cat: dict[str, list] = {}
    for p in pairs:
        by_cat.setdefault(p["category"], []).append(p)

    stats = {
        "total_kalshi":    len(kalshi_markets),
        "total_polymarket":len(poly_markets),
        "matches_found":   len(pairs),
        "large_edges":     sum(1 for p in pairs if p["edge_pct"] >= 5),
        "by_category":     {cat: len(ms) for cat, ms in by_cat.items()},
        "scan_time":       started,
    }

    print(f"[scanner] Done. {len(pairs)} matched pairs, {stats['large_edges']} with edge ≥5%")
    return {"stats": stats, "pairs": pairs}


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
