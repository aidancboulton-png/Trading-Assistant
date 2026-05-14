"""Multi-channel alert hub.
- SMS via Twilio
- Twitter watcher (mentions, accounts to follow) — feeds research swarm
- News watcher (RSS / Finnhub headlines)
- YouTube uploads watcher
"""
from __future__ import annotations
import logging, time
from typing import Any
from . import storage
from .data import Twitter, RSS, YouTube, Finnhub

log = logging.getLogger(__name__)


# ============================================================ Twilio SMS
class SMS:
    def __init__(self, sid: str | None, token: str | None,
                 from_num: str | None, to_num: str | None):
        self.sid = sid; self.token = token
        self.from_num = from_num; self.to_num = to_num
        self.client = None
        if all([sid, token, from_num, to_num]):
            try:
                from twilio.rest import Client  # type: ignore
                self.client = Client(sid, token)
            except Exception as e:
                log.warning("Twilio init failed: %s", e)

    def available(self) -> bool:
        return self.client is not None

    def send(self, body: str) -> dict:
        if not self.client:
            log.info("[SMS-DRY] %s", body[:160]); return {"ok": False, "msg": "no client"}
        try:
            m = self.client.messages.create(
                body=body[:1550], from_=self.from_num, to=self.to_num)
            log.info("SMS sent: %s", m.sid)
            return {"ok": True, "sid": m.sid}
        except Exception as e:
            log.warning("SMS send failed: %s", e)
            return {"ok": False, "msg": str(e)}


# ============================================================ Watchers (poll)
class Notifier:
    """Periodic-poll watchers that drop events into storage.alerts_log."""
    def __init__(self, twitter: Twitter, rss_feeds: list[str],
                 youtube: YouTube, youtube_channels: list[str],
                 twitter_accounts: list[str], finnhub: Finnhub | None = None):
        self.tw = twitter; self.rss = rss_feeds
        self.yt = youtube; self.yt_ch = youtube_channels
        self.tw_accounts = twitter_accounts
        self.fn = finnhub

    def poll_twitter(self) -> int:
        if not self.tw.available() or not self.tw_accounts:
            return 0
        n = 0
        for tweet in self.tw.watch(self.tw_accounts):
            storage.log_alert("twitter", "watcher", tweet); n += 1
        return n

    def poll_rss(self) -> int:
        items = RSS.fetch(self.rss, per_feed=10)
        for it in items:
            storage.log_alert("rss", it.get("feed", ""), it)
        return len(items)

    def poll_news(self) -> int:
        if not self.fn or not self.fn.available():
            return 0
        items = self.fn.news(20)
        for it in items:
            storage.log_alert("news", "finnhub", it)
        return len(items)

    def poll_youtube(self) -> int:
        if not self.yt.available() or not self.yt_ch:
            return 0
        n = 0
        for ch in self.yt_ch:
            for v in self.yt.latest_uploads(ch, max_results=5):
                storage.log_alert("youtube", ch, v); n += 1
        return n

    def poll_all(self) -> dict[str, int]:
        return {
            "twitter": self.poll_twitter(),
            "rss":     self.poll_rss(),
            "news":    self.poll_news(),
            "youtube": self.poll_youtube(),
        }
