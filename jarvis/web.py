"""FastAPI dashboard. Serves prototype.html + JSON API for the live agents."""
from __future__ import annotations
import os, json
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import storage


HERE = os.path.dirname(__file__)


def build_app(svc: dict) -> FastAPI:
    app = FastAPI(title="JARVIS")

    @app.get("/", response_class=HTMLResponse)
    def index():
        path = os.path.join(HERE, "prototype.html")
        if os.path.exists(path):
            with open(path) as f:
                return HTMLResponse(f.read())
        return HTMLResponse("<h1>JARVIS</h1><p>prototype.html missing</p>")

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/scan")
    def scan():
        return svc["filt"].run()

    @app.get("/api/research")
    def research(q: str):
        return svc["research"].run({"query": q, "market": q})

    @app.get("/api/predict")
    def predict(slug: str):
        market = svc["pm"].market(slug) or {}
        research_out = svc["research"].run({"query": market.get("question", slug),
                                            "market": slug})
        from .jarvis import _features_from_research, _bankroll
        feats = _features_from_research(research_out, market)
        pred = svc["predict"].run({
            "market": slug, "question": market.get("question", slug),
            "market_price": float(market.get("lastTradePrice") or 0.5),
            "narrative": research_out.get("narrative"),
            "news_summary": research_out.get("narrative"),
            "features": feats,
        })
        risk = svc["risk"].run({
            "market": slug, "domain": "prediction_market",
            "true_prob": pred["true_prob"], "market_price": pred["market_price"],
            "side": pred["side"], "edge_pct": pred["edge_pct"],
            "bankroll_usd": _bankroll(svc["cfg"]),
        })
        return {"market": market, "research": research_out,
                "prediction": pred, "risk": risk}

    @app.get("/api/predictions")
    def predictions():
        return JSONResponse(storage.open_predictions())

    @app.get("/api/rules")
    def rules_list():
        return JSONResponse(storage.active_rules())

    @app.get("/api/bankroll")
    def bankroll():
        return {"bankroll_usd": storage.latest_bankroll(),
                "daily_pnl": storage.daily_pnl()}

    @app.get("/api/alerts")
    def alerts():
        return JSONResponse(storage.unconsumed_alerts(50))

    return app
