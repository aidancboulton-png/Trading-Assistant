"""External data sources. Each source has a small adapter class with a
uniform `available()` flag so missing keys gracefully degrade to stubs."""
from __future__ import annotations
import os, time, logging, hashlib
from typing import Any
import requests

log = logging.getLogger(__name__)


# ============================================================ Finnhub (equities)
class Finnhub:
    BASE = "https://finnhub.io/api/v1"

    def __init__(self, key: str | None):
        self.key = key

    def available(self) -> bool:
        return bool(self.key)

    def quote(self, symbol: str) -> dict:
        if not self.key:
            return {"current": 0, "change_pct": 0}
        try:
            r = requests.get(f"{self.BASE}/quote",
                             params={"symbol": symbol, "token": self.key},
                             timeout=10).json()
            c = r.get("c", 0) or 0
            pc = r.get("pc", 1) or 1
            return {"current": c,
                    "change_pct": round(((c - pc) / pc) * 100, 2) if pc else 0}
        except Exception as e:
            log.warning("Finnhub quote failed: %s", e)
            return {"current": 0, "change_pct": 0}

    def news(self, count: int = 10) -> list[dict]:
        if not self.key:
            return []
        try:
            r = requests.get(f"{self.BASE}/news",
                             params={"category": "general", "token": self.key},
                             timeout=10).json()
            return [{"headline": n.get("headline", ""),
                     "summary": n.get("summary", ""),
                     "source": n.get("source", ""),
                     "url": n.get("url", ""),
                     "ts": n.get("datetime", time.time())} for n in r[:count]]
        except Exception as e:
            log.warning("Finnhub news failed: %s", e)
            return []


# ============================================================ CoinGecko (BTC)
class CoinGecko:
    @staticmethod
    def btc() -> dict:
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin", "vs_currencies": "usd",
                        "include_24hr_change": "true"}, timeout=10).json()
            d = r.get("bitcoin", {})
            return {"current": d.get("usd", 0),
                    "change_pct": round(d.get("usd_24h_change", 0), 2)}
        except Exception as e:
            log.warning("CoinGecko failed: %s", e)
            return {"current": 0, "change_pct": 0}


# ============================================================ Polymarket
class Polymarket:
    """Read-only market scanner using gamma + clob. On-chain execution
    requires `wallet_private_key` and is delegated to py_clob_client."""

    GAMMA = "https://gamma-api.polymarket.com"
    CLOB = "https://clob.polymarket.com"

    def __init__(self, api_key: str | None = None,
                 funder: str | None = None,
                 wallet_pk: str | None = None):
        self.api_key = api_key
        self.funder = funder
        self.wallet_pk = wallet_pk

    def available(self) -> bool:
        return True  # public endpoints

    def list_markets(self, limit: int = 500, active: bool = True,
                     closed: bool = False) -> list[dict]:
        try:
            r = requests.get(f"{self.GAMMA}/markets",
                             params={"limit": limit,
                                     "active": str(active).lower(),
                                     "closed": str(closed).lower()},
                             timeout=15).json()
            out = []
            for m in r:
                out.append({
                    "slug":       m.get("slug"),
                    "question":   m.get("question"),
                    "yes_price":  float(m.get("lastTradePrice") or 0.5),
                    "volume_24h": float(m.get("volume24hr") or 0),
                    "liquidity":  float(m.get("liquidity") or 0),
                    "end_date":   m.get("endDate"),
                    "category":   m.get("category"),
                    "outcomes":   m.get("outcomes"),
                })
            return out
        except Exception as e:
            log.warning("Polymarket list_markets failed: %s", e)
            return []

    def market(self, slug: str) -> dict | None:
        try:
            r = requests.get(f"{self.GAMMA}/markets",
                             params={"slug": slug}, timeout=10).json()
            return r[0] if r else None
        except Exception as e:
            log.warning("Polymarket market(%s) failed: %s", slug, e)
            return None

    def execute(self, slug: str, side: str, size_usd: float, price: float) -> dict:
        """Execute a Polymarket order. Returns {ok, tx, msg}.
        Requires py_clob_client + wallet credentials."""
        if not (self.wallet_pk and self.funder):
            return {"ok": False, "tx": None,
                    "msg": "wallet_private_key/funder not configured"}
        try:
            from py_clob_client.client import ClobClient            # type: ignore
            from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
            from py_clob_client.constants import POLYGON               # type: ignore
            client = ClobClient(self.CLOB, key=self.wallet_pk,
                                chain_id=POLYGON, signature_type=2,
                                funder=self.funder)
            client.set_api_creds(client.create_or_derive_api_creds())
            tok = self.market(slug)
            if not tok:
                return {"ok": False, "tx": None, "msg": "market not found"}
            token_id = tok["clobTokenIds"][0 if side.upper() == "YES" else 1]
            args = OrderArgs(price=price, size=size_usd / price,
                             side=side.upper(), token_id=token_id)
            signed = client.create_order(args)
            resp = client.post_order(signed, OrderType.GTC)
            return {"ok": True, "tx": resp, "msg": "submitted"}
        except Exception as e:
            return {"ok": False, "tx": None, "msg": str(e)}


# ============================================================ Twitter / X
class Twitter:
    def __init__(self, bearer: str | None):
        self.bearer = bearer

    def available(self) -> bool:
        return bool(self.bearer)

    def search(self, query: str, max_results: int = 50) -> list[dict]:
        if not self.bearer:
            return []
        try:
            r = requests.get(
                "https://api.twitter.com/2/tweets/search/recent",
                params={"query": query, "max_results": min(100, max_results),
                        "tweet.fields": "created_at,public_metrics,author_id"},
                headers={"Authorization": f"Bearer {self.bearer}"},
                timeout=15).json()
            return r.get("data", [])
        except Exception as e:
            log.warning("Twitter search failed: %s", e)
            return []

    def watch(self, accounts: list[str]) -> list[dict]:
        """Return latest tweets across watched accounts."""
        if not self.bearer or not accounts:
            return []
        q = " OR ".join(f"from:{a.lstrip('@')}" for a in accounts)
        return self.search(q, max_results=100)


# ============================================================ Reddit
class Reddit:
    def __init__(self, client_id: str | None, secret: str | None,
                 user_agent: str = "jarvis/1.0"):
        self.client_id = client_id; self.secret = secret; self.ua = user_agent
        self._reddit = None

    def available(self) -> bool:
        return bool(self.client_id and self.secret)

    def _client(self):
        if self._reddit is None and self.available():
            try:
                import praw  # type: ignore
                self._reddit = praw.Reddit(
                    client_id=self.client_id, client_secret=self.secret,
                    user_agent=self.ua)
            except Exception as e:
                log.warning("PRAW init failed: %s", e)
        return self._reddit

    def search(self, subreddit: str, query: str, limit: int = 50) -> list[dict]:
        r = self._client()
        if not r:
            return []
        try:
            return [{
                "title": p.title, "body": p.selftext[:500],
                "score": p.score, "num_comments": p.num_comments,
                "ts": p.created_utc, "url": p.url,
            } for p in r.subreddit(subreddit).search(query, limit=limit)]
        except Exception as e:
            log.warning("Reddit search failed: %s", e)
            return []


# ============================================================ RSS
class RSS:
    @staticmethod
    def fetch(feeds: list[str], per_feed: int = 20) -> list[dict]:
        try:
            import feedparser  # type: ignore
        except ImportError:
            log.warning("feedparser not installed")
            return []
        out: list[dict] = []
        for url in feeds:
            try:
                f = feedparser.parse(url)
                for e in f.entries[:per_feed]:
                    out.append({
                        "title": e.get("title", ""),
                        "summary": e.get("summary", "")[:500],
                        "link": e.get("link", ""),
                        "published": e.get("published", ""),
                        "feed": url,
                    })
            except Exception as ex:
                log.warning("RSS %s failed: %s", url, ex)
        return out


# ============================================================ YouTube
class YouTube:
    def __init__(self, api_key: str | None):
        self.api_key = api_key

    def available(self) -> bool:
        return bool(self.api_key)

    def latest_uploads(self, channel_id: str, max_results: int = 5) -> list[dict]:
        if not self.api_key:
            return []
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"key": self.api_key, "channelId": channel_id,
                        "part": "snippet", "order": "date",
                        "maxResults": max_results, "type": "video"},
                timeout=15).json()
            return [{
                "video_id": i["id"]["videoId"],
                "title": i["snippet"]["title"],
                "description": i["snippet"]["description"],
                "published": i["snippet"]["publishedAt"],
                "channel": i["snippet"]["channelTitle"],
            } for i in r.get("items", [])]
        except Exception as e:
            log.warning("YouTube uploads failed: %s", e)
            return []

    @staticmethod
    def transcript(video_id: str) -> str:
        try:
            from yt_dlp import YoutubeDL  # type: ignore
            with YoutubeDL({"quiet": True, "skip_download": True,
                            "writesubtitles": True, "writeautomaticsub": True,
                            "subtitleslangs": ["en"]}) as ydl:
                info = ydl.extract_info(
                    f"https://youtu.be/{video_id}", download=False)
                subs = info.get("automatic_captions") or info.get("subtitles") or {}
                en = subs.get("en") or []
                if en:
                    return en[0].get("url", "")
        except Exception as e:
            log.warning("YouTube transcript failed: %s", e)
        return ""


# ============================================================ Calendar
class EconCalendar:
    """Lightweight event-feed: combines Finnhub econ calendar + user-supplied dates."""

    def __init__(self, finnhub_key: str | None,
                 extra_events: list[dict] | None = None):
        self.key = finnhub_key
        self.extra = extra_events or []

    def upcoming(self, days: int = 14) -> list[dict]:
        events: list[dict] = list(self.extra)
        if self.key:
            try:
                r = requests.get(
                    "https://finnhub.io/api/v1/calendar/economic",
                    params={"token": self.key}, timeout=15).json()
                for e in (r.get("economicCalendar") or [])[:200]:
                    events.append({
                        "name": e.get("event"),
                        "country": e.get("country"),
                        "ts": e.get("time"),
                        "impact": e.get("impact"),
                        "actual": e.get("actual"),
                        "estimate": e.get("estimate"),
                    })
            except Exception as e:
                log.warning("Finnhub econ calendar failed: %s", e)
        return events
