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

PT = ZoneInfo("America/Los_Angeles")
ALERTS_PATH = os.path.expanduser("~/trading-assistant/.alerts_history.json")
LAST_PATH   = os.path.expanduser("~/trading-assistant/.last_prices.json")

WATCHLIST = {
    "CL":  {"name": "Crude Oil", "finnhub": "USO",             "alert_pct": 0.8,  "type": "commodity"},
    "ES":  {"name": "S&P 500",   "finnhub": "SPY",             "alert_pct": 0.4,  "type": "index"},
    "NQ":  {"name": "Nasdaq",    "finnhub": "QQQ",             "alert_pct": 0.5,  "type": "index"},
    "GC":  {"name": "Gold",      "finnhub": "GLD",             "alert_pct": 0.5,  "type": "commodity"},
    "BTC": {"name": "Bitcoin",   "finnhub": "BINANCE:BTCUSDT", "alert_pct": 2.0,  "type": "crypto"},
    "DXY": {"name": "USD Index", "finnhub": "UUP",             "alert_pct": 0.3,  "type": "forex"},
}

# Read API key from env var (Railway) or fall back to config.json (local dev)
_fkey_env = os.environ.get("FINNHUB_API_KEY")
if _fkey_env:
    FKEY = _fkey_env
else:
    _cfg_path = os.path.expanduser("~/trading-assistant/config.json")
    with open(_cfg_path) as f:
        FKEY = json.load(f)["finnhub_api_key"]

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

def get_btc() -> dict:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"}, timeout=12)
        d = r.json().get("bitcoin", {})
        price = d.get("usd", 0)
        chg   = d.get("usd_24h_change", 0)
        prev  = round(price / (1 + chg / 100), 2) if chg else price
        return {"current": price, "prev_close": prev, "high": 0, "low": 0, "change_pct": round(chg, 2)}
    except:
        return {"current": 0, "prev_close": 0, "high": 0, "low": 0, "change_pct": 0}

def build_snapshot() -> dict:
    snap = {}
    for k, info in WATCHLIST.items():
        q = get_btc() if k == "BTC" else get_quote(info["finnhub"])
        snap[k] = {**info, **q}
        time.sleep(0.2)
    return snap

def get_candles(symbol: str, count: int = 35) -> list:
    try:
        to = int(time.time())
        fr = to - count * 24 * 3600
        d  = fh("/stock/candle", {"symbol": symbol, "resolution": "D", "from": fr, "to": to})
        return d.get("c", []) if d.get("s") == "ok" else []
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

def get_rsi_all() -> dict:
    result = {}
    for sym, info in WATCHLIST.items():
        if sym == "BTC":
            result[sym] = {"rsi": None, "signal": "n/a", "name": info["name"]}
            continue
        closes = get_candles(info["finnhub"])
        rsi    = compute_rsi(closes)
        signal = "overbought" if rsi and rsi > 70 else "oversold" if rsi and rsi < 30 else "neutral"
        result[sym] = {"rsi": rsi, "signal": signal, "name": info["name"]}
        time.sleep(0.3)
    return result

def get_news() -> list:
    news = []
    for cat in ("general", "crypto", "forex", "merger"):
        try:
            items = fh("/news", {"category": cat, "minId": 0})
            for n in items[:15]:
                news.append({
                    "id":       n.get("id", 0),
                    "headline": n.get("headline", ""),
                    "summary":  (n.get("summary") or "")[:220],
                    "source":   n.get("source", ""),
                    "url":      n.get("url", ""),
                    "datetime": n.get("datetime", 0),
                    "category": cat,
                    "image":    n.get("image", ""),
                    "related":  n.get("related", ""),
                })
        except:
            pass
    news.sort(key=lambda x: x["datetime"], reverse=True)
    return news[:50]

def get_fear_greed() -> dict:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except:
        return {"value": 50, "label": "Neutral"}

def get_social_sentiment(symbol: str) -> dict:
    try:
        fr = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        d  = fh("/stock/social-sentiment", {"symbol": symbol, "from": fr})
        reddit  = d.get("reddit",  [])[-5:]
        twitter = d.get("twitter", [])[-5:]
        return {
            "reddit_score":    round(sum(x.get("score", 0) for x in reddit) / max(len(reddit), 1), 3),
            "twitter_score":   round(sum(x.get("score", 0) for x in twitter) / max(len(twitter), 1), 3),
            "reddit_mentions": sum(x.get("mention", 0) for x in reddit),
            "twitter_mentions": sum(x.get("mention", 0) for x in twitter),
        }
    except:
        return {"reddit_score": 0, "twitter_score": 0, "reddit_mentions": 0, "twitter_mentions": 0}

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

def check_alerts(snap: dict) -> list:
    last = load_last()
    hits = []
    for k, d in snap.items():
        cur, prv = d.get("current", 0), last.get(k, 0)
        if not cur or not prv: continue
        pct = abs((cur - prv) / prv) * 100
        if pct >= d.get("alert_pct", 0.5):
            hits.append({"symbol": k, "name": d.get("name", k),
                         "direction": "UP" if cur > prv else "DOWN",
                         "pct": round(pct, 2), "current": cur,
                         "threshold": d.get("alert_pct"), "timestamp": int(time.time())})
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
TTL = {"snapshot": 30, "news": 300, "fear_greed": 3600, "rsi": 3600, "social": 1800}

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
            "social": cached("social_SPY", get_social_sentiment, "SPY"),
            "ts": int(time.time())}

@app.get("/api/rsi")
async def api_rsi():
    return {"data": await asyncio.to_thread(get_rsi_all), "ts": int(time.time())}

@app.get("/api/alerts")
async def api_alerts():
    return {"data": load_alerts()[:50]}

@app.get("/api/insider/{symbol}")
async def api_insider(symbol: str):
    return {"data": await asyncio.to_thread(get_insider_activity, symbol.upper())}

_static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
