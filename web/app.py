import os, json, time, asyncio, math
from pathlib import Path
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List
from web.predmarket import run_scan

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

# Read API key from env var (Railway) or fall back to config.json (local dev)
_fkey_env = os.environ.get("FINNHUB_API_KEY")
if _fkey_env:
    FKEY = _fkey_env
else:
    try:
        _cfg_path = _BASE_DIR / "config.json"
        with open(_cfg_path) as f:
            FKEY = json.load(f)["finnhub_api_key"]
    except Exception:
        FKEY = ""

# ── helpers ──────────────────────────────────────────────────────────────────

def fh(path: str, params: dict) -> dict:
    params["token"] = FKEY
    r = requests.get(f"https://finnhub.io/api/v1{path}", params=params, timeout=12)
    return r.json()

def get_quote(symbol: str) -> dict:
    try:
        d = fh("/quote", {"symbol": symbol})
        c, pc = d.get("c") or 0, d.get("pc") or 1
        return {"current": round(c, 4), "prev_close": round(pc, 4),
                "high": round(d.get("h") or 0, 4), "low": round(d.get("l") or 0, 4),
                "change_pct": round(((c - pc) / pc) * 100, 2) if pc else 0}
    except:
        return {"current": 0, "prev_close": 0, "high": 0, "low": 0, "change_pct": 0}

def build_snapshot() -> dict:
    snap = {}
    for k, info in WATCHLIST.items():
        q = get_quote(info["finnhub"])
        snap[k] = {**info, **q}
        time.sleep(0.2)
    return snap

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

def get_analyst_recs() -> dict:
    result = {}
    stocks = [s for s, i in WATCHLIST.items() if i["type"] == "stock"]
    for sym in stocks:
        try:
            d = fh("/stock/recommendation", {"symbol": sym})
            if d:
                latest = d[0]
                total  = sum([latest.get("strongBuy",0), latest.get("buy",0),
                               latest.get("hold",0), latest.get("sell",0), latest.get("strongSell",0)])
                buys   = latest.get("strongBuy",0) + latest.get("buy",0)
                sells  = latest.get("sell",0) + latest.get("strongSell",0)
                consensus = "buy" if buys > sells and buys > total*0.4 else \
                            "sell" if sells > buys and sells > total*0.4 else "hold"
                result[sym] = {
                    "strongBuy": latest.get("strongBuy",0), "buy": latest.get("buy",0),
                    "hold": latest.get("hold",0), "sell": latest.get("sell",0),
                    "strongSell": latest.get("strongSell",0),
                    "total": total, "consensus": consensus,
                    "period": latest.get("period",""),
                }
            time.sleep(0.3)
        except:
            pass
    return result

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
        mood, mood_color = "RISK ON 🚀", "green"
        mood_desc = "Markets rallying with low fear. Investors are confident and buying risk assets."
    elif es_chg < -0.8 and vix_val > 22:
        mood, mood_color = "RISK OFF 🛡️", "red"
        mood_desc = "Stocks falling while fear rises. Investors are protecting capital and avoiding risk."
    elif vix_val > 30:
        mood, mood_color = "HIGH FEAR ⚠️", "red"
        mood_desc = f"VIX at {vix_val:.0f} — extreme volatility. The market is scared. Expect large swings."
    elif abs(es_chg) < 0.25:
        mood, mood_color = "FLAT ↔️", "yellow"
        mood_desc = "Markets have no clear direction today. Traders waiting for a catalyst."
    elif es_chg > 0:
        mood, mood_color = "BULLISH 📈", "green"
        mood_desc = "Stocks are climbing. More buyers than sellers in the market right now."
    else:
        mood, mood_color = "BEARISH 📉", "red"
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
        signals.append({"icon": "🚨", "title": "Market in Panic Mode", "type": "danger",
            "plain": f"VIX at {vix_val:.0f} is panic territory. Markets can swing wildly. Don't make rushed decisions.",
            "expert": f"VIX {vix_val:.1f} → implied daily SPX move ~{vix_val/16:.1f}%. Vol term structure likely inverted. Short-vol strategies at max risk."})
    elif vix_val > 20:
        signals.append({"icon": "⚠️", "title": "Volatility is Elevated", "type": "warning",
            "plain": f"The fear index is at {vix_val:.0f}. Markets are jittery. Be careful with large bets.",
            "expert": f"VIX {vix_val:.1f} above 20 threshold. Dealer hedging pressure elevated. Expect gap risk and wider spreads."})
    elif vix_val < 13:
        signals.append({"icon": "😴", "title": "Markets Are Too Calm", "type": "info",
            "plain": f"VIX at {vix_val:.0f} — markets feel very safe. But extreme calm often comes before a storm.",
            "expert": f"VIX {vix_val:.1f} near complacency levels. Low vol regime risk. Tail hedges are cheap — consider adding protection."})

    # Fear & Greed
    if fg_val <= 25:
        signals.append({"icon": "🔴", "title": "Extreme Fear = Buying Opportunity?", "type": "opportunity",
            "plain": f"Fear & Greed index at {fg_val}/100. Historically, when everyone is scared, prices are low and smart money buys.",
            "expert": f"CNN F&G {fg_val} (Extreme Fear). Contrarian long signal. Check if fundamental catalyst justifies fear or if this is sentiment overshoot."})
    elif fg_val >= 75:
        signals.append({"icon": "🟡", "title": "Extreme Greed — Be Cautious", "type": "warning",
            "plain": f"Fear & Greed at {fg_val}/100. Everyone is greedy — this is when bubbles form. Markets may be stretched.",
            "expert": f"CNN F&G {fg_val} (Extreme Greed). Sentiment overbought. Mean reversion risk elevated. Trim overweight positions."})

    # DXY vs Gold
    if dxy_chg > 0.4 and gc_chg < -0.3:
        signals.append({"icon": "💵", "title": "Strong Dollar, Weak Gold", "type": "info",
            "plain": "The US Dollar is gaining strength while Gold falls. Investors trust the US economy and want dollars over safe havens.",
            "expert": f"DXY proxy +{dxy_chg:.2f}% / GLD {gc_chg:.2f}%. Real rate pickup likely. Dollar strength negative for commodities and EM assets."})
    elif dxy_chg < -0.4 and gc_chg > 0.3:
        signals.append({"icon": "🥇", "title": "Dollar Weakening, Gold Rising", "type": "warning",
            "plain": "The dollar is falling and gold is rising — a classic signal that investors are worried about inflation or economic instability.",
            "expert": f"DXY proxy {dxy_chg:.2f}% / GLD +{gc_chg:.2f}%. Real rates pressured lower. Dollar weakness supportive of commodities and risk assets."})

    # Oil
    if cl_chg > 2.5:
        signals.append({"icon": "🛢️", "title": "Oil Spiking — Watch Inflation", "type": "danger",
            "plain": f"Crude oil is up {cl_chg:.1f}% today. Higher oil means higher gas prices and more inflation — bad for most consumers, great for energy stocks like XOM.",
            "expert": f"USO +{cl_chg:.2f}%. Energy sector should outperform. Negative for growth/tech via rate expectations. Monitor TIPS breakevens."})
    elif cl_chg < -2.5:
        signals.append({"icon": "🛢️", "title": "Oil Dropping — Inflation Relief", "type": "opportunity",
            "plain": f"Crude oil fell {abs(cl_chg):.1f}%. Cheaper oil means cheaper gas and less inflation — a positive for the overall economy and consumer stocks.",
            "expert": f"USO {cl_chg:.2f}%. Disinflationary input cost signal. Positive for margin expansion in industrials and consumer discretionary."})

    # BTC risk barometer
    if btc_chg > 4 and es_chg > 0:
        signals.append({"icon": "🚀", "title": "Crypto + Stocks Both Surging", "type": "bullish",
            "plain": f"Bitcoin up {btc_chg:.1f}% alongside stocks — maximum 'risk-on' signal. Investors are in full buying mode across all markets.",
            "expert": "Cross-asset risk appetite elevated. BTC/equity correlation positive. Rotate into high-beta: growth tech, small caps, crypto alts."})
    elif btc_chg < -4 and es_chg < 0:
        signals.append({"icon": "📉", "title": "Crypto + Stocks Both Falling", "type": "danger",
            "plain": f"Bitcoin down {abs(btc_chg):.1f}% with stocks also falling — everything selling off at once. Classic de-risking.",
            "expert": "Macro deleveraging event. BTC leading risk-off. Watch high-yield credit spreads and USD/JPY for confirmation of severity."})

    # NVDA / AI signal
    if nvda.get("change_pct", 0) > 3:
        signals.append({"icon": "🤖", "title": "AI Stocks Leading the Market", "type": "bullish",
            "plain": f"NVDA up {nvda.get('change_pct',0):.1f}%. AI/tech names are the strongest performers today — the market believes in the AI trade.",
            "expert": "Mega-cap AI outperforming. Semis acting as risk-on leading indicator. Watch SOX index for sustainability."})
    elif nvda.get("change_pct", 0) < -3:
        signals.append({"icon": "💻", "title": "Tech/AI Stocks Under Pressure", "type": "warning",
            "plain": f"NVDA down {abs(nvda.get('change_pct',0)):.1f}%. Tech is lagging — could signal rotation away from growth into value.",
            "expert": "Semis underperforming. Growth/duration risk-off. Check 10yr yield — if rising, that explains the tech pressure."})

    # Banks
    if abs(jpm.get("change_pct", 0)) > 1.5:
        up = jpm.get("change_pct", 0) > 0
        signals.append({"icon": "🏦", "title": f"Banking Sector {'Rallying' if up else 'Selling Off'}", "type": "bullish" if up else "warning",
            "plain": f"JPMorgan {'up' if up else 'down'} {abs(jpm.get('change_pct',0)):.1f}%. Banks are a barometer of economic health. {'Positive signal' if up else 'Watch for broader credit stress'}.",
            "expert": f"JPM {jpm.get('change_pct',0):.2f}%. {'Yield curve steepening / credit expansion signal.' if up else 'Potential NIM compression or credit risk concerns. Watch CDS spreads.'}"})

    # Geopolitical news
    geo = [n for n in news if n.get("category") == "geopolitical"][:1]
    if geo:
        signals.append({"icon": "🌍", "title": "Geopolitical Event in the News", "type": "warning",
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

def get_news() -> list:
    news = []
    seen = set()

    # Finnhub market categories
    for cat in ("general", "crypto", "forex", "merger"):
        try:
            items = fh("/news", {"category": cat, "minId": 0})
            for n in items[:20]:
                nid = n.get("id", 0)
                if nid in seen:
                    continue
                seen.add(nid)
                headline = n.get("headline", "")
                summary  = (n.get("summary") or "")[:220]
                tagged   = _tag_category(headline, summary, cat)
                news.append({
                    "id":       nid,
                    "headline": headline,
                    "summary":  summary,
                    "source":   n.get("source", ""),
                    "url":      n.get("url", ""),
                    "datetime": n.get("datetime", 0),
                    "category": tagged,
                    "image":    n.get("image", ""),
                    "related":  n.get("related", ""),
                })
        except:
            pass

    # Company news for major S&P movers (earnings, guidance leaks, etc.)
    for sym in ("AAPL", "NVDA", "MSFT", "META", "AMZN", "TSLA", "JPM", "XOM"):
        try:
            fr = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
            to = datetime.now().strftime("%Y-%m-%d")
            items = fh(f"/company-news", {"symbol": sym, "from": fr, "to": to})
            for n in items[:4]:
                nid = n.get("id", 0)
                if nid in seen:
                    continue
                seen.add(nid)
                headline = n.get("headline", "")
                summary  = (n.get("summary") or "")[:220]
                tagged   = _tag_category(headline, summary, "earnings")
                news.append({
                    "id":       nid,
                    "headline": headline,
                    "summary":  summary,
                    "source":   n.get("source", sym),
                    "url":      n.get("url", ""),
                    "datetime": n.get("datetime", 0),
                    "category": tagged,
                    "image":    n.get("image", ""),
                    "related":  sym,
                })
            time.sleep(0.15)
        except:
            pass

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

def check_alerts(snap: dict) -> list:
    last = load_last()
    hits = []
    for k, d in snap.items():
        cur, prv = d.get("current", 0), last.get(k, 0)
        if not cur or not prv: continue
        pct = abs((cur - prv) / prv) * 100
        if pct >= d.get("alert_pct", 0.5):
            direction = "UP" if cur > prv else "DOWN"
            ctx = _ALERT_CONTEXT.get(k, {}).get(direction, f"{d.get('name',k)} made a significant move.")
            hits.append({
                "symbol": k, "name": d.get("name", k),
                "direction": direction,
                "pct": round(pct, 2), "current": cur,
                "prev": prv,
                "threshold": d.get("alert_pct"),
                "context": ctx,
                "type": d.get("type", ""),
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
TTL = {"snapshot": 30, "news": 300, "fear_greed": 3600, "rsi": 3600,
       "social": 1800, "technicals": 7200, "analyst_recs": 7200, "analysis": 300,
       "insider_trades": 3600, "earnings_cal": 1800, "upgrades": 3600}

def cached(key: str, fn, *args):
    if key not in _cache or time.time() - _cache_ts.get(key, 0) > TTL.get(key, 60):
        _cache[key] = fn(*args)
        _cache_ts[key] = time.time()
    return _cache[key]

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="Trading Dashboard")
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
    await asyncio.sleep(2)  # let the snapshot loop go first
    try:
        await asyncio.to_thread(lambda: cached("rsi",        get_rsi_all))
        await asyncio.to_thread(lambda: cached("technicals", get_technicals_all))
        await asyncio.to_thread(lambda: cached("news",       get_news))
        await asyncio.to_thread(lambda: cached("fear_greed", get_fear_greed))
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

@app.get("/api/technicals")
async def api_technicals():
    return {"data": await asyncio.to_thread(lambda: cached("technicals", get_technicals_all)),
            "ts": int(time.time())}

@app.get("/api/analyst-recs")
async def api_analyst_recs():
    return {"data": await asyncio.to_thread(lambda: cached("analyst_recs", get_analyst_recs)),
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

_static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
