import os, sys, json, time, requests, logging
from datetime import datetime
from zoneinfo import ZoneInfo
import anthropic
from twilio.rest import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[logging.FileHandler(os.path.expanduser("~/trading-assistant/assistant.log")), logging.StreamHandler()])
log = logging.getLogger(__name__)

WATCHLIST = {
    "CL":  {"name": "Crude Oil", "finnhub": "USO",             "alert_pct": 0.8},
    "ES":  {"name": "SP500",     "finnhub": "SPY",             "alert_pct": 0.4},
    "NQ":  {"name": "Nasdaq",    "finnhub": "QQQ",             "alert_pct": 0.5},
    "GC":  {"name": "Gold",      "finnhub": "GLD",             "alert_pct": 0.5},
    "BTC": {"name": "Bitcoin",   "finnhub": "BINANCE:BTCUSDT", "alert_pct": 2.0},
    "DXY": {"name": "Dollar",    "finnhub": "UUP",             "alert_pct": 0.3},
}

PT = ZoneInfo("America/Los_Angeles")
CFG = os.path.expanduser("~/trading-assistant/config.json")
LAST = os.path.expanduser("~/trading-assistant/.last_prices.json")

def load_config():
    if not os.path.exists(CFG):
        print("Run setup first: python3 trading_assistant.py setup")
        sys.exit(1)
    with open(CFG) as f:
        return json.load(f)

def get_quote(symbol, key):
    try:
        r = requests.get("https://finnhub.io/api/v1/quote", params={"symbol": symbol, "token": key}, timeout=10)
        d = r.json()
        c = d.get("c", 0) or 0
        pc = d.get("pc", 1) or 1
        return {"current": c, "change_pct": round(((c - pc) / pc) * 100, 2)}
    except:
        return {"current": 0, "change_pct": 0}

def get_btc():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"}, timeout=10)
        d = r.json().get("bitcoin", {})
        return {"current": d.get("usd", 0), "change_pct": round(d.get("usd_24h_change", 0), 2)}
    except:
        return {"current": 0, "change_pct": 0}

def get_news(key, count=5):
    try:
        r = requests.get("https://finnhub.io/api/v1/news", params={"category": "general", "token": key}, timeout=10)
        return [n.get("headline", "") for n in r.json()[:count]]
    except:
        return []

def build_snapshot(key):
    snap = {}
    for k, info in WATCHLIST.items():
        q = get_btc() if k == "BTC" else get_quote(info["finnhub"], key)
        snap[k] = {**info, **q}
        time.sleep(0.3)
    return snap

def fmt(snap):
    out = []
    for k, d in snap.items():
        p = d.get("current", 0)
        c = d.get("change_pct", 0)
        out.append(f"{k}: \${p:,.2f} {'up' if c>=0 else 'dn'} {abs(c):.2f}%")
    return "\n".join(out)

def ai_brief(mode, snap, news, cfg, alerts=None):
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    now = datetime.now(PT).strftime("%a %b %d %I:%M%p PT")
    mkt = fmt(snap)
    nws = "\n".join(news[:5])
    if mode == "morning":
        p = f"Time: {now}. 30 min before NYSE open.\nMarket:\n{mkt}\nNews:\n{nws}\n\nWrite a morning trading brief. Plain text only.\n\nMORNING BRIEF {now}\n[one line bias]\n\nLEVELS:\nCL: S x R x\nES: S x R x\nNQ: S x R x\nBTC: S x R x\n\nWATCH: [2 things at open]\nNEWS: [top catalyst]"
    elif mode == "evening":
        p = f"Time: {now}.\nMarket:\n{mkt}\nNews:\n{nws}\n\nWrite evening recap. Plain text only.\n\nEVENING RECAP {now}\n[day summary]\nMOVERS: [top 3]\nOVERNIGHT: [watch]\nTOMORROW: [setup]"
    else:
        al = "\n".join([f"{a['symbol']} {a['direction']} {a['pct']:.2f}%" for a in (alerts or [])])
        p = f"ALERT:\n{al}\nMarket:\n{mkt}\nWrite brief urgent SMS alert plain text under 160 chars."
    try:
        msg = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=500, messages=[{"role": "user", "content": p}])
        return msg.content[0].text.strip()
    except Exception as e:
        return f"Error: {mkt}"

def sms(body, cfg):
    c = Client(cfg["twilio_account_sid"], cfg["twilio_auth_token"])
    m = c.messages.create(body=body[:1550], from_=cfg["twilio_from_number"], to=cfg["your_phone_number"])
    log.info(f"SMS sent: {m.sid}")

def load_last():
    try:
        with open(LAST) as f:
            return json.load(f)
    except:
        return {}

def save_last(snap):
    with open(LAST, "w") as f:
        json.dump({k: v.get("current", 0) for k, v in snap.items()}, f)

def check_alerts(snap):
    last = load_last()
    hits = []
    for k, d in snap.items():
        cur = d.get("current", 0)
        prv = last.get(k, 0)
        if not cur or not prv:
            continue
        pct = abs((cur - prv) / prv) * 100
        if pct >= d.get("alert_pct", 0.5):
            hits.append({"symbol": k, "direction": "UP" if cur > prv else "DOWN", "pct": round(pct, 2), "current": cur})
    save_last(snap)
    return hits

def setup():
    print("=== Trading Assistant Setup ===")
    keys = {}
    for key, label in [
        ("finnhub_api_key",    "Finnhub API key"),
        ("anthropic_api_key",  "Anthropic API key"),
        ("twilio_account_sid", "Twilio Account SID"),
        ("twilio_auth_token",  "Twilio Auth Token"),
        ("twilio_from_number", "Twilio phone number +1xxxxxxxxxx"),
        ("your_phone_number",  "Your cell number +1xxxxxxxxxx"),
    ]:
        keys[key] = input(f"{label}: ").strip()
    with open(CFG, "w") as f:
        json.dump(keys, f, indent=2)
    print("Config saved. Sending test SMS...")
    snap = build_snapshot(keys["finnhub_api_key"])
    sms(f"Trading Assistant is live!\n\n{fmt(snap)}", keys)
    print("Done! Check your phone.")

def daemon(cfg):
    log.info("Daemon started.")
    last_morning = last_evening = None
    while True:
        now = datetime.now(PT)
        today = now.date()
        wd = now.weekday() < 5
        if wd and now.hour == 6 and now.minute < 5 and last_morning != today:
            snap = build_snapshot(cfg["finnhub_api_key"])
            sms(ai_brief("morning", snap, get_news(cfg["finnhub_api_key"]), cfg), cfg)
            last_morning = today
            time.sleep(300)
        elif wd and now.hour == 13 and 5 <= now.minute < 10 and last_evening != today:
            snap = build_snapshot(cfg["finnhub_api_key"])
            sms(ai_brief("evening", snap, get_news(cfg["finnhub_api_key"]), cfg), cfg)
            last_evening = today
            time.sleep(300)
        else:
            snap = build_snapshot(cfg["finnhub_api_key"])
            hits = check_alerts(snap)
            if hits:
                sms(ai_brief("alert", snap, get_news(cfg["finnhub_api_key"], 3), cfg, hits), cfg)
            time.sleep(600 if (wd and 6 <= now.hour < 13) else 1800)

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "help"
    if mode == "setup":
        setup()
        return
    cfg = load_config()
    snap = build_snapshot(cfg["finnhub_api_key"])
    if mode == "morning":
        b = ai_brief("morning", snap, get_news(cfg["finnhub_api_key"]), cfg)
        print(b); sms(b, cfg)
    elif mode == "evening":
        b = ai_brief("evening", snap, get_news(cfg["finnhub_api_key"]), cfg)
        print(b); sms(b, cfg)
    elif mode == "alert":
        hits = check_alerts(snap)
        if hits:
            b = ai_brief("alert", snap, get_news(cfg["finnhub_api_key"], 3), cfg, hits)
            print(b); sms(b, cfg)
        else:
            print("No alerts.")
    elif mode == "snapshot":
        print(fmt(snap))
    elif mode == "test":
        sms(f"Test!\n{fmt(snap)}", cfg)
        print("Test SMS sent.")
    elif mode == "daemon":
        daemon(cfg)
    else:
        print("Commands: setup morning evening alert snapshot test daemon")

if __name__ == "__main__":
    main()
