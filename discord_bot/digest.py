"""
Scheduled briefings — pre-market and end-of-day wrap, posted to a configured
channel in the layered-intelligence format.
"""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

from . import config, data_client, intelligence


ET = ZoneInfo("America/New_York")


async def _build_premarket_text() -> str:
    snap = await data_client.snapshot()
    news = await data_client.news()
    headlines = []
    items = news.get("articles") or news.get("items") or []
    for n in items[:10]:
        h = n.get("title") or n.get("headline")
        if h:
            headlines.append(f"- {h}")
    ctx = (
        intelligence.compact_snapshot(snap)
        + "\n\nTOP HEADLINES:\n"
        + "\n".join(headlines)
    )
    prompt = (
        "Write the Conviction Capital pre-market briefing for today. "
        "Open with a one-line state-of-play. Then run the most important "
        "story through the 5-level layered format. Close with the "
        "'How does this affect MY life?' tie-back."
    )
    return await intelligence.explain(prompt, context=ctx, max_tokens=1600)


async def _build_wrap_text() -> str:
    snap = await data_client.snapshot()
    sent = await data_client.sentiment()
    corr = await data_client.correlations()
    ctx = (
        intelligence.compact_snapshot(snap)
        + "\n\nSENTIMENT:\n" + json.dumps(sent)[:1500]
        + "\n\nCORRELATIONS:\n" + json.dumps(corr)[:1500]
    )
    prompt = (
        "Write the Conviction Capital end-of-day wrap. "
        "What actually happened today, why it matters, and what to watch "
        "tomorrow. Use the 5-level layered format on the most important move "
        "of the day. End with 'How does this affect MY life?'."
    )
    return await intelligence.explain(prompt, context=ctx, max_tokens=1600)


async def _post(client: discord.Client, channel_id: str, text: str) -> None:
    if not channel_id:
        return
    ch = client.get_channel(int(channel_id))
    if ch is None:
        try:
            ch = await client.fetch_channel(int(channel_id))
        except Exception:
            return
    # Discord caps at 2000 chars/message — chunk safely on paragraph boundaries.
    for chunk in _chunk(text, 1900):
        await ch.send(chunk)


def _chunk(text: str, n: int):
    if len(text) <= n:
        yield text
        return
    buf = ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > n:
            yield buf
            buf = para
        else:
            buf = (buf + "\n\n" + para) if buf else para
    if buf:
        yield buf


async def schedule_loop(client: discord.Client) -> None:
    """Awaits until the next 7am ET / 4:30pm ET slot, then posts."""
    last_premarket = None
    last_wrap = None
    while True:
        try:
            now = datetime.now(ET)
            today = now.date()
            if (
                now.hour == config.PREMARKET_HOUR_ET
                and last_premarket != today
                and config.DISCORD_BRIEFING_CHANNEL_ID
            ):
                text = await _build_premarket_text()
                await _post(client, config.DISCORD_BRIEFING_CHANNEL_ID,
                            "**PRE-MARKET BRIEFING**\n\n" + text)
                last_premarket = today
            if (
                now.hour == config.WRAP_HOUR_ET
                and now.minute >= config.WRAP_MINUTE_ET
                and last_wrap != today
                and config.DISCORD_BRIEFING_CHANNEL_ID
            ):
                text = await _build_wrap_text()
                await _post(client, config.DISCORD_BRIEFING_CHANNEL_ID,
                            "**MARKET WRAP**\n\n" + text)
                last_wrap = today
        except Exception as e:
            print(f"[digest] error: {e}")
        await asyncio.sleep(60)
