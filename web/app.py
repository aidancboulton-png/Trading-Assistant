import os, json, time, asyncio, math
from pathlib import Path
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from typing import List
from web.predmarket import run_scan
from web.podcasts import (
    list_episodes as podcast_list_episodes,
    poll_and_process as podcast_poll,
    SUBSCRIBED as PODCAST_SUBSCRIBED,
)
from web.sports import (
    league_board as sports_board,
    event_props as sports_event_props,
    leagues_index as sports_leagues,
)
from jarvis.newsengine import aggregate as news_aggregate
from jarvis.scriptwriter import generate_script, generate_short

# Shared thread pool for parallel external API calls
_POOL = ThreadPoolExecutor(max_workers=16)

PT = ZoneInfo("America/Los_Angeles")
_BASE_DIR   = Path(__file__).parent.parent
ALERTS_PATH = str(_BASE_DIR / ".alerts_history.json")
LAST_PATH   = str(_BASE_DIR / ".last_prices.json")

WATCHLIST = {
    # ── Indices ──────────────────────────────────────────────────────────────
    "ES":   {"name": "S&P 500",      "finnhub": "SPY",              "alert_pct": 0.4,  "type": "index"},
    "NQ":   {"name": "Nasdaq",       "finnhub": "QQQ",              "alert_pct": 0.5,  "type": "index"},
    "DJI":  {"name": "Dow Jones",    "finnhub": "DIA",              "alert_pct": 0.4,  "type": "index"},
    "RUT":  {"name": "Russell 2000", "finnhub": "IWM",              "alert_pct": 0.6,  "type": "index"},
    "VIX":  {"name": "Volatility",   "finnhub": "VIXY",             "alert_pct": 5.0,  "type": "index"},
    # ── Commodities ──────────────────────────────────────────────────────────
    "CL":   {"name": "Crude Oil",    "finnhub": "USO",              "alert_pct": 0.8,  "type": "commodity"},
    "GC":   {"name": "Gold",         "finnhub": "GLD",              "alert_pct": 0.5,  "type": "commodity"},
    "DXY":  {"name": "USD Index",    "finnhub": "UUP",              "alert_pct": 0.3,  "type": "forex"},
    # ── Crypto ───────────────────────────────────────────────────────────────
    "BTC":  {"name": "Bitcoin",      "finnhub": "BINANCE:BTCUSDT",  "alert_pct": 2.0,  "type": "crypto"},
    "ETH":  {"name": "Ethereum",     "finnhub": "BINANCE:ETHUSDT",  "alert_pct": 2.5,  "type": "crypto"},
    "SOL":  {"name": "Solana",       "finnhub": "BINANCE:SOLUSDT",  "alert_pct": 3.0,  "type": "crypto"},
    # ── Key Stocks ───────────────────────────────────────────────────────────
    "NVDA": {"name": "Nvidia",       "finnhub": "NVDA",             "alert_pct": 1.5,  "type": "stock"},
    "AAPL": {"name": "Apple",        "finnhub": "AAPL",             "alert_pct": 1.0,  "type": "stock"},
    "TSLA": {"name": "Tesla",        "finnhub": "TSLA",             "alert_pct": 2.0,  "type": "stock"},
    "META": {"name": "Meta",         "finnhub": "META",             "alert_pct": 1.5,  "type": "stock"},
    "AMZN": {"name": "Amazon",       "finnhub": "AMZN",             "alert_pct": 1.2,  "type": "stock"},
    "MSFT": {"name": "Microsoft",    "finnhub": "MSFT",             "alert_pct": 1.0,  "type": "stock"},
    "JPM":  {"name": "JPMorgan",     "finnhub": "JPM",              "alert_pct": 1.2,  "type": "stock"},
    "XOM":  {"name": "ExxonMobil",   "finnhub": "XOM",              "alert_pct": 1.0,  "type": "stock"},
}

# Read API keys from env vars (Railway) or fall back to config.json (local dev)
def _read_cfg() -> dict:
    try:
        return json.load(open(_BASE_DIR / "config.json"))
    except Exception:
        return {}

_cfg = _read_cfg()

def _key(env_name: str, cfg_name: str) -> str:
    return os.environ.get(env_name, "").strip() or _cfg.get(cfg_name, "")

FKEY = _key("FINNHUB_API_KEY", "finnhub_api_key")

# Inject config.json keys into env so llm_router + discord_bot pick them up
for _evar, _ckey in [
    ("ANTHROPIC_API_KEY", "anthropic_api_key"),
    ("GEMINI_API_KEY",    "gemini_api_key"),
]:
    if not os.environ.get(_evar) and _cfg.get(_ckey):
        os.environ[_evar] = _cfg[_ckey]

# ── helpers ──────────────────────────────────────────────────────────────────

def fh(path: str, params: dict) -> dict:
    params["token"] = FKEY
    r = requests.get(f"https://finnhub.io/api/v1{path}", params=params, timeout=12)
    return r.json()

_quote_cache: dict = {}  # last-known-good quotes, never evicted

def _yahoo_quote(symbol: str) -> dict | None:
    """Fallback: fetch current quote from Yahoo Finance (free, no key)."""
    ticker = _YAHOO_SYMBOL_MAP.get(symbol, symbol.replace("BINANCE:", "").replace("USDT", "-USD"))
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        r = requests.get(url, params={"interval": "1d", "range": "5d"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        d = r.json()
        res = d["chart"]["result"][0]
        meta = res["meta"]
        c = meta.get("regularMarketPrice") or meta.get("previousClose") or 0
        pc = meta.get("chartPreviousClose") or meta.get("previousClose") or 1
        h = meta.get("regularMarketDayHigh") or c
        lo = meta.get("regularMarketDayLow") or c
        return {"current": round(c, 4), "prev_close": round(pc, 4),
                "high": round(h, 4), "low": round(lo, 4),
                "change_pct": round(((c - pc) / pc) * 100, 2) if pc else 0}
    except:
        return None

def get_quote(symbol: str) -> dict:
    try:
        d = fh("/quote", {"symbol": symbol})
        # 429 rate limit — Finnhub returns {"error": "..."}
        if "error" in d or not d.get("c"):
            raise ValueError("finnhub_limit")
        c, pc = d.get("c") or 0, d.get("pc") or 1
        q = {"current": round(c, 4), "prev_close": round(pc, 4),
             "high": round(d.get("h") or 0, 4), "low": round(d.get("l") or 0, 4),
             "change_pct": round(((c - pc) / pc) * 100, 2) if pc else 0}
        _quote_cache[symbol] = q  # store last-known-good
        return q
    except:
        # Try Yahoo Finance fallback
        yq = _yahoo_quote(symbol)
        if yq and yq["current"] > 0:
            _quote_cache[symbol] = yq
            return yq
        # Return last-known-good if available, otherwise zeros
        return _quote_cache.get(symbol, {"current": 0, "prev_close": 0, "high": 0, "low": 0, "change_pct": 0})

def build_snapshot() -> dict:
    """Parallel fetch of all WATCHLIST quotes. ~6s → ~0.5s."""
    keys = list(WATCHLIST.keys())
    quotes = list(_POOL.map(lambda k: get_quote(WATCHLIST[k]["finnhub"]), keys))
    return {k: {**WATCHLIST[k], **q} for k, q in zip(keys, quotes)}

_YAHOO_SYMBOL_MAP = {
    "USO": "USO", "SPY": "SPY", "QQQ": "QQQ", "DIA": "DIA", "IWM": "IWM",
    "VIXY": "VIXY", "GLD": "GLD", "UUP": "UUP",
    "NVDA": "NVDA", "AAPL": "AAPL", "TSLA": "TSLA", "META": "META",
    "AMZN": "AMZN", "MSFT": "MSFT", "JPM": "JPM", "XOM": "XOM",
}

def get_candles(symbol: str, count: int = 220) -> list:
    """Fetch daily closes from Yahoo Finance (free, no API key needed)."""
    ticker = _YAHOO_SYMBOL_MAP.get(symbol, symbol)
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        r = requests.get(url, params={"interval": "1d", "range": "1y"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        d = r.json()
        closes = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        return [c for c in closes if c is not None]
    except:
        return []

def compute_rsi(closes: list, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas[-period:]]
    losses = [max(-d, 0) for d in deltas[-period:]]
    ag, al = sum(gains) / period, sum(losses) / period
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)

def compute_ema(closes: list, period: int) -> float | None:
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 4)

def compute_macd(closes: list) -> dict:
    if len(closes) < 26:
        return {"macd": None, "trend": "neutral"}
    ema12 = compute_ema(closes, 12)
    ema26 = compute_ema(closes, 26)
    if not ema12 or not ema26:
        return {"macd": None, "trend": "neutral"}
    macd = ema12 - ema26
    return {"macd": round(macd, 4), "trend": "bullish" if macd > 0 else "bearish"}

def compute_bollinger(closes: list, period: int = 20) -> dict:
    if len(closes) < period:
        return {"signal": "neutral", "position": None}
    recent = closes[-period:]
    mean   = sum(recent) / period
    std    = (sum((c - mean) ** 2 for c in recent) / period) ** 0.5
    upper, lower = mean + 2 * std, mean - 2 * std
    price  = closes[-1]
    pos    = round((price - lower) / (upper - lower), 2) if upper != lower else 0.5
    return {"upper": round(upper, 4), "lower": round(lower, 4), "middle": round(mean, 4),
            "position": pos, "signal": "overbought" if pos > 0.8 else "oversold" if pos < 0.2 else "neutral"}

def get_rsi_all() -> dict:
    result = {}
    for sym, info in WATCHLIST.items():
        if info["type"] == "crypto":
            continue  # crypto excluded
        closes = get_candles(info["finnhub"])
        rsi    = compute_rsi(closes)
        signal = "overbought" if rsi and rsi > 70 else "oversold" if rsi and rsi < 30 else "neutral"
        result[sym] = {"rsi": rsi, "signal": signal, "name": info["name"]}
        time.sleep(0.3)
    return result

def get_technicals_all() -> dict:
    result = {}
    for sym, info in WATCHLIST.items():
        if info["type"] == "crypto":
            continue  # crypto excluded from technicals
        closes = get_candles(info["finnhub"], count=220)
        if len(closes) < 20:
            result[sym] = {"name": info["name"], "type": info["type"], "available": False}
            time.sleep(0.3)
            continue
        price  = closes[-1]
        rsi    = compute_rsi(closes)
        macd   = compute_macd(closes)
        bb     = compute_bollinger(closes)
        sma20  = round(sum(closes[-20:]) / 20, 4)
        sma50  = round(sum(closes[-50:]) / 50, 4) if len(closes) >= 50 else None
        sma200 = round(sum(closes[-200:]) / 200, 4) if len(closes) >= 200 else None
        vs50   = ("above" if price > sma50  else "below") if sma50  else None
        vs200  = ("above" if price > sma200 else "below") if sma200 else None
        golden = (sma50 > sma200) if (sma50 and sma200) else None
        # score out of 6
        pts = sum([
            rsi is not None and 40 < rsi < 70,
            vs50  == "above",
            vs200 == "above",
            golden is True,
            macd.get("trend") == "bullish",
            bb.get("signal") == "neutral",
        ])
        overall = "bullish" if pts >= 4 else "bearish" if pts <= 1 else "neutral"
        result[sym] = {
            "name": info["name"], "type": info["type"], "available": True,
            "rsi": rsi,
            "rsi_signal": "overbought" if rsi and rsi > 70 else "oversold" if rsi and rsi < 30 else "neutral",
            "sma50": sma50, "sma200": sma200,
            "vs_sma50": vs50, "vs_sma200": vs200,
            "golden_cross": golden,
            "macd_trend": macd.get("trend"),
            "bb_signal": bb.get("signal"),
            "bb_position": bb.get("position"),
            "overall": overall,
        }
        time.sleep(0.3)
    return result

def _score_recs(r: dict) -> float:
    """Weighted score: -2..+2. +2 = unanimous strong buy, -2 = unanimous strong sell."""
    sb, b, h, s, ss = (r.get("strongBuy",0), r.get("buy",0), r.get("hold",0),
                       r.get("sell",0), r.get("strongSell",0))
    total = sb + b + h + s + ss
    if not total:
        return 0.0
    return round((2*sb + 1*b + 0*h - 1*s - 2*ss) / total, 2)

def get_analyst_recs() -> dict:
    """Per-stock analyst consensus + 3-month trend + price target if available."""
    result = {}
    stocks = [s for s, i in WATCHLIST.items() if i["type"] == "stock"]

    def _one(sym):
        try:
            d = fh("/stock/recommendation", {"symbol": sym})
            if not d:
                return sym, None
            latest = d[0]
            prior  = d[3] if len(d) > 3 else (d[-1] if len(d) > 1 else None)

            sb = latest.get("strongBuy",0); b = latest.get("buy",0)
            h  = latest.get("hold",0);      s = latest.get("sell",0)
            ss = latest.get("strongSell",0)
            total = sb + b + h + s + ss
            buys, sells = sb + b, s + ss
            consensus = "buy" if buys > sells and buys > total*0.4 else \
                        "sell" if sells > buys and sells > total*0.4 else "hold"
            score = _score_recs(latest)
            prior_score = _score_recs(prior) if prior else None
            trend = round(score - prior_score, 2) if prior_score is not None else None

            # Price target (best-effort — Finnhub free tier may return blanks)
            target = None
            try:
                tt = fh("/stock/price-target", {"symbol": sym})
                target = {
                    "median": tt.get("targetMedian"),
                    "high":   tt.get("targetHigh"),
                    "low":    tt.get("targetLow"),
                    "n":      tt.get("numberOfAnalysts"),
                }
                if not target.get("median"):
                    target = None
            except:
                target = None

            return sym, {
                "strongBuy": sb, "buy": b, "hold": h, "sell": s, "strongSell": ss,
                "total": total, "consensus": consensus, "score": score,
                "prior_score": prior_score, "trend": trend,
                "period": latest.get("period",""),
                "target": target,
            }
        except:
            return sym, None

    # Parallel fetch — Finnhub allows ~30 req/sec
    for sym, info in _POOL.map(_one, stocks):
        if info:
            result[sym] = info
    return result

# ── Correlations engine ──────────────────────────────────────────────────────
# Each correlation is a known pair, the expected sign, and a plain-English
# explanation of WHY it normally holds. We compare the snapshot's daily change
# percent and flag whether the relationship held today.
_CORRELATIONS = [
    {
        "pair":     "DXY ↑  →  Gold ↓",
        "a":        "DXY",       "b": "GC",        "expected": "inverse",
        "why":      "A stronger dollar makes gold (priced in dollars) more expensive globally, suppressing demand. When this inverse breaks, it usually means fear of inflation or de-dollarization is overriding the math.",
    },
    {
        "pair":     "DXY ↑  →  Bitcoin ↓",
        "a":        "DXY",       "b": "BTC",       "expected": "inverse",
        "why":      "Tighter dollar liquidity drains risk assets. BTC behaves like long-duration tech in macro terms — when the dollar rallies, crypto usually fades.",
    },
    {
        "pair":     "VIX ↑  →  S&P 500 ↓",
        "a":        "VIX",       "b": "ES",        "expected": "inverse",
        "why":      "VIX measures expected volatility — it spikes when investors buy puts. Falling stocks + rising VIX = panic hedging. If both rise together, that's unusual and usually short-lived.",
    },
    {
        "pair":     "Oil ↑  →  Energy stocks ↑",
        "a":        "CL",        "b": "XOM",       "expected": "positive",
        "why":      "Oil majors' earnings are tied to crude prices. When oil rallies, XOM and the energy sector rally with it. A divergence usually signals something company-specific.",
    },
    {
        "pair":     "S&P 500 ↑  →  Bitcoin ↑",
        "a":        "ES",        "b": "BTC",       "expected": "positive",
        "why":      "Both are risk assets in modern markets. When liquidity expands and confidence rises, both rally. When BTC leads stocks down, it's a leading indicator of risk-off.",
    },
    {
        "pair":     "Banks ↑  →  Yields rising",
        "a":        "JPM",       "b": "ES",        "expected": "positive",
        "why":      "Banks earn the spread between what they pay depositors and what they charge borrowers. Steeper yield curves → fatter margins. JPM rallying alongside the broader market suggests credit conditions are healthy.",
    },
    {
        "pair":     "NVDA ↑  →  Nasdaq ↑",
        "a":        "NVDA",      "b": "NQ",        "expected": "positive",
        "why":      "Nvidia is the largest single weight in the AI/tech complex. Strong NVDA pulls the entire Nasdaq higher. NVDA weak while QQQ holds up suggests a rotation out of AI into other tech.",
    },
]

def get_correlations() -> list:
    """Compute today's correlation state from the cached snapshot."""
    snap = cached("snapshot", build_snapshot)
    out = []
    for rule in _CORRELATIONS:
        a = snap.get(rule["a"], {}).get("change_pct")
        b = snap.get(rule["b"], {}).get("change_pct")
        if a is None or b is None:
            continue

        if rule["expected"] == "inverse":
            held = (a >= 0 and b <= 0) or (a <= 0 and b >= 0)
        else:
            held = (a >= 0 and b >= 0) or (a <= 0 and b <= 0)

        # If both moves are tiny (<0.1%), call it weak
        if abs(a) < 0.1 and abs(b) < 0.1:
            state = "weak"
        else:
            state = "normal" if held else "broken"

        out.append({
            "pair":   rule["pair"],
            "why":    rule["why"],
            "a_pct":  round(a, 2),
            "b_pct":  round(b, 2),
            "state":  state,
            "label":  {"normal":"Holding","broken":"BROKEN","weak":"Quiet"}[state],
        })
    return out

def generate_analysis(snap: dict, fear_greed: dict, news: list) -> dict:
    es  = snap.get("ES",  {}); nq  = snap.get("NQ",  {})
    vix = snap.get("VIX", {}); btc = snap.get("BTC", {})
    cl  = snap.get("CL",  {}); gc  = snap.get("GC",  {})
    dxy = snap.get("DXY", {}); nvda = snap.get("NVDA",{})
    jpm = snap.get("JPM", {})

    es_chg  = es.get("change_pct",  0); nq_chg  = nq.get("change_pct",  0)
    vix_val = vix.get("current",   20); btc_chg = btc.get("change_pct", 0)
    cl_chg  = cl.get("change_pct",  0); gc_chg  = gc.get("change_pct",  0)
    dxy_chg = dxy.get("change_pct", 0); fg_val  = fear_greed.get("value", 50)

    up_count = sum(1 for d in snap.values() if d.get("change_pct", 0) > 0)
    total    = len(snap)

    # Mood
    if es_chg > 0.8 and vix_val < 22:
        mood, mood_color = "RISK ON", "green"
        mood_desc = "Markets rallying with low fear. Investors are confident and buying risk assets."
    elif es_chg < -0.8 and vix_val > 22:
        mood, mood_color = "RISK OFF", "red"
        mood_desc = "Stocks falling while fear rises. Investors are protecting capital and avoiding risk."
    elif vix_val > 30:
        mood, mood_color = "HIGH FEAR", "red"
        mood_desc = f"VIX at {vix_val:.0f} — extreme volatility. The market is scared. Expect large swings."
    elif abs(es_chg) < 0.25:
        mood, mood_color = "FLAT", "yellow"
        mood_desc = "Markets have no clear direction today. Traders waiting for a catalyst."
    elif es_chg > 0:
        mood, mood_color = "BULLISH", "green"
        mood_desc = "Stocks are climbing. More buyers than sellers in the market right now."
    else:
        mood, mood_color = "BEARISH", "red"
        mood_desc = "Stocks under pressure. Sellers are in control today."

    # Plain English summary
    sp_word = f"rose {es_chg:.1f}%" if es_chg > 0 else f"fell {abs(es_chg):.1f}%"
    lines = [f"The S&P 500 {sp_word} today, with {up_count} of {total} tracked assets positive."]
    if vix_val > 25:
        lines.append(f"The fear gauge (VIX) sits at {vix_val:.0f}, meaning markets are nervous and volatile.")
    if abs(btc_chg) > 1.5:
        lines.append(f"Bitcoin {'surged' if btc_chg > 0 else 'fell'} {abs(btc_chg):.1f}%, {'amplifying' if btc_chg * es_chg > 0 else 'diverging from'} the stock trend.")
    if abs(cl_chg) > 1.5:
        lines.append(f"Oil {'jumped' if cl_chg > 0 else 'dropped'} {abs(cl_chg):.1f}%, which {'pushes inflation up' if cl_chg > 0 else 'relieves inflation pressure'}.")

    signals = []

    # VIX
    if vix_val > 30:
        signals.append({"kind": "vol", "title": "Market in Panic Mode", "type": "danger",
            "plain": f"VIX at {vix_val:.0f} is panic territory. Markets can swing wildly. Don't make rushed decisions.",
            "expert": f"VIX {vix_val:.1f} → implied daily SPX move ~{vix_val/16:.1f}%. Vol term structure likely inverted. Short-vol strategies at max risk."})
    elif vix_val > 20:
        signals.append({"kind": "vol", "title": "Volatility is Elevated", "type": "warning",
            "plain": f"The fear index is at {vix_val:.0f}. Markets are jittery. Be careful with large bets.",
            "expert": f"VIX {vix_val:.1f} above 20 threshold. Dealer hedging pressure elevated. Expect gap risk and wider spreads."})
    elif vix_val < 13:
        signals.append({"kind": "vol", "title": "Markets Are Too Calm", "type": "info",
            "plain": f"VIX at {vix_val:.0f} — markets feel very safe. But extreme calm often comes before a storm.",
            "expert": f"VIX {vix_val:.1f} near complacency levels. Low vol regime risk. Tail hedges are cheap — consider adding protection."})

    # Fear & Greed
    if fg_val <= 25:
        signals.append({"kind": "sent", "title": "Extreme Fear — Possible Opportunity", "type": "opportunity",
            "plain": f"Fear & Greed index at {fg_val}/100. Historically, when everyone is scared, prices are low and smart money buys.",
            "expert": f"CNN F&G {fg_val} (Extreme Fear). Contrarian long signal. Check if fundamental catalyst justifies fear or if this is sentiment overshoot."})
    elif fg_val >= 75:
        signals.append({"kind": "sent", "title": "Extreme Greed — Be Cautious", "type": "warning",
            "plain": f"Fear & Greed at {fg_val}/100. Everyone is greedy — this is when bubbles form. Markets may be stretched.",
            "expert": f"CNN F&G {fg_val} (Extreme Greed). Sentiment overbought. Mean reversion risk elevated. Trim overweight positions."})

    # DXY vs Gold
    if dxy_chg > 0.4 and gc_chg < -0.3:
        signals.append({"kind": "fx", "title": "Strong Dollar, Weak Gold", "type": "info",
            "plain": "The US Dollar is gaining strength while Gold falls. Investors trust the US economy and want dollars over safe havens.",
            "expert": f"DXY proxy +{dxy_chg:.2f}% / GLD {gc_chg:.2f}%. Real rate pickup likely. Dollar strength negative for commodities and EM assets."})
    elif dxy_chg < -0.4 and gc_chg > 0.3:
        signals.append({"kind": "fx", "title": "Dollar Weakening, Gold Rising", "type": "warning",
            "plain": "The dollar is falling and gold is rising — a classic signal that investors are worried about inflation or economic instability.",
            "expert": f"DXY proxy {dxy_chg:.2f}% / GLD +{gc_chg:.2f}%. Real rates pressured lower. Dollar weakness supportive of commodities and risk assets."})

    # Oil
    if cl_chg > 2.5:
        signals.append({"kind": "energy", "title": "Oil Spiking — Watch Inflation", "type": "danger",
            "plain": f"Crude oil is up {cl_chg:.1f}% today. Higher oil means higher gas prices and more inflation — bad for most consumers, great for energy stocks like XOM.",
            "expert": f"USO +{cl_chg:.2f}%. Energy sector should outperform. Negative for growth/tech via rate expectations. Monitor TIPS breakevens."})
    elif cl_chg < -2.5:
        signals.append({"kind": "energy", "title": "Oil Dropping — Inflation Relief", "type": "opportunity",
            "plain": f"Crude oil fell {abs(cl_chg):.1f}%. Cheaper oil means cheaper gas and less inflation — a positive for the overall economy and consumer stocks.",
            "expert": f"USO {cl_chg:.2f}%. Disinflationary input cost signal. Positive for margin expansion in industrials and consumer discretionary."})

    # BTC risk barometer
    if btc_chg > 4 and es_chg > 0:
        signals.append({"kind": "crypto", "title": "Crypto + Stocks Both Surging", "type": "bullish",
            "plain": f"Bitcoin up {btc_chg:.1f}% alongside stocks — maximum 'risk-on' signal. Investors are in full buying mode across all markets.",
            "expert": "Cross-asset risk appetite elevated. BTC/equity correlation positive. Rotate into high-beta: growth tech, small caps, crypto alts."})
    elif btc_chg < -4 and es_chg < 0:
        signals.append({"kind": "crypto", "title": "Crypto + Stocks Both Falling", "type": "danger",
            "plain": f"Bitcoin down {abs(btc_chg):.1f}% with stocks also falling — everything selling off at once. Classic de-risking.",
            "expert": "Macro deleveraging event. BTC leading risk-off. Watch high-yield credit spreads and USD/JPY for confirmation of severity."})

    # NVDA / AI signal
    if nvda.get("change_pct", 0) > 3:
        signals.append({"kind": "tech", "title": "AI Stocks Leading the Market", "type": "bullish",
            "plain": f"NVDA up {nvda.get('change_pct',0):.1f}%. AI/tech names are the strongest performers today — the market believes in the AI trade.",
            "expert": "Mega-cap AI outperforming. Semis acting as risk-on leading indicator. Watch SOX index for sustainability."})
    elif nvda.get("change_pct", 0) < -3:
        signals.append({"kind": "tech", "title": "Tech/AI Stocks Under Pressure", "type": "warning",
            "plain": f"NVDA down {abs(nvda.get('change_pct',0)):.1f}%. Tech is lagging — could signal rotation away from growth into value.",
            "expert": "Semis underperforming. Growth/duration risk-off. Check 10yr yield — if rising, that explains the tech pressure."})

    # Banks
    if abs(jpm.get("change_pct", 0)) > 1.5:
        up = jpm.get("change_pct", 0) > 0
        signals.append({"kind": "banks", "title": f"Banking Sector {'Rallying' if up else 'Selling Off'}", "type": "bullish" if up else "warning",
            "plain": f"JPMorgan {'up' if up else 'down'} {abs(jpm.get('change_pct',0)):.1f}%. Banks are a barometer of economic health. {'Positive signal' if up else 'Watch for broader credit stress'}.",
            "expert": f"JPM {jpm.get('change_pct',0):.2f}%. {'Yield curve steepening / credit expansion signal.' if up else 'Potential NIM compression or credit risk concerns. Watch CDS spreads.'}"})

    # Geopolitical news
    geo = [n for n in news if n.get("category") == "geopolitical"][:1]
    if geo:
        signals.append({"kind": "geo", "title": "Geopolitical Event in the News", "type": "warning",
            "plain": f"{geo[0]['headline'][:120]}. Geopolitical events can cause sudden, sharp moves — especially in oil and defense stocks.",
            "expert": "Active geo risk. Monitor: crude oil, USD, defense sector (LMT/RTX/NOC/GD), safe havens (GLD, TLT). Tail risk elevated."})

    # Top movers
    movers = sorted(
        [(s, d) for s, d in snap.items() if d.get("change_pct")],
        key=lambda x: abs(x[1].get("change_pct", 0)), reverse=True
    )[:6]

    return {
        "mood": mood, "mood_color": mood_color, "mood_desc": mood_desc,
        "simple_summary": " ".join(lines),
        "signals": signals,
        "top_movers": [{"symbol": s, "name": d.get("name", s), "change_pct": d.get("change_pct", 0),
                        "current": d.get("current", 0), "type": d.get("type", "")} for s, d in movers],
        "stats": {"up_count": up_count, "down_count": total - up_count, "total": total,
                  "vix": round(vix_val, 2), "fear_greed": fg_val},
        "timestamp": int(time.time()),
    }

# Keywords used to tag geopolitical / macro / earnings news from general feed
_GEO_KEYWORDS   = ["iran","war","strike","attack","military","sanction","missile",
                    "conflict","ukraine","russia","china","taiwan","middle east",
                    "opec","oil supply","troops","nato","pentagon","nuclear","tariff",
                    "trade war","embargo"]
_MACRO_KEYWORDS = ["fed","federal reserve","rate","inflation","cpi","gdp","recession",
                    "treasury","yield","powell","interest rate","jobs report","payroll",
                    "fomc","deficit","debt ceiling","stimulus","fiscal","monetary"]
_EARNINGS_KEYWORDS = ["earnings","revenue","profit","beat","miss","guidance","eps",
                       "quarterly","results","outlook","forecast","raised","lowered"]

def _tag_category(headline: str, summary: str, base_cat: str) -> str:
    text = (headline + " " + summary).lower()
    if any(k in text for k in _GEO_KEYWORDS):
        return "geopolitical"
    if any(k in text for k in _MACRO_KEYWORDS):
        return "macro"
    if any(k in text for k in _EARNINGS_KEYWORDS):
        return "earnings"
    return base_cat

def _fetch_market_news(cat: str) -> list:
    try:
        items = fh("/news", {"category": cat, "minId": 0})
    except:
        return []
    out = []
    for n in items[:20]:
        headline = n.get("headline", "")
        summary  = (n.get("summary") or "")[:220]
        out.append({
            "id":       n.get("id", 0),
            "headline": headline,
            "summary":  summary,
            "source":   n.get("source", ""),
            "url":      n.get("url", ""),
            "datetime": n.get("datetime", 0),
            "category": _tag_category(headline, summary, cat),
            "image":    n.get("image", ""),
            "related":  n.get("related", ""),
        })
    return out

def _fetch_company_news(sym: str) -> list:
    try:
        fr = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        to = datetime.now().strftime("%Y-%m-%d")
        items = fh("/company-news", {"symbol": sym, "from": fr, "to": to})
    except:
        return []
    out = []
    for n in items[:4]:
        headline = n.get("headline", "")
        summary  = (n.get("summary") or "")[:220]
        out.append({
            "id":       n.get("id", 0),
            "headline": headline,
            "summary":  summary,
            "source":   n.get("source", sym),
            "url":      n.get("url", ""),
            "datetime": n.get("datetime", 0),
            "category": _tag_category(headline, summary, "earnings"),
            "image":    n.get("image", ""),
            "related":  sym,
        })
    return out

def get_news() -> list:
    """Parallel fetch of market + company news. ~2.8s → ~0.5s."""
    cats   = ("general", "crypto", "forex", "merger")
    syms   = ("AAPL", "NVDA", "MSFT", "META", "AMZN", "TSLA", "JPM", "XOM")
    market = _POOL.map(_fetch_market_news, cats)
    company = _POOL.map(_fetch_company_news, syms)

    news, seen = [], set()
    for batch in list(market) + list(company):
        for item in batch:
            nid = item.get("id", 0)
            if nid and nid in seen:
                continue
            seen.add(nid)
            news.append(item)

    news.sort(key=lambda x: x["datetime"], reverse=True)
    return news[:80]

def get_fear_greed() -> dict:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=8", timeout=10)
        data = r.json()["data"]
        current = data[0]
        history = [{"value": int(d["value"]), "label": d["value_classification"],
                    "timestamp": int(d["timestamp"])} for d in data]
        val = int(current["value"])
        prev_val = int(data[1]["value"]) if len(data) > 1 else val
        change = val - prev_val
        zone = ("Extreme Fear" if val <= 25 else "Fear" if val <= 45
                else "Neutral" if val <= 55 else "Greed" if val <= 75 else "Extreme Greed")
        advice = {
            "Extreme Fear": "Historically a buying opportunity — markets are oversold on emotion.",
            "Fear":         "Caution in the market. Good time to look for undervalued assets.",
            "Neutral":      "Balanced market. No extreme bias either way.",
            "Greed":        "Markets leaning bullish but getting stretched. Be selective.",
            "Extreme Greed":"Warning: euphoria mode. Corrections often follow extreme greed.",
        }
        return {
            "value": val, "label": current["value_classification"],
            "zone": zone, "change_1d": change,
            "advice": advice.get(zone, ""),
            "history": history[:7],
            "prev_value": prev_val,
        }
    except:
        return {"value": 50, "label": "Neutral", "zone": "Neutral", "change_1d": 0,
                "advice": "", "history": [], "prev_value": 50}

def get_insider_trades_all() -> list:
    """
    SEC Form 4 insider transactions for key stocks.
    Buys = insiders loading up (bullish signal).
    Sells = insiders cashing out (bearish / neutral signal).
    """
    results = []
    stocks = ["NVDA", "AAPL", "TSLA", "META", "AMZN", "MSFT", "JPM", "XOM"]
    fr = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    to = datetime.now().strftime("%Y-%m-%d")
    CODE_LABELS = {
        "P": "BUY", "S": "SELL", "A": "AWARD", "M": "EXERCISE",
        "G": "GIFT", "F": "TAX", "D": "SELL", "I": "INDIRECT",
    }
    for sym in stocks:
        try:
            d = fh("/stock/insider-transactions", {"symbol": sym, "from": fr, "to": to})
            txns = [t for t in d.get("data", []) if t.get("transactionCode") in ("P", "S")]
            buys  = [t for t in txns if t.get("transactionCode") == "P"]
            sells = [t for t in txns if t.get("transactionCode") == "S"]
            buy_val  = sum(abs(t.get("change", 0)) * (t.get("transactionPrice") or 0) for t in buys)
            sell_val = sum(abs(t.get("change", 0)) * (t.get("transactionPrice") or 0) for t in sells)
            signal = "bullish" if buy_val > sell_val * 2 else \
                     "bearish" if sell_val > buy_val * 2 else "neutral"
            recent = sorted(txns, key=lambda x: x.get("transactionDate",""), reverse=True)[:3]
            results.append({
                "symbol": sym,
                "buy_count":  len(buys),  "sell_count": len(sells),
                "buy_value":  round(buy_val / 1e6, 2),
                "sell_value": round(sell_val / 1e6, 2),
                "signal": signal,
                "recent": [{"name": t.get("name",""), "action": CODE_LABELS.get(t.get("transactionCode",""),"?"),
                             "shares": abs(t.get("change",0)),
                             "price": t.get("transactionPrice", 0),
                             "date": t.get("transactionDate","")} for t in recent],
            })
            time.sleep(0.25)
        except:
            results.append({"symbol": sym, "buy_count": 0, "sell_count": 0,
                            "buy_value": 0, "sell_value": 0, "signal": "neutral", "recent": []})
    return results

def _priced_in_verdict(move_5d: float, move_1m: float, rsi: float | None) -> dict:
    """
    Estimate whether good/bad news is already priced in before earnings.

    Rules:
    - Stock up big recently + RSI overbought  → good news likely priced in, upside limited
    - Stock down big recently + RSI oversold  → bad news priced in, beat could spark big rally
    - Stock up big + RSI normal               → momentum play, could go either way
    - Flat                                    → market undecided, reaction could be large
    """
    abs_move = abs(move_1m)
    if move_1m > 15 and rsi and rsi > 70:
        verdict   = "PRICED IN"
        color     = "warning"
        plain     = (f"Up {move_1m:.0f}% in the last month with RSI at {rsi:.0f} — "
                     f"the market already expects a strong report. "
                     f"Even a beat may not move the stock much. Sell the news risk.")
        expert    = (f"+{move_1m:.1f}% 1M run-up. RSI {rsi:.0f} overbought. "
                     f"Options market likely pricing large move. "
                     f"High bar set — beat+raise needed to sustain momentum.")
    elif move_1m > 8:
        verdict   = "MOSTLY PRICED IN"
        color     = "warning"
        plain     = (f"Up {move_1m:.0f}% over the past month — investors are already "
                     f"optimistic going in. A beat would help but may only give a small pop.")
        expert    = (f"+{move_1m:.1f}% pre-earnings drift. Consensus expectations already elevated. "
                     f"Risk/reward skewed — modest beat likely muted response.")
    elif move_1m < -15 and rsi and rsi < 35:
        verdict   = "FEAR PRICED IN"
        color     = "opportunity"
        plain     = (f"Down {abs(move_1m):.0f}% over the past month with RSI at {rsi:.0f} — "
                     f"the market is bracing for a bad report. If they even slightly beat, "
                     f"expect a sharp rally as shorts cover.")
        expert    = (f"{move_1m:.1f}% 1M drawdown. RSI {rsi:.0f} oversold. "
                     f"Short interest likely elevated. Beat + guidance hold = violent short squeeze risk.")
    elif move_1m < -8:
        verdict   = "SELL-OFF AHEAD OF REPORT"
        color     = "info"
        plain     = (f"Down {abs(move_1m):.0f}% heading into earnings — "
                     f"investors are nervous. Low expectations mean a small beat could "
                     f"be enough to bounce the stock.")
        expert    = (f"{move_1m:.1f}% pre-earnings weakness. Bar lowered. "
                     f"Negative sentiment could be a contrarian setup if fundamentals hold.")
    elif abs_move < 3:
        verdict   = "NOT PRICED IN"
        color     = "info"
        plain     = (f"Barely moved ({move_1m:+.1f}%) in the past month — "
                     f"the market has no strong view. Earnings could move this stock "
                     f"sharply in either direction.")
        expert    = (f"{move_1m:+.1f}% 1M flat. Low pre-earnings drift = high binary risk. "
                     f"Reaction likely driven purely by actual results vs whisper number.")
    else:
        verdict   = "MIXED SIGNALS"
        color     = "neutral"
        plain     = (f"{move_1m:+.1f}% over the past month — modest move, "
                     f"market is watching but not fully committed either way.")
        expert    = (f"{move_1m:+.1f}% 1M move. Inconclusive pre-earnings setup. "
                     f"Watch guidance language more than the headline EPS number.")

    return {"verdict": verdict, "color": color, "plain": plain, "expert": expert,
            "move_5d": round(move_5d, 2), "move_1m": round(move_1m, 2),
            "rsi": round(rsi, 1) if rsi else None}


def get_earnings_with_sentiment() -> list:
    """
    Earnings calendar for next 10 days enriched with:
    - 5-day and 1-month price momentum
    - RSI heading into earnings
    - Plain-English 'priced in' verdict
    """
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        in10  = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
        d = fh("/calendar/earnings", {"from": today, "to": in10})
        watchsyms = (set(WATCHLIST.keys()) |
                     {"AAPL","NVDA","MSFT","META","AMZN","TSLA","JPM","XOM",
                      "GOOGL","NFLX","AMD","INTC","CRM","UBER","PYPL"})
        events = [e for e in d.get("earningsCalendar", []) if e.get("symbol") in watchsyms]
        events.sort(key=lambda x: x.get("date", ""))
    except:
        return []

    results = []
    for e in events[:20]:
        sym = e["symbol"]
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
            r   = requests.get(url, params={"interval": "1d", "range": "3mo"},
                               headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
        except:
            closes = []

        if len(closes) >= 22:
            move_5d = ((closes[-1] - closes[-6])  / closes[-6])  * 100
            move_1m = ((closes[-1] - closes[-22]) / closes[-22]) * 100
            rsi     = compute_rsi(closes)
            priced  = _priced_in_verdict(move_5d, move_1m, rsi)
        else:
            move_5d = move_1m = 0.0
            rsi     = None
            priced  = {"verdict": "INSUFFICIENT DATA", "color": "neutral",
                       "plain": "Not enough price history to assess.", "expert": "",
                       "move_5d": 0, "move_1m": 0, "rsi": None}

        # Get today's quote for current price + day change
        try:
            q     = fh("/quote", {"symbol": sym})
            price = q.get("c", 0)
            chg   = round(((q.get("c",0) - q.get("pc",1)) / q.get("pc",1)) * 100, 2)
        except:
            price = chg = 0

        results.append({
            "symbol":          sym,
            "date":            e.get("date", ""),
            "hour":            e.get("hour", ""),
            "epsEstimate":     e.get("epsEstimate"),
            "revenueEstimate": e.get("revenueEstimate"),
            "epsActual":       e.get("epsActual"),
            "revenueActual":   e.get("revenueActual"),
            "price":           price,
            "change_pct":      chg,
            "priced_in":       priced,
        })
        time.sleep(0.2)

    return results


def get_earnings_calendar() -> list:
    """Thin wrapper kept for cache key compatibility."""
    return get_earnings_with_sentiment()

def get_upgrade_downgrades() -> list:
    """Disabled — requires Finnhub premium tier."""
    return []

def get_social_sentiment_all() -> dict:
    return {}

def get_social_sentiment(symbol: str) -> dict:
    return {}

def get_insider_activity(symbol: str) -> list:
    try:
        fr = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        to = datetime.now().strftime("%Y-%m-%d")
        d  = fh("/stock/insider-transactions", {"symbol": symbol, "from": fr, "to": to})
        txns = d.get("data", [])[:5]
        return [{"name": t.get("name",""), "share": t.get("share",0),
                 "change": t.get("change",0), "transactionDate": t.get("transactionDate",""),
                 "transactionCode": t.get("transactionCode","")} for t in txns]
    except:
        return []

# ── alert history ─────────────────────────────────────────────────────────────

def load_last() -> dict:
    try:
        with open(LAST_PATH) as f: return json.load(f)
    except: return {}

def save_last(snap: dict):
    with open(LAST_PATH, "w") as f:
        json.dump({k: v.get("current", 0) for k, v in snap.items()}, f)

_ALERT_CONTEXT = {
    "VIX": {
        "UP":   "Volatility spike — fear is entering the market. Risk assets may sell off.",
        "DOWN": "Volatility falling — fear is easing. Markets calming down.",
    },
    "BTC": {
        "UP":   "Bitcoin surging — risk appetite is high. Watch crypto alts for follow-through.",
        "DOWN": "Bitcoin selling off — crypto risk-off. May bleed into tech stocks.",
    },
    "GC": {
        "UP":   "Gold rising — safe haven demand. Investors hedging against uncertainty.",
        "DOWN": "Gold falling — risk-on mode. Investors moving out of safe havens.",
    },
    "CL": {
        "UP":   "Oil spiking — inflation risk rises. Energy stocks benefit, consumer hit.",
        "DOWN": "Oil dropping — inflation relief. Good for consumers and growth stocks.",
    },
    "DXY": {
        "UP":   "Dollar strengthening — bad for commodities and emerging markets.",
        "DOWN": "Dollar weakening — supportive of gold, oil, and international stocks.",
    },
}

_ALERT_FIRED_TODAY: dict = {}  # {symbol: {"UP" or "DOWN": "YYYY-MM-DD"}}

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def check_alerts(snap: dict) -> list:
    """
    Fire alerts on TWO conditions:
      1. Big daily move (current vs prev_close) — once per direction per day
      2. Flash 30s move (current vs last poll) — fires every time, throttled
         only by the per-symbol alert_pct threshold scaled 3x
    """
    last  = load_last()
    today = _today_str()
    hits  = []

    for k, d in snap.items():
        cur = d.get("current", 0)
        if not cur:
            continue
        threshold = d.get("alert_pct", 0.5)
        name = d.get("name", k)
        sym_state = _ALERT_FIRED_TODAY.setdefault(k, {})

        # Reset daily flags if it's a new day
        for direction in ("UP", "DOWN"):
            if sym_state.get(direction) and sym_state[direction] != today:
                sym_state.pop(direction, None)

        # ─── 1. DAILY MOVE alert — fire once per direction per session ──
        prev_close = d.get("prev_close", 0)
        if prev_close:
            day_pct = (cur - prev_close) / prev_close * 100
            day_dir = "UP" if day_pct > 0 else "DOWN"
            if abs(day_pct) >= threshold and sym_state.get(day_dir) != today:
                ctx = _ALERT_CONTEXT.get(k, {}).get(
                    day_dir, f"{name} moved {abs(day_pct):.1f}% today.")
                hits.append({
                    "symbol": k, "name": name,
                    "direction": day_dir,
                    "pct": round(abs(day_pct), 2),
                    "current": cur, "prev": prev_close,
                    "threshold": threshold,
                    "context": f"Daily move: {ctx}",
                    "type": d.get("type", ""),
                    "kind": "daily",
                    "timestamp": int(time.time()),
                })
                sym_state[day_dir] = today

        # ─── 2. FLASH MOVE alert — large 30s move (3× the daily threshold) ──
        prv = last.get(k, 0)
        if prv:
            flash_pct = abs((cur - prv) / prv) * 100
            flash_threshold = threshold * 3   # require sharper move intraday
            if flash_pct >= flash_threshold:
                flash_dir = "UP" if cur > prv else "DOWN"
                ctx = _ALERT_CONTEXT.get(k, {}).get(
                    flash_dir, f"{name} flash move {flash_pct:.2f}%.")
                hits.append({
                    "symbol": k, "name": name,
                    "direction": flash_dir,
                    "pct": round(flash_pct, 2),
                    "current": cur, "prev": prv,
                    "threshold": flash_threshold,
                    "context": f"Flash move: {ctx}",
                    "type": d.get("type", ""),
                    "kind": "flash",
                    "timestamp": int(time.time()),
                })

    save_last(snap)
    return hits

def append_alert(alert: dict):
    history = []
    try:
        with open(ALERTS_PATH) as f: history = json.load(f)
    except: pass
    history.insert(0, alert)
    with open(ALERTS_PATH, "w") as f: json.dump(history[:100], f)

def load_alerts() -> list:
    try:
        with open(ALERTS_PATH) as f: return json.load(f)
    except: return []

# ── simple TTL cache ──────────────────────────────────────────────────────────

_cache: dict = {}
_cache_ts: dict = {}
TTL = {"snapshot": 90, "news": 300, "fear_greed": 3600, "rsi": 3600,
       "social": 1800, "technicals": 7200, "analyst_recs": 7200, "analysis": 300,
       "insider_trades": 3600, "earnings_cal": 1800, "upgrades": 3600,
       "correlations": 60}

def cached(key: str, fn, *args):
    if key not in _cache or time.time() - _cache_ts.get(key, 0) > TTL.get(key, 60):
        _cache[key] = fn(*args)
        _cache_ts[key] = time.time()
    return _cache[key]

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Trading Dashboard")
app.add_middleware(GZipMiddleware, minimum_size=512)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class Manager:
    def __init__(self): self.active: List[WebSocket] = []
    async def connect(self, ws):
        await ws.accept(); self.active.append(ws)
    def disconnect(self, ws):
        self.active.remove(ws) if ws in self.active else None
    async def broadcast(self, data):
        dead = []
        for ws in self.active:
            try: await ws.send_json(data)
            except: dead.append(ws)
        for ws in dead: self.disconnect(ws)

mgr = Manager()
_latest: dict = {"snapshot": {}, "alerts": []}

@app.on_event("startup")
async def startup():
    asyncio.create_task(market_loop())
    asyncio.create_task(warm_caches())

async def warm_caches():
    """Pre-populate slow caches so the first visitor doesn't wait."""
    try:
        # Warm snapshot first — it's the most-loaded endpoint
        await asyncio.to_thread(lambda: cached("snapshot",   build_snapshot))
        await asyncio.to_thread(lambda: cached("news",       get_news))
        await asyncio.to_thread(lambda: cached("fear_greed", get_fear_greed))
        await asyncio.to_thread(lambda: cached("rsi",        get_rsi_all))
        await asyncio.to_thread(lambda: cached("technicals", get_technicals_all))
        # Kick off the slow predmarket scan in the background
        asyncio.create_task(_predmarket_refresh())
    except Exception as e:
        print(f"[warm] cache warm error: {e}")

async def market_loop():
    while True:
        snap = await asyncio.to_thread(build_snapshot)
        alerts = check_alerts(snap)
        for a in alerts: append_alert(a)
        _latest["snapshot"] = snap
        _latest["alerts"] = alerts
        # Invalidate snapshot cache so REST endpoints also get fresh data
        _cache_ts.pop("snapshot", None)
        _cache["snapshot"] = snap
        _cache_ts["snapshot"] = time.time()
        await mgr.broadcast({"type": "update", "snapshot": snap,
                              "alerts": alerts, "ts": int(time.time())})
        await asyncio.sleep(30)

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await mgr.connect(ws)
    if _latest["snapshot"]:
        await ws.send_json({"type": "update", "snapshot": _latest["snapshot"],
                            "alerts": _latest["alerts"], "ts": int(time.time())})
    try:
        while True: await asyncio.sleep(60)
    except (WebSocketDisconnect, Exception):
        mgr.disconnect(ws)

@app.get("/api/snapshot")
async def api_snapshot():
    snap = cached("snapshot", build_snapshot)
    alerts = check_alerts(snap)
    for a in alerts: append_alert(a)
    return {"data": snap, "alerts": alerts, "ts": int(time.time())}

@app.get("/api/news")
async def api_news():
    return {"data": cached("news", get_news), "ts": int(time.time())}

@app.get("/api/sentiment")
async def api_sentiment():
    return {"fear_greed": cached("fear_greed", get_fear_greed),
            "social": {},
            "ts": int(time.time())}

@app.get("/api/structural")
async def api_structural():
    return {
        "insider_trades":    await asyncio.to_thread(lambda: cached("insider_trades", get_insider_trades_all)),
        "earnings_calendar": await asyncio.to_thread(lambda: cached("earnings_cal",   get_earnings_calendar)),
        "upgrades":          await asyncio.to_thread(lambda: cached("upgrades",        get_upgrade_downgrades)),
        "ts": int(time.time()),
    }

@app.get("/api/rsi")
async def api_rsi():
    # Cache RSI for 1h (TTL["rsi"] = 3600) — prevents 19 sequential Yahoo
    # calls on every page load. Without this, /api/rsi takes ~7s on every hit.
    return {"data": await asyncio.to_thread(lambda: cached("rsi", get_rsi_all)),
            "ts": int(time.time())}

@app.get("/api/alerts")
async def api_alerts():
    return {"data": load_alerts()[:50]}

@app.get("/api/insider/{symbol}")
async def api_insider(symbol: str):
    return {"data": await asyncio.to_thread(get_insider_activity, symbol.upper())}

# ── Live ticker grounding (Gemini + Google Search) ─────────────────────────
_LIVE_CACHE: dict = {}
_LIVE_TTL = 600  # 10 min — live news refresh window

@app.get("/api/ticker/{symbol}/live")
async def api_ticker_live(symbol: str):
    """
    Live web-grounded snippet for a ticker. Uses Gemini with Google Search
    grounding to return 2-3 sentences of today's market-relevant news.
    Cached 10 min to keep cost down.
    """
    sym = symbol.upper().strip()
    now = time.time()
    cached = _LIVE_CACHE.get(sym)
    if cached and now - cached["ts"] < _LIVE_TTL:
        return {"symbol": sym, "summary": cached["summary"], "ts": cached["ts"], "cached": True}

    try:
        from web.llm_router import ground_ticker
        summary = await asyncio.to_thread(ground_ticker, sym)
    except Exception as e:
        return {"symbol": sym, "summary": None, "error": str(e)}

    if summary:
        _LIVE_CACHE[sym] = {"summary": summary, "ts": now}
    return {"symbol": sym, "summary": summary, "ts": now, "cached": False}

@app.get("/api/technicals")
async def api_technicals():
    return {"data": await asyncio.to_thread(lambda: cached("technicals", get_technicals_all)),
            "ts": int(time.time())}

@app.get("/api/analyst-recs")
async def api_analyst_recs():
    return {"data": await asyncio.to_thread(lambda: cached("analyst_recs", get_analyst_recs)),
            "ts": int(time.time())}

@app.get("/api/correlations")
async def api_correlations():
    return {"data": await asyncio.to_thread(lambda: cached("correlations", get_correlations)),
            "ts": int(time.time())}

@app.get("/api/analysis")
async def api_analysis():
    snap = cached("snapshot", build_snapshot)
    fg   = cached("fear_greed", get_fear_greed)
    news = cached("news", get_news)
    key  = f"analysis_{int(time.time())//300}"
    if key not in _cache:
        _cache[key] = generate_analysis(snap, fg, news)
    return {"data": _cache[key], "ts": int(time.time())}

_PREDMARKET_CACHE: dict = {}
_PREDMARKET_TTL = 300              # serve cached for 5 min before refreshing
_PREDMARKET_SCANNING = {"flag": False}

async def _predmarket_refresh():
    """Run the (slow) predmarket scan in the background and cache the result."""
    if _PREDMARKET_SCANNING["flag"]:
        return
    _PREDMARKET_SCANNING["flag"] = True
    try:
        result = await asyncio.to_thread(run_scan)
        _PREDMARKET_CACHE["data"] = result
        _PREDMARKET_CACHE["ts"]   = time.time()
    except Exception as e:
        print(f"[predmarket] scan error: {e}")
    finally:
        _PREDMARKET_SCANNING["flag"] = False

@app.get("/api/predmarkets")
async def api_predmarkets():
    """
    Serve cached predmarket result instantly. If the cache is missing or
    stale, kick off a refresh in the background — don't block the request.
    Railway has ~60s request timeout; the scan can take 30-60s.
    """
    now = time.time()
    cached_age = now - _PREDMARKET_CACHE.get("ts", 0) if _PREDMARKET_CACHE else None

    # No cache yet — kick off scan and return a stub so the UI shows status
    if not _PREDMARKET_CACHE:
        asyncio.create_task(_predmarket_refresh())
        return {
            "stats": {
                "total_kalshi": 0, "total_polymarket": 0,
                "matches_found": 0, "large_edges": 0, "arb_count": 0,
                "by_category": {}, "scan_time": None, "scanning": True,
            },
            "pairs": [],
            "top_by_category": {},
            "scanning": True,
            "message": "First scan in progress — refresh in 30 seconds.",
        }

    # Stale cache — refresh in background but serve what we have
    if cached_age is not None and cached_age > _PREDMARKET_TTL:
        asyncio.create_task(_predmarket_refresh())

    return _PREDMARKET_CACHE["data"]

# ── Podcast intelligence ────────────────────────────────────────────────────
_PODCAST_POLLING = {"flag": False, "last_run": 0.0}
_PODCAST_POLL_INTERVAL = 15 * 60  # 15 minutes between background polls


async def _podcast_refresh_bg():
    """Poll + process new podcast episodes in the background. Never blocks."""
    if _PODCAST_POLLING["flag"]:
        return
    _PODCAST_POLLING["flag"] = True
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(_POOL, podcast_poll, 3)
        print(f"[podcasts] refresh result: {result}")
        _PODCAST_POLLING["last_run"] = time.time()
    except Exception as e:
        print(f"[podcasts] refresh error: {e}")
    finally:
        _PODCAST_POLLING["flag"] = False


@app.get("/api/podcasts")
async def api_podcasts(limit: int = 20):
    """
    List recent processed podcast episodes with intel.
    Triggers a background poll if last poll was >15 min ago.
    """
    now = time.time()
    if now - _PODCAST_POLLING["last_run"] > _PODCAST_POLL_INTERVAL:
        asyncio.create_task(_podcast_refresh_bg())

    episodes = podcast_list_episodes(limit=limit)
    return {
        "shows": [
            {"id": k, "name": v["name"], "kind": v.get("kind", "news")}
            for k, v in PODCAST_SUBSCRIBED.items()
        ],
        "episodes": episodes,
        "polling": _PODCAST_POLLING["flag"],
        "last_poll": _PODCAST_POLLING["last_run"],
    }


@app.post("/api/podcasts/refresh")
async def api_podcasts_refresh():
    """Force a podcast refresh (manual trigger from the UI)."""
    asyncio.create_task(_podcast_refresh_bg())
    return {"started": True}


# ── Mainframe — live telemetry for every LLM call ──────────────────────────
@app.get("/api/mainframe")
async def api_mainframe():
    """
    Live view of the AI brain. Returns:
      - stats: lifetime counters per provider
      - recent: last ~60 LLM calls with model, label, tokens, elapsed, preview
      - models: which models are configured
      - keys:   which API keys are present (booleans only — no secrets)
    """
    from web.llm_router import telemetry, health
    tel = telemetry()
    h   = health()
    return {
        "stats":   tel["stats"],
        "recent":  tel["recent"],
        "uptime_s": tel["uptime_s"],
        "models":  {p: v["model"] for p, v in h.items()},
        "keys":    {p: v["key_set"] for p, v in h.items()},
        "openai_key_set": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "ts": time.time(),
    }


# ── War Room — aggregate status for all operatives ─────────────────────────
@app.get("/api/warroom")
async def api_warroom():
    """
    Single endpoint for the War Room dashboard.
    Aggregates real status from every operative without blocking.
    Returns only what is genuinely known — no fake data.
    """
    from web.llm_router import telemetry, health

    tel = telemetry()
    h   = health()

    # Key presence (booleans only)
    keys = {
        "claude":  h.get("claude",  {}).get("key_set", False),
        "gemini":  h.get("gemini",  {}).get("key_set", False),
        "finnhub": bool(FKEY),
        "discord": bool(os.environ.get("DISCORD_TOKEN", "").strip()),
    }

    # LLM stats
    stats = tel["stats"]
    calls_total  = stats.get("gemini_calls",  0) + stats.get("claude_calls",  0)
    tokens_total = stats.get("gemini_tokens", 0) + stats.get("claude_tokens", 0)
    errors_total = stats.get("gemini_errors", 0) + stats.get("claude_errors", 0)

    # Recent LLM calls → jarvis/intel feed
    recent = tel["recent"][:40]

    # Quick market snapshot (from shared cache — no extra API calls)
    snap_cached = _cache.get("snapshot", {})
    market_summary: dict = {}
    if snap_cached:
        prices = snap_cached.get("data", {})
        for sym in ["ES", "BTC", "VIX", "NVDA", "DXY"]:
            if sym in prices:
                p = prices[sym]
                market_summary[sym] = {
                    "price":   p.get("current"),
                    "chg_pct": p.get("change_pct"),
                }

    # Analysis mood (from shared cache) — response shape: {data: {mood, ...}}
    analysis_raw    = _cache.get("analysis", {})
    analysis_cached = analysis_raw.get("data", analysis_raw) if analysis_raw else {}
    mood        = analysis_cached.get("mood", "")
    mood_signal = analysis_cached.get("mood_desc", "")

    # Scripts status
    scripts_cached = bool(_SCRIPTS_CACHE.get("payload"))

    # Per-operative status
    operatives = {
        "intel": {
            "online": bool(FKEY),
            "status": "ONLINE" if FKEY else "NO KEY",
            "market_summary": market_summary,
            "mood": mood,
            "mood_signal": mood_signal,
            "last_snap_age_s": int(time.time() - _cache_ts.get("snapshot", 0)) if _cache_ts.get("snapshot") else None,
        },
        "jarvis": {
            "online": keys["claude"] or keys["gemini"],
            "status": "ONLINE" if (keys["claude"] or keys["gemini"]) else "NO KEY",
            "recent_calls": recent,
            "calls_today": calls_total,
            "tokens_today": tokens_total,
            "errors_today": errors_total,
        },
        "content": {
            "online": keys["claude"] or keys["gemini"],
            "status": "SCRIPTS READY" if scripts_cached else ("IDLE" if (keys["claude"] or keys["gemini"]) else "NO KEY"),
            "scripts_generated": scripts_cached,
        },
        "discord": {
            "online": keys["discord"],
            "status": "CONFIGURED" if keys["discord"] else "NOT CONFIGURED",
        },
    }

    return {
        "ts": time.time(),
        "uptime_s": tel["uptime_s"],
        "keys": keys,
        "stats": {
            "calls_total": calls_total,
            "tokens_total": tokens_total,
            "errors_total": errors_total,
        },
        "operatives": operatives,
        "recent_llm": recent,
    }


# ── Daily scripts (for IG/Twitter/TikTok content) ──────────────────────────
_SCRIPTS_CACHE: dict = {}
_SCRIPTS_TTL = 6 * 3600   # 6 hours

@app.get("/api/scripts/today")
async def api_scripts_today():
    """
    Returns 3 ready-to-shoot 30-second video scripts pulled from today's
    top intel. Format: hook → fact → tickers → CTA. Refreshed every 6 hours.
    """
    now = time.time()
    cached = _SCRIPTS_CACHE.get("payload")
    if cached and (now - _SCRIPTS_CACHE.get("ts", 0)) < _SCRIPTS_TTL:
        return cached

    from web.llm_router import gemini_json
    prompt = """You write 30-second vertical-video scripts for Conviction Capital,
a market intelligence platform. The goal: a viewer on Instagram Reels / TikTok
hooks within 1.5 seconds, learns one specific market-relevant fact, sees
named tickers, and is told to visit convictioncapital.com for the full play.

Generate THREE scripts based on the most important market intel for the
last 48 hours. Each script must be specific, not generic. Each must contain
real named tickers, a real dollar amount or percentage, and a real catalyst.

Output ONLY this JSON:
{
  "scripts": [
    {
      "topic":   "one-line topic",
      "hook":    "1.5-second opener — bold claim with a number/name",
      "body":    "20-second main script — fact, structural why, named tickers, the play",
      "cta":     "5-second close — 'link in bio for the full breakdown'",
      "tickers": ["TICKER1", "TICKER2"],
      "caption": "Instagram caption — punchy first line, 2-3 short lines, 5 hashtags",
      "hashtags": ["#hash1", "#hash2", "#hash3", "#hash4", "#hash5"]
    },
    ...
  ]
}

Rules:
- Every hook must have a specific number, ticker, name, or date.
- No platitudes. No "the market is moving today."
- Real catalysts only. If you don't know one, say so in the topic but DO NOT fabricate.
- Voice: confident, plain-English, no jargon, no hype-bro tone."""

    payload = await asyncio.to_thread(
        gemini_json, prompt, None, 3000, "scripts:daily"
    )
    if not payload:
        return JSONResponse({"error": "script generation failed"}, status_code=503)

    payload["ts"] = now
    _SCRIPTS_CACHE["payload"] = payload
    _SCRIPTS_CACHE["ts"] = now
    return payload


# ── Intel-Bot — public-facing AI Person Q&A (conversion funnel) ────────────
# Free tier: 1 question per IP per day. Answers come from Gemini grounded
# in the live web. Anything they ask becomes a lead.
_INTELBOT_LOG: dict = {}   # ip → { last_ts, count_today, day }
_INTELBOT_DAILY_LIMIT = 3

@app.post("/api/intelbot/ask")
async def api_intelbot_ask(payload: dict):
    """
    Public-facing AI Person endpoint. POST { question: str }.
    Rate-limited per IP. Uses Gemini with web grounding so answers are current.
    """
    from web.llm_router import gemini_call
    from fastapi import Request
    q = (payload.get("question") or "").strip()
    if not q or len(q) < 5:
        return JSONResponse({"error": "ask a real question"}, status_code=400)
    if len(q) > 400:
        return JSONResponse({"error": "keep questions under 400 chars"}, status_code=400)

    ip = payload.get("_ip") or "anon"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rec = _INTELBOT_LOG.get(ip) or {"day": today, "count": 0}
    if rec["day"] != today:
        rec = {"day": today, "count": 0}
    if rec["count"] >= _INTELBOT_DAILY_LIMIT:
        return {
            "answer": ("You've hit today's free limit (3 questions/day). "
                       "Sign up at convictioncapital.com for unlimited intel."),
            "rate_limited": True,
        }

    prompt = f"""You are the public-facing AI analyst for Conviction Capital, a market intel platform.
A visitor asked: "{q}"

Answer in 4-6 sentences. Be specific — name tickers, dates, dollar amounts.
Be honest if you don't know. End with: "For the full breakdown including conviction scores and the layered take, visit convictioncapital.com."

If the question is off-topic (not markets/economics/policy/companies), politely steer them back: "I focus on market intelligence — try asking about a stock, sector, or macro event."
"""
    answer = await asyncio.to_thread(
        gemini_call, prompt, None, 800, True, "intelbot:ask"
    )
    if not answer:
        return JSONResponse({"error": "intel-bot is busy, try again"}, status_code=503)

    rec["count"] += 1
    _INTELBOT_LOG[ip] = rec
    return {"answer": answer, "remaining": _INTELBOT_DAILY_LIMIT - rec["count"]}


_NEWS_CACHE: dict = {}
_NEWS_RAW_TTL   = 600   # 10 min — RSS re-fetch
_NEWS_SCRIPT_TTL = 3600 # 1 hour — Claude script stays fixed

@app.get("/api/newsbrief/raw")
async def api_newsbrief_raw():
    """Raw clustered stories from all feeds — no script generation."""
    now = time.time()
    if _NEWS_CACHE.get("raw") and (now - _NEWS_CACHE.get("raw_ts", 0)) < _NEWS_RAW_TTL:
        return _NEWS_CACHE["raw"]
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: news_aggregate(translate=False)
        )
        _NEWS_CACHE["raw"]    = result
        _NEWS_CACHE["raw_ts"] = now
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/newsbrief/script")
async def api_newsbrief_script():
    """Full Jarvis-voiced script + short-form cut. Cached 1 hour."""
    now = time.time()
    if _NEWS_CACHE.get("script") and (now - _NEWS_CACHE.get("script_ts", 0)) < _NEWS_SCRIPT_TTL:
        return _NEWS_CACHE["script"]
    try:
        raw = _NEWS_CACHE.get("raw")
        if not raw:
            raw = await asyncio.get_event_loop().run_in_executor(
                None, lambda: news_aggregate(translate=False)
            )
            _NEWS_CACHE["raw"]    = raw
            _NEWS_CACHE["raw_ts"] = now
        stories = raw.get("stories", [])
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: generate_script(stories)
        )
        short = await asyncio.get_event_loop().run_in_executor(
            None, lambda: generate_short(result["script"])
        )
        combined = {**result, **short}
        _NEWS_CACHE["script"]    = combined
        _NEWS_CACHE["script_ts"] = now
        return combined
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ── Sports lines + props ────────────────────────────────────────────────────
@app.get("/api/sports/leagues")
async def api_sports_leagues():
    return {"leagues": sports_leagues()}


@app.get("/api/sports/{league}")
async def api_sports_league(league: str):
    try:
        return await asyncio.to_thread(sports_board, league.lower())
    except Exception as e:
        return JSONResponse({"error": str(e), "games": [], "futures": []},
                            status_code=500)


@app.get("/api/sports/{league}/props/{event_id}")
async def api_sports_props(league: str, event_id: str):
    try:
        return await asyncio.to_thread(sports_event_props, league.lower(), event_id)
    except Exception as e:
        return JSONResponse({"error": str(e), "props": []}, status_code=500)


_static_dir = Path(__file__).parent / "static"

# Tell browsers (and iOS Safari especially) NOT to cache the HTML so users
# always get the latest version. CSS/JS use ?v=N query strings for busting.
class _NoCacheHTMLStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        try:
            target = path or "index.html"
            if target.endswith(".html") or target in ("", "/"):
                resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                resp.headers["Pragma"] = "no-cache"
                resp.headers["Expires"] = "0"
            # Cache versioned static assets (CSS/JS/images/fonts) aggressively —
            # we cache-bust via ?v=N query string in the HTML, so it's safe to
            # let the browser hold onto them for a year.
            elif target.endswith((".css", ".js", ".png", ".jpg", ".jpeg",
                                   ".webp", ".svg", ".woff", ".woff2", ".ico")):
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        except Exception:
            pass
        return resp

app.mount("/", _NoCacheHTMLStatic(directory=str(_static_dir), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
