"""The five agents of JARVIS.

Each agent has a uniform interface:

    class Agent:
        name: str
        def run(self, ctx: dict) -> dict   # returns findings dict
"""
from __future__ import annotations
import math, time, logging, statistics, hashlib, json
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from datetime import datetime, timezone

from . import storage, bankroll as bk, rules
from .llm import LLM
from .data import (Finnhub, CoinGecko, Polymarket, Twitter, Reddit,
                   RSS, YouTube, EconCalendar)

log = logging.getLogger(__name__)


# ============================================================ 1. MARKET FILTER
class MarketFilterAgent:
    """Step 2: scan 300+ markets → filter → small set of qualified opportunities."""
    name = "market_filter"

    def __init__(self, pm: Polymarket, min_liquidity: float = 50_000,
                 min_volume_24h: float = 10_000, max_days_to_resolution: int = 14,
                 min_edge_proxy: float = 0.03):
        self.pm = pm
        self.min_liquidity = min_liquidity
        self.min_volume = min_volume_24h
        self.max_days = max_days_to_resolution
        self.min_edge = min_edge_proxy

    def run(self, ctx: dict | None = None) -> dict:
        ctx = ctx or {}
        markets = self.pm.list_markets(limit=500)
        total = len(markets)

        # Stage 1: liquidity
        s1 = [m for m in markets if (m.get("liquidity") or 0) >= self.min_liquidity]
        # Stage 2: volume
        s2 = [m for m in s1 if (m.get("volume_24h") or 0) >= self.min_volume]
        # Stage 3: time-to-resolution
        s3 = []
        now = datetime.now(timezone.utc)
        for m in s2:
            try:
                end = datetime.fromisoformat((m.get("end_date") or "").replace("Z", "+00:00"))
                days = (end - now).days
                if 0 <= days <= self.max_days:
                    m["days_to_resolution"] = days
                    s3.append(m)
            except Exception:
                continue
        # Stage 4: edge proxy = |0.5 - yes_price| > min  (markets near coin-flip
        #   are noisy; clear directional sentiment opportunities show as deviations)
        s4 = [m for m in s3 if abs(0.5 - (m.get("yes_price") or 0.5)) >= self.min_edge]

        return {
            "agent": self.name,
            "stages": {
                "input": total,
                "liquidity_pass": len(s1),
                "volume_pass": len(s2),
                "time_pass": len(s3),
                "edge_pass": len(s4),
            },
            "qualified": s4[:50],   # cap result
        }


# ============================================================ 2. RESEARCH SWARM
@dataclass
class SubAgentResult:
    name: str
    posts: int = 0
    sentiment_pct: float = 0.0
    bullish_signals: list[str] = field(default_factory=list)
    bearish_signals: list[str] = field(default_factory=list)
    raw: list[dict] = field(default_factory=list)


class ResearchSwarmAgent:
    """Step 1: parallel agents on Twitter / Reddit / RSS / YouTube.
    Sentiment vs. market odds → bullish/bearish bias."""
    name = "research_swarm"

    BULLISH = ["bullish", "rally", "surge", "breakout", "buy", "long",
               "moon", "pump", "uptrend", "support holds"]
    BEARISH = ["bearish", "dump", "crash", "breakdown", "sell", "short",
               "puts", "downtrend", "resistance", "weakness", "pullback"]

    def __init__(self, twitter: Twitter, reddit: Reddit, rss_feeds: list[str],
                 youtube: YouTube, youtube_channels: list[str], llm: LLM | None = None):
        self.tw = twitter; self.rd = reddit; self.rss_feeds = rss_feeds
        self.yt = youtube; self.yt_channels = youtube_channels
        self.llm = llm

    @staticmethod
    def _score(text: str) -> tuple[int, int]:
        t = (text or "").lower()
        b = sum(1 for w in ResearchSwarmAgent.BULLISH if w in t)
        x = sum(1 for w in ResearchSwarmAgent.BEARISH if w in t)
        return b, x

    def _twitter(self, query: str) -> SubAgentResult:
        r = SubAgentResult(name="twitter")
        items = self.tw.search(query, max_results=100) if self.tw.available() else []
        bull = bear = 0
        for it in items:
            b, x = self._score(it.get("text", ""))
            bull += b; bear += x
        r.posts = len(items); r.raw = items
        r.sentiment_pct = round(100 * bull / max(1, bull + bear), 1)
        return r

    def _reddit(self, query: str, subs: list[str]) -> SubAgentResult:
        r = SubAgentResult(name="reddit")
        items: list[dict] = []
        if self.rd.available():
            for s in subs:
                items.extend(self.rd.search(s, query, limit=25))
        bull = bear = 0
        for it in items:
            b, x = self._score(it.get("title", "") + " " + it.get("body", ""))
            bull += b; bear += x
        r.posts = len(items); r.raw = items
        r.sentiment_pct = round(100 * bull / max(1, bull + bear), 1)
        return r

    def _rss(self, query: str) -> SubAgentResult:
        r = SubAgentResult(name="rss")
        items = RSS.fetch(self.rss_feeds, per_feed=20)
        items = [i for i in items
                 if query.lower() in (i.get("title", "") + i.get("summary", "")).lower()]
        bull = bear = 0
        for it in items:
            b, x = self._score(it.get("title", "") + " " + it.get("summary", ""))
            bull += b; bear += x
        r.posts = len(items); r.raw = items
        r.sentiment_pct = round(100 * bull / max(1, bull + bear), 1)
        return r

    def _youtube(self, query: str) -> SubAgentResult:
        r = SubAgentResult(name="youtube")
        items: list[dict] = []
        if self.yt.available():
            for ch in self.yt_channels:
                items.extend(self.yt.latest_uploads(ch, max_results=5))
        items = [i for i in items
                 if query.lower() in (i.get("title", "") + i.get("description", "")).lower()]
        bull = bear = 0
        for it in items:
            b, x = self._score(it.get("title", "") + " " + it.get("description", ""))
            bull += b; bear += x
        r.posts = len(items); r.raw = items
        r.sentiment_pct = round(100 * bull / max(1, bull + bear), 1)
        return r

    def run(self, ctx: dict) -> dict:
        query = ctx.get("query") or ctx.get("market") or ""
        subs = ctx.get("subreddits") or ["wallstreetbets", "stocks", "investing", "CryptoCurrency"]
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {
                ex.submit(self._twitter, query): "twitter",
                ex.submit(self._reddit, query, subs): "reddit",
                ex.submit(self._rss, query): "rss",
                ex.submit(self._youtube, query): "youtube",
            }
            results = {futs[f]: f.result() for f in as_completed(futs)}

        # Aggregate weighted by post count
        total_posts = sum(r.posts for r in results.values()) or 1
        weighted = sum(r.sentiment_pct * r.posts for r in results.values()) / total_posts
        narrative = "BULLISH" if weighted >= 60 else "BEARISH" if weighted <= 40 else "MIXED"
        return {
            "agent": self.name,
            "query": query,
            "sub_agents": {k: vars(v) for k, v in results.items()},
            "aggregate_sentiment_pct": round(weighted, 1),
            "narrative": narrative,
            "total_posts": total_posts,
        }


# ============================================================ 3. PREDICTION
class PredictionAgent:
    """Step 3: XGBoost classifier (raw) → LLM calibrator (adjusted true_prob)."""
    name = "prediction"

    DEFAULT_FEATURES = [
        "historical_accuracy", "volume_momentum", "sentiment_score",
        "time_decay", "market_correlation",
    ]

    def __init__(self, llm: LLM | None = None, model_path: str | None = None):
        self.llm = llm
        self.model_path = model_path
        self._model = None

    # ---- raw probability via XGBoost (or fallback heuristic)
    def _xgboost(self, features: dict[str, float]) -> float:
        try:
            import xgboost as xgb  # type: ignore
            import numpy as np
            if self._model is None and self.model_path:
                self._model = xgb.Booster(); self._model.load_model(self.model_path)
            if self._model is not None:
                X = np.array([[features.get(k, 0.0) for k in self.DEFAULT_FEATURES]])
                return float(self._model.predict(xgb.DMatrix(X))[0])
        except Exception:
            pass
        # Fallback: logistic over a hand-tuned linear combo
        z = (
            1.5 * features.get("historical_accuracy", 0.5)
            + 0.8 * features.get("volume_momentum", 0)
            + 1.2 * (features.get("sentiment_score", 0.5) - 0.5)
            + 0.6 * features.get("time_decay", 0)
            + 0.4 * features.get("market_correlation", 0)
        )
        return 1.0 / (1.0 + math.exp(-z))

    # ---- LLM calibrator
    def _calibrate(self, raw_prob: float, ctx: dict) -> dict:
        if not (self.llm and self.llm.available()):
            return {"adjustment": 0.0, "uncertainty": 0.08,
                    "news_analysis": "neutral",
                    "expert_consensus": "n/a", "rationale": "LLM unavailable"}
        prompt = (
            f"Market: {ctx.get('market')}\n"
            f"Question: {ctx.get('question')}\n"
            f"Raw model probability (YES): {raw_prob:.3f}\n"
            f"Recent narrative: {ctx.get('narrative')}\n"
            f"News (last 24h):\n{ctx.get('news_summary','(none)')}\n\n"
            "As a probability calibrator, decide whether to adjust the raw probability. "
            "Return JSON with keys: adjustment (-0.15..+0.15), uncertainty (0..0.2), "
            "news_analysis (str), expert_consensus (str), rationale (str)."
        )
        return self.llm.json_complete(prompt, schema_hint=
            "{adjustment, uncertainty, news_analysis, expert_consensus, rationale}")

    def run(self, ctx: dict) -> dict:
        feats = ctx.get("features") or {}
        raw = self._xgboost(feats)
        calib = self._calibrate(raw, ctx)
        adj = float(calib.get("adjustment", 0) or 0)
        true_prob = max(0.01, min(0.99, raw + adj))
        market_price = float(ctx.get("market_price", 0.5) or 0.5)
        edge = true_prob - market_price          # YES side
        return {
            "agent": self.name,
            "features": feats,
            "raw_prob": round(raw, 4),
            "calibration": calib,
            "true_prob": round(true_prob, 4),
            "market_price": market_price,
            "edge_pct": round(edge, 4),
            "side": "YES" if edge > 0 else "NO",
            "abs_edge_pct": round(abs(edge), 4),
        }


# ============================================================ 4. RISK / EXEC
class RiskAgent:
    """Step 4: Kelly sizing + rules-engine gate + (optional) on-chain execute."""
    name = "risk"

    def __init__(self, pm: Polymarket, limits: dict, kelly_fraction: float = 0.5):
        self.pm = pm; self.limits = limits; self.kelly_fraction = kelly_fraction

    def run(self, ctx: dict) -> dict:
        true_prob = float(ctx.get("true_prob", 0.5))
        market_price = float(ctx.get("market_price", 0.5))
        bankroll_usd = float(ctx.get("bankroll_usd", 0.0))
        side = ctx.get("side", "YES")

        # YES-side kelly assumes you bet at market_price; for NO-side flip both
        if side == "NO":
            true_prob = 1.0 - true_prob
            market_price = 1.0 - market_price

        kelly = bk.kelly_pm(true_prob, market_price, bankroll_usd, self.kelly_fraction)
        risk = bk.check_limits(kelly.suggested_size_usd, bankroll_usd, self.limits,
                               edge_pct=abs(ctx.get("edge_pct", 0)))

        # Rules engine — may block or shrink
        decision = rules.evaluate({**ctx, "edge_pct": ctx.get("edge_pct"),
                                   "true_prob": true_prob})
        if decision["action"] == "block":
            risk.approved = False
            risk.reasons.extend(decision["reasons"])
        elif decision["action"] == "shrink":
            risk.sized_usd = round(risk.sized_usd * decision["factor"], 2)
            risk.reasons.append(f"shrunk by rule(s): {decision['reasons']}")
        elif decision["action"] == "warn":
            risk.reasons.extend([f"warn: {r}" for r in decision["reasons"]])

        return {
            "agent": self.name,
            "kelly": kelly.__dict__,
            "approved": risk.approved,
            "sized_usd": risk.sized_usd,
            "sized_pct": risk.sized_pct,
            "reasons": risk.reasons,
            "rules_decision": decision,
            "side": side,
        }

    def execute(self, slug: str, side: str, size_usd: float, price: float) -> dict:
        return self.pm.execute(slug, side, size_usd, price)


# ============================================================ 5. POST-MORTEM
class PostMortemAgent:
    """Step 5: 5-sub-agent analysis on every loss → root cause + new rules."""
    name = "postmortem"

    SUB_AGENTS = ["data", "sentiment", "timing", "model", "risk"]

    def __init__(self, llm: LLM | None = None):
        self.llm = llm

    def _ask(self, role: str, prediction: dict) -> dict:
        if not (self.llm and self.llm.available()):
            return {"finding": f"[{role}] LLM unavailable",
                    "severity": "unknown", "rule_suggestion": None}
        prompt = (
            f"You are the {role.upper()} agent in a post-mortem on a losing trade.\n"
            f"Prediction record:\n{json.dumps(prediction, default=str, indent=2)}\n\n"
            f"Identify what {role}-related factor most likely contributed to the loss. "
            "Be specific. Then suggest at most ONE concrete rule that would have "
            "prevented or shrunk this position. Return JSON with keys: "
            "finding (str), severity ('high'|'medium'|'low'), "
            "rule_suggestion (object or null with keys "
            "{key, scope, condition:{feature,op,value}, action, reason})."
        )
        return self.llm.json_complete(prompt,
            schema_hint="{finding, severity, rule_suggestion}")

    def run(self, ctx: dict) -> dict:
        prediction = ctx["prediction"]
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(self._ask, role, prediction): role
                    for role in self.SUB_AGENTS}
            findings = {futs[f]: f.result() for f in as_completed(futs)}

        # Consensus root cause: pick highest severity, fall back to first
        sev_rank = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
        ranked = sorted(findings.items(),
                        key=lambda kv: -sev_rank.get(kv[1].get("severity", "unknown"), 0))
        root_cause = ranked[0][1].get("finding", "(no root cause identified)") if ranked else ""

        # Install rules
        rules_added: list[str] = []
        for role, f in findings.items():
            sug = f.get("rule_suggestion")
            if not sug or not isinstance(sug, dict):
                continue
            try:
                rules.add_rule(
                    key=sug.get("key", f"{role}_{int(time.time())}"),
                    scope=sug.get("scope", "global"),
                    condition=sug.get("condition", {}),
                    action=sug.get("action", "warn"),
                    reason=sug.get("reason", ""),
                )
                rules_added.append(sug.get("key"))
            except Exception as e:
                log.warning("Failed to add rule from %s: %s", role, e)

        incident_id = storage.save_incident(
            prediction_id=prediction.get("id", 0),
            root_cause=root_cause,
            agent_findings=findings,
            rules_added=rules_added,
        )

        return {
            "agent": self.name,
            "incident_id": incident_id,
            "root_cause": root_cause,
            "findings": findings,
            "rules_added": rules_added,
        }
