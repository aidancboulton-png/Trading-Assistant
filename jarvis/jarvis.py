"""JARVIS — main CLI orchestrator.

Subcommands:
  setup            init DB, save bankroll, send test SMS
  scan             Step 2 — filter live prediction markets
  research <q>     Step 1 — research swarm (twitter/reddit/rss/youtube)
  predict <slug>   Steps 1+3 — research + XGBoost + LLM calibration
  size <slug> <p>  Step 4 — Kelly + risk for given true_prob p
  execute <slug>   Steps 1–4 end-to-end (Polymarket on-chain if configured)
  postmortem <id>  Step 5 — 5-agent post-mortem on a resolved loss
  resolve <id> <WIN|LOSS|PUSH> <pnl_usd>
  poll             one-shot pull from twitter/rss/news/youtube watchers
  daemon           always-on alert watcher + auto-research
  serve            FastAPI dashboard on :8000
  snapshot         legacy: prints equity watchlist (parity with old script)
"""
from __future__ import annotations
import argparse, json, os, sys, time, logging
from datetime import datetime
from zoneinfo import ZoneInfo

from . import storage
from .llm import LLM
from .data import (Finnhub, CoinGecko, Polymarket, Twitter, Reddit,
                   YouTube, EconCalendar)
from .notify import SMS, Notifier
from . import agents

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("jarvis")

CFG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
PT = ZoneInfo("America/Los_Angeles")


# ============================================================ wiring
def load_cfg() -> dict:
    if not os.path.exists(CFG_PATH):
        sys.exit(f"Missing {CFG_PATH}. Copy config.example.json to config.json and edit.")
    with open(CFG_PATH) as f:
        return json.load(f)


def build(cfg: dict):
    llm = LLM(cfg.get("anthropic_api_key"),
              cfg.get("anthropic_model", "claude-sonnet-4-6"))
    fin = Finnhub(cfg.get("finnhub_api_key"))
    pm = Polymarket(cfg.get("polymarket_api_key"),
                    cfg.get("polymarket_funder"),
                    cfg.get("wallet_private_key"))
    tw = Twitter(cfg.get("twitter_bearer_token"))
    rd = Reddit(cfg.get("reddit_client_id"), cfg.get("reddit_secret"),
                cfg.get("reddit_user_agent", "jarvis/1.0"))
    yt = YouTube(cfg.get("youtube_api_key"))
    cal = EconCalendar(cfg.get("finnhub_api_key"))
    sms = SMS(cfg.get("twilio_account_sid"), cfg.get("twilio_auth_token"),
              cfg.get("twilio_from_number"), cfg.get("your_phone_number"))
    notifier = Notifier(tw, cfg.get("rss_feeds", []), yt,
                        cfg.get("youtube_watch", []),
                        cfg.get("twitter_watch", []), fin)

    filt = agents.MarketFilterAgent(pm)
    research = agents.ResearchSwarmAgent(tw, rd, cfg.get("rss_feeds", []), yt,
                                         cfg.get("youtube_watch", []), llm)
    predict = agents.PredictionAgent(llm)
    risk = agents.RiskAgent(pm, cfg.get("limits", {}),
                            cfg.get("limits", {}).get("kelly_fraction", 0.5))
    pm_agent = agents.PostMortemAgent(llm)

    return dict(cfg=cfg, llm=llm, fin=fin, pm=pm, tw=tw, rd=rd, yt=yt, cal=cal,
                sms=sms, notifier=notifier,
                filt=filt, research=research, predict=predict,
                risk=risk, postmortem=pm_agent)


# ============================================================ helpers
def _features_from_research(research_out: dict, market: dict | None,
                            history: dict | None = None) -> dict:
    """Build the 5-feature vector consumed by PredictionAgent (XGBoost)."""
    sentiment = (research_out.get("aggregate_sentiment_pct") or 50) / 100.0
    vol = (market or {}).get("volume_24h", 0) or 0
    vol_mom = max(-1.0, min(1.0, (vol - 50_000) / 200_000))
    days_left = (market or {}).get("days_to_resolution", 14) or 14
    time_decay = max(-1.0, min(1.0, (14 - days_left) / 14))
    return {
        "historical_accuracy": (history or {}).get("hit_rate", 0.6),
        "volume_momentum":     vol_mom,
        "sentiment_score":     sentiment,
        "time_decay":          time_decay,
        "market_correlation":  (history or {}).get("correlation", 0.4),
    }


def _bankroll(cfg: dict) -> float:
    saved = storage.latest_bankroll()
    return saved if saved is not None else float(cfg.get("bankroll_usd", 10_000))


# ============================================================ commands
def cmd_setup(svc):
    cfg = svc["cfg"]
    storage.init_db()
    storage.record_bankroll(float(cfg.get("bankroll_usd", 10_000)),
                            reason="initial setup")
    msg = f"JARVIS initialized — bankroll ${cfg.get('bankroll_usd'):,.0f}"
    print(msg)
    if svc["sms"].available():
        svc["sms"].send(msg)


def cmd_scan(svc):
    out = svc["filt"].run()
    print(json.dumps(out["stages"], indent=2))
    print(f"\n{len(out['qualified'])} qualified markets:")
    for m in out["qualified"][:20]:
        print(f"  {m['slug']:40s}  {m['yes_price']:.2f}  "
              f"vol24={m['volume_24h']:>10.0f}  liq={m['liquidity']:>10.0f}  "
              f"days={m.get('days_to_resolution','?')}")
    return out


def cmd_research(svc, query: str):
    out = svc["research"].run({"query": query, "market": query})
    print(json.dumps({k: v for k, v in out.items() if k != "sub_agents"}, indent=2))
    for k, v in out["sub_agents"].items():
        print(f"  {k:8s} posts={v['posts']:>4}  sentiment={v['sentiment_pct']:>5}%")
    return out


def cmd_predict(svc, slug: str):
    market = svc["pm"].market(slug) or {}
    research_out = svc["research"].run({"query": market.get("question", slug),
                                        "market": slug})
    feats = _features_from_research(research_out, market)
    pred = svc["predict"].run({
        "market": slug,
        "question": market.get("question", slug),
        "market_price": float(market.get("lastTradePrice") or 0.5),
        "narrative": research_out.get("narrative"),
        "news_summary": research_out.get("narrative"),
        "features": feats,
    })
    print(json.dumps({"market": slug, **pred}, indent=2, default=str))
    return {"research": research_out, "prediction": pred, "market": market}


def cmd_size(svc, slug: str, true_prob: float):
    market = svc["pm"].market(slug) or {}
    market_price = float(market.get("lastTradePrice") or 0.5)
    edge = true_prob - market_price
    out = svc["risk"].run({
        "market": slug,
        "domain": "prediction_market",
        "true_prob": true_prob,
        "market_price": market_price,
        "side": "YES" if edge > 0 else "NO",
        "edge_pct": edge,
        "bankroll_usd": _bankroll(svc["cfg"]),
    })
    print(json.dumps(out, indent=2, default=str))
    return out


def cmd_execute(svc, slug: str):
    full = cmd_predict(svc, slug)
    pred = full["prediction"]
    risk_out = svc["risk"].run({
        "market": slug,
        "domain": "prediction_market",
        "true_prob": pred["true_prob"],
        "market_price": pred["market_price"],
        "side": pred["side"],
        "edge_pct": pred["edge_pct"],
        "bankroll_usd": _bankroll(svc["cfg"]),
    })
    pid = storage.save_prediction({
        "domain": "prediction_market",
        "market": slug,
        "question": full["market"].get("question"),
        "side": pred["side"],
        "market_price": pred["market_price"],
        "raw_prob": pred["raw_prob"],
        "true_prob": pred["true_prob"],
        "edge_pct": pred["edge_pct"],
        "confidence": 1.0 - float(pred["calibration"].get("uncertainty", 0.08)),
        "size_usd": risk_out["sized_usd"],
        "features": pred["features"],
        "agents": {"research": full["research"], "prediction": pred, "risk": risk_out},
    })
    print(f"\nSaved prediction id={pid}")
    if not risk_out["approved"]:
        print(f"❌ NOT executed: {risk_out['reasons']}"); return
    if risk_out["sized_usd"] <= 0:
        print("❌ Sized at $0; skipping."); return
    res = svc["risk"].execute(slug, pred["side"],
                              risk_out["sized_usd"], pred["market_price"])
    print(f"Execution: {res}")
    if svc["sms"].available():
        svc["sms"].send(
            f"JARVIS: {pred['side']} {slug} ${risk_out['sized_usd']:.0f} "
            f"@ {pred['market_price']:.2f} | edge {pred['edge_pct']*100:.1f}%")


def cmd_resolve(svc, pred_id: int, resolution: str, pnl_usd: float):
    storage.resolve_prediction(pred_id, resolution.upper(), pnl_usd)
    bk = _bankroll(svc["cfg"]) + pnl_usd
    storage.record_bankroll(bk, delta_usd=pnl_usd, reason=f"pred {pred_id} {resolution}")
    print(f"Resolved {pred_id}: {resolution} ${pnl_usd:+.2f}  →  bankroll ${bk:.2f}")
    if resolution.upper() == "LOSS":
        print("Triggering post-mortem…")
        cmd_postmortem(svc, pred_id)


def cmd_postmortem(svc, pred_id: int):
    pred = storage.get_prediction(pred_id)
    if not pred:
        print(f"No prediction id {pred_id}"); return
    out = svc["postmortem"].run({"prediction": pred})
    print("\n=== POST-MORTEM ===")
    print(f"Root cause: {out['root_cause']}")
    print(f"Rules added: {out['rules_added']}")
    print(json.dumps(out["findings"], indent=2, default=str))
    return out


def cmd_poll(svc):
    counts = svc["notifier"].poll_all()
    print(json.dumps(counts, indent=2))


def cmd_daemon(svc):
    log.info("JARVIS daemon started.")
    while True:
        try:
            counts = svc["notifier"].poll_all()
            log.info("polled: %s", counts)
            time.sleep(300)
        except KeyboardInterrupt:
            log.info("daemon stopped"); return
        except Exception as e:
            log.exception("daemon iteration failed: %s", e)
            time.sleep(60)


def cmd_serve(svc):
    try:
        import uvicorn  # type: ignore
    except ImportError:
        sys.exit("uvicorn not installed; pip install -r requirements.txt")
    from .web import build_app
    app = build_app(svc)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


def cmd_snapshot(svc):
    """Equity watchlist snapshot (parity with old trading_assistant.py)."""
    cfg = svc["cfg"]; fin = svc["fin"]
    out = []
    for k, info in cfg.get("watchlist_equities", {}).items():
        q = (CoinGecko.btc() if k == "BTC"
             else fin.quote(info["finnhub"]))
        c = q.get("current", 0); pct = q.get("change_pct", 0)
        out.append(f"{k}: ${c:,.2f} {'up' if pct>=0 else 'dn'} {abs(pct):.2f}% ({info['name']})")
    print("\n".join(out))


# ============================================================ entrypoint
def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="jarvis")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("scan")
    a = sub.add_parser("research"); a.add_argument("query")
    a = sub.add_parser("predict"); a.add_argument("slug")
    a = sub.add_parser("size"); a.add_argument("slug"); a.add_argument("true_prob", type=float)
    a = sub.add_parser("execute"); a.add_argument("slug")
    a = sub.add_parser("resolve"); a.add_argument("pred_id", type=int); a.add_argument("resolution"); a.add_argument("pnl_usd", type=float)
    a = sub.add_parser("postmortem"); a.add_argument("pred_id", type=int)
    sub.add_parser("poll")
    sub.add_parser("daemon")
    sub.add_parser("serve")
    sub.add_parser("snapshot")
    args = p.parse_args(argv)

    svc = build(load_cfg())
    fn = {
        "setup": lambda: cmd_setup(svc),
        "scan": lambda: cmd_scan(svc),
        "research": lambda: cmd_research(svc, args.query),
        "predict": lambda: cmd_predict(svc, args.slug),
        "size": lambda: cmd_size(svc, args.slug, args.true_prob),
        "execute": lambda: cmd_execute(svc, args.slug),
        "resolve": lambda: cmd_resolve(svc, args.pred_id, args.resolution, args.pnl_usd),
        "postmortem": lambda: cmd_postmortem(svc, args.pred_id),
        "poll": lambda: cmd_poll(svc),
        "daemon": lambda: cmd_daemon(svc),
        "serve": lambda: cmd_serve(svc),
        "snapshot": lambda: cmd_snapshot(svc),
    }[args.cmd]
    fn()


if __name__ == "__main__":
    main()
