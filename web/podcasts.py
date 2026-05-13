"""
Podcast Intelligence Pipeline.

Polls subscribed RSS feeds, downloads new episodes, transcribes them via
OpenAI Whisper, and runs Claude over each transcript to extract a structured
"episode_intel" object: tickers, themes, quotes, layered take, and candidate
content hooks.

Designed to feed the Conviction Capital content factory — every episode
becomes input for long-form videos, Shorts, and IG captions.

NEVER raises. If a step fails, the episode is marked errored and skipped
on the next poll; downstream UI just omits that episode.
"""
import os
import json
import re
import time
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import requests

UA = {"User-Agent": "conviction-capital/1.0"}
TIMEOUT = 20

# ── Storage ─────────────────────────────────────────────────────────────────
_BASE = Path(__file__).parent.parent
_DATA = _BASE / "data" / "podcasts"
_AUDIO_DIR = _DATA / "audio"
_TRANSCRIPT_DIR = _DATA / "transcripts"
_INTEL_DIR = _DATA / "intel"
_INDEX = _DATA / "index.json"  # { episode_id: { ...metadata, state } }

for d in (_AUDIO_DIR, _TRANSCRIPT_DIR, _INTEL_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ── Subscribed shows ────────────────────────────────────────────────────────
# Each entry: id → { name, rss_url, keep_audio (bool) }
# Every source flows into ONE unified "Market Intel & Alpha" feed — there is
# no "wisdom" vs "news" split. Everything is filtered through the same lens:
# "what in this is actionable market intel or alpha right now?"
SUBSCRIBED = {
    "bloomberg_news_now": {
        "name":       "Bloomberg News Now",
        "rss_url":    "https://www.omnycontent.com/d/playlist/e73c998e-6e60-432f-8610-ae210140c5b1/d9566f78-0464-4367-9dcc-b05700aeec6f/7f880b3c-7f67-4b4b-b520-b05700af9172/podcast.rss",
        "keep_audio": False,
    },
    "diary_of_a_ceo": {
        "name":       "The Diary of a CEO",
        "rss_url":    "https://rss2.flightcast.com/xmsftuzjjykcmqwolaqn6mdn",
        "keep_audio": False,
    },
}


# ── Index (persistent state) ────────────────────────────────────────────────
def _load_index() -> dict:
    if not _INDEX.exists():
        return {}
    try:
        return json.loads(_INDEX.read_text())
    except Exception:
        return {}


def _save_index(idx: dict) -> None:
    _INDEX.write_text(json.dumps(idx, indent=2, default=str))


# ── RSS parsing ─────────────────────────────────────────────────────────────
def _episode_id(show_id: str, guid: str) -> str:
    """Deterministic episode id from show + GUID."""
    h = hashlib.sha1(f"{show_id}:{guid}".encode()).hexdigest()[:16]
    return f"{show_id}_{h}"


def fetch_feed(show_id: str) -> list[dict]:
    """Pull current episodes from a show's RSS feed."""
    show = SUBSCRIBED.get(show_id)
    if not show:
        return []
    try:
        r = requests.get(show["rss_url"], headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except Exception as e:
        print(f"[podcasts] {show_id} feed fetch error: {e}")
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    out = []
    for item in channel.findall("item"):
        title_el = item.find("title")
        guid_el  = item.find("guid")
        enc      = item.find("enclosure")
        pub      = item.find("pubDate")
        desc     = item.find("description")
        if title_el is None or enc is None:
            continue
        title    = (title_el.text or "").strip()
        guid_raw = (guid_el.text if guid_el is not None else title).strip()
        audio    = enc.get("url", "")
        if not audio:
            continue
        # Strip podtrac/swap.fm prefixes to get clean audio URL
        for prefix in (
            "https://podtrac.com/pts/redirect.mp3/",
            "https://podtrac.com/pts/redirect.mp3?",
        ):
            if audio.startswith(prefix):
                audio = "https://" + audio[len(prefix):]
                break

        out.append({
            "episode_id":  _episode_id(show_id, guid_raw),
            "show_id":     show_id,
            "show_name":   show["name"],
            "title":       title,
            "guid":        guid_raw,
            "audio_url":   audio,
            "pub_date":    pub.text.strip() if pub is not None and pub.text else "",
            "description": (desc.text or "").strip() if desc is not None and desc.text else "",
        })
    return out


# ── Audio download ──────────────────────────────────────────────────────────
def _audio_path(episode_id: str) -> Path:
    return _AUDIO_DIR / f"{episode_id}.mp3"


def download_audio(ep: dict) -> Optional[Path]:
    """Download episode audio to local file. Returns path or None on failure."""
    path = _audio_path(ep["episode_id"])
    if path.exists() and path.stat().st_size > 0:
        return path
    try:
        with requests.get(ep["audio_url"], headers=UA, stream=True,
                          timeout=60, allow_redirects=True) as r:
            r.raise_for_status()
            tmp = path.with_suffix(".mp3.part")
            with tmp.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
            tmp.rename(path)
        return path
    except Exception as e:
        print(f"[podcasts] download error {ep['episode_id']}: {e}")
        return None


# ── Whisper transcription ───────────────────────────────────────────────────
def _transcript_path(episode_id: str) -> Path:
    return _TRANSCRIPT_DIR / f"{episode_id}.txt"


def transcribe(ep: dict, audio_path: Path) -> Optional[str]:
    """Run OpenAI Whisper. Returns transcript text or None if no key/error."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print(f"[podcasts] OPENAI_API_KEY not set — skipping transcription")
        return None

    out_path = _transcript_path(ep["episode_id"])
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path.read_text()

    try:
        with audio_path.open("rb") as f:
            r = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (audio_path.name, f, "audio/mpeg")},
                data={"model": "whisper-1", "response_format": "text"},
                timeout=600,
            )
            r.raise_for_status()
            text = r.text.strip()
            out_path.write_text(text)
            return text
    except Exception as e:
        print(f"[podcasts] transcription error {ep['episode_id']}: {e}")
        return None


# ── Claude extraction ───────────────────────────────────────────────────────
# ONE unified prompt. Every source (Bloomberg, DOAC, TrendSpider, anything we
# add later) flows through the same lens: "what in this transcript is
# actionable market intel or alpha?" — not life-wisdom, not motivation,
# not show pleasantries. If a source has no market signal, signal_score is
# low and we don't fabricate one.

_INTEL_PROMPT = """You are the intelligence extractor for Conviction Capital — a paid
market-intelligence platform. Every piece of content you process gets
filtered through ONE question:

  "What in this transcript is actionable market intel or alpha right now?"

That is the ONLY thing that matters. Health tips, life advice, motivation,
guest backstory, business memoir — all FLUFF unless it directly translates
to a market-actionable take (a sector view, a ticker, a macro thesis, a
flow signal, a regulatory shift, an industry inflection).

YOUR FIRST JOB IS TO FILTER OUT FLUFF.

Before extracting anything, classify every section of the transcript as
SIGNAL or FLUFF. Then ONLY extract from SIGNAL.

FLUFF — IGNORE COMPLETELY:
- Ad reads, sponsor segments
- Show intros / outros / "thanks for listening" / "subscribe"
- Host pleasantries, weather, small-talk
- Guest CV, book promo, social handles
- Life advice with no market angle (sleep, exercise, mindset, relationships)
- Motivational platitudes ("follow your dreams", "consistency is key")
- Vague macro takes ("the economy is uncertain", "markets are volatile")
- Repetition of the same point without new context
- Anything you could find on the front page of any news site

SIGNAL — EXTRACT FROM THESE:
- Named tickers, sectors, ETFs, commodities, currencies, rates
- Specific dollar amounts, percentages, dates, named parties
- Earnings drivers, guidance changes, capex shifts, M&A specifics
- Forward-looking specifics: scheduled events, deadlines, catalyst dates
- Causal claims: "X happened because Y" with reasoning a trader can use
- Contrarian / non-consensus views from named analysts, CEOs, officials
- Flow / positioning data, technicals with levels, regulatory shifts
- Quotes that name something specific (not host transitional patter)

If after filtering there is NOT ENOUGH MARKET SIGNAL to fill the schema
honestly, return signal_score ≤ 3 and leave fields empty. DO NOT FABRICATE.
A DOAC episode about childhood trauma with no market angle should return
signal_score 1 and almost-empty fields. That is correct behavior.

Return ONLY valid JSON with this shape:

{
  "signal_score":    0-10 integer — how much actionable market signal. 0 = pure life/motivation/fluff with no market angle, 10 = dense tradeable intel.
  "fluff_skipped":   "1-2 sentences naming what you filtered (e.g. '2-min Whoop ad read; 40-min discussion of guest's morning routine with no market relevance').",
  "headline":        "One-line plain-English statement of THE market-relevant thing in this content. If signal_score ≤ 3, write 'No material market signal.'",
  "summary_why":     "One sentence: why a trader/investor should care.",
  "summary_impact":  "One sentence: most likely market/economic impact — with sectors, tickers, or levels if available.",
  "tickers":         ["TICKER1", "TICKER2"],
  "sectors":         ["Energy", "Semis", "Banks", ...],
  "themes":          ["Fed policy", "AI capex", "Trade war", ...],
  "catalysts": [
    {"date": "YYYY-MM-DD or 'unknown'", "event": "what's scheduled", "matters_because": "why it moves things"}
  ],
  "key_quotes": [
    {"speaker": "name or unknown", "quote": "...verbatim non-trivial market-relevant quote..."}
  ],
  "layered_take":    "4-6 sentences in Conviction Capital house style: plain-English fact → structural why → who wins / who loses (named) → how this shows up in a regular portfolio. No hype, no fear-porn, no platitudes.",
  "watch_list": [
    "Specific thing to monitor: ticker / level / data release / date / spread / ratio"
  ],
  "short_hooks": [
    "Hook 1 — hard claim with a specific number, name, ticker, or date",
    "Hook 2 — contrarian framing of the same intel",
    "Hook 3 — 'what they're not telling you' angle, with specifics"
  ],
  "long_form_outline": [
    "Section 1: the hook — specific claim",
    "Section 2: the evidence — data / quote / event cited",
    "Section 3: the mechanism — why this moves markets",
    "Section 4: the trade / position implication",
    "Section 5: what to watch next — named catalyst or level"
  ],
  "ig_caption":      "Instagram caption — punchy first line with a specific number/ticker, then 2-3 short lines, then 5 hashtags"
}

Rules:
- Output ONLY the JSON object. No markdown fences, no preamble.
- Empty is honest. Padded is harmful to the brand. Empty arrays / empty strings are FINE.
- Tickers must be REAL companies/ETFs explicitly tied to the SIGNAL — never invent.
- Quotes MUST be verbatim. If no quote passes the bar, return [].
- Every short hook must contain at least one specific number, ticker, name, or date.
- The layered_take always ends with how this shows up for a regular investor.
- If the content is a long-form life/wisdom podcast with NO market angle, return signal_score ≤ 3 with mostly-empty fields and a one-line headline saying so. Do not try to "save" it by inventing market relevance.

Transcript:
---
{TRANSCRIPT}
---
"""


# Default watchlist tickers used for triage when no caller-provided list is
# supplied. Matches the dashboard WATCHLIST in web/app.py. Kept local to avoid
# a circular import.
_DEFAULT_WATCHLIST = [
    "SPY", "QQQ", "DIA", "IWM", "VIXY",
    "USO", "GLD", "UUP", "TLT",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "JPM", "BAC", "WFC", "GS", "MS",
    "XOM", "CVX", "COP",
    "BTC", "ETH",
]


def extract_intel(ep: dict, transcript: str,
                  watchlist: Optional[list[str]] = None) -> Optional[dict]:
    """
    Two-stage hybrid extraction:
      Stage 1 (Gemini Flash, cheap): triage — does this transcript have
          market signal for any watchlist ticker? Score 0-10.
      Stage 2 (Claude Opus, expensive): only if triage scored >= 5,
          write the full Conviction Capital layered take.

    Returns intel dict (with triage metadata merged in) or None on failure.
    """
    if not transcript or len(transcript) < 100:
        return None

    out_path = _INTEL_DIR / f"{ep['episode_id']}.json"
    if out_path.exists():
        try:
            return json.loads(out_path.read_text())
        except Exception:
            pass

    # Lazy import to avoid circular deps and to keep this module importable
    # even if llm_router is missing in some environments.
    try:
        from web.llm_router import filter_for_tickers, write_layered_take
    except Exception as e:
        print(f"[podcasts] llm_router unavailable: {e}")
        return None

    wl = watchlist or _DEFAULT_WATCHLIST
    source_label = f"{ep.get('show_name','')} — {ep.get('title','')}"

    # ── Stage 1: Gemini triage ──────────────────────────────────────────────
    triage = filter_for_tickers(transcript, wl, source_label=source_label)
    if not triage:
        print(f"[podcasts] triage failed for {ep['episode_id']}")
        return None

    score = int(triage.get("signal_score", 0) or 0)
    # Below 5 → not worth Claude's compute. Save a stub so the UI can show
    # "we looked at this, nothing material" without re-processing later.
    if score < 5:
        stub = {
            "signal_score":   score,
            "headline":       triage.get("one_liner") or "No material market signal.",
            "summary_why":    "",
            "summary_impact": "",
            "tickers":        triage.get("tickers", []) or [],
            "sectors":        [],
            "themes":         triage.get("themes", []) or [],
            "catalysts":      [],
            "key_quotes":     [],
            "layered_take":   "",
            "watch_list":     [],
            "short_hooks":    [],
            "source":         source_label,
            "stage":          "triage_only",
        }
        out_path.write_text(json.dumps(stub, indent=2))
        return stub

    # ── Stage 2: Claude full writeup ────────────────────────────────────────
    intel = write_layered_take(transcript, triage, source_label=source_label)
    if not intel:
        print(f"[podcasts] claude writeup failed for {ep['episode_id']}")
        # Still save the triage so the UI shows something useful.
        intel = {
            "signal_score":   score,
            "headline":       triage.get("one_liner") or "",
            "tickers":        triage.get("tickers", []) or [],
            "themes":         triage.get("themes", []) or [],
            "stage":          "triage_only",
        }
    else:
        intel["signal_score"] = score
        intel["stage"] = "full"

    intel["source"] = source_label
    out_path.write_text(json.dumps(intel, indent=2))
    return intel


# ── Orchestrator ────────────────────────────────────────────────────────────
def poll_and_process(max_new_per_show: int = 3) -> dict:
    """
    Full pipeline for all subscribed shows.
    Returns {processed: int, errors: int, ready: int}
    """
    idx = _load_index()
    processed, errors = 0, 0

    for show_id in SUBSCRIBED:
        feed = fetch_feed(show_id)
        if not feed:
            continue

        new_eps = [e for e in feed if e["episode_id"] not in idx]
        new_eps = new_eps[:max_new_per_show]

        for ep in new_eps:
            ep_id = ep["episode_id"]
            idx[ep_id] = {**ep, "state": "fetched",
                          "fetched_at": datetime.now(timezone.utc).isoformat()}
            _save_index(idx)

            audio = download_audio(ep)
            if not audio:
                idx[ep_id]["state"] = "error_download"
                errors += 1
                _save_index(idx)
                continue
            idx[ep_id]["state"] = "downloaded"
            _save_index(idx)

            transcript = transcribe(ep, audio)
            # Audio is large — delete unless show is keep_audio
            if not SUBSCRIBED[show_id]["keep_audio"]:
                try:
                    audio.unlink()
                except Exception:
                    pass
            if not transcript:
                idx[ep_id]["state"] = "error_transcribe"
                errors += 1
                _save_index(idx)
                continue
            idx[ep_id]["state"] = "transcribed"
            idx[ep_id]["transcript_len"] = len(transcript)
            _save_index(idx)

            intel = extract_intel(ep, transcript)
            if not intel:
                idx[ep_id]["state"] = "error_intel"
                errors += 1
                _save_index(idx)
                continue
            idx[ep_id]["state"] = "ready"
            idx[ep_id]["intel_summary"] = intel.get("summary_plain", "")[:160]
            _save_index(idx)
            processed += 1

    ready = sum(1 for v in idx.values() if v.get("state") == "ready")
    return {"processed": processed, "errors": errors, "ready": ready}


# ── Read API for the UI ─────────────────────────────────────────────────────
def list_episodes(limit: int = 20) -> list[dict]:
    """
    Return the most-recent processed episodes, newest first, with intel inline.
    """
    idx = _load_index()
    rows = list(idx.values())
    # Sort by pub_date if present, else fetched_at
    def _sort_key(v):
        return v.get("pub_date", "") or v.get("fetched_at", "")
    rows.sort(key=_sort_key, reverse=True)

    out = []
    for v in rows[:limit]:
        ep_id = v.get("episode_id") or ""
        intel = None
        intel_path = _INTEL_DIR / f"{ep_id}.json"
        if intel_path.exists():
            try:
                intel = json.loads(intel_path.read_text())
            except Exception:
                pass
        out.append({
            "episode_id":  ep_id,
            "show_name":   v.get("show_name", ""),
            "title":       v.get("title", ""),
            "pub_date":    v.get("pub_date", ""),
            "state":       v.get("state", ""),
            "intel":       intel,
            "audio_url":   v.get("audio_url", ""),
        })
    return out


# ── CLI for manual runs ─────────────────────────────────────────────────────
if __name__ == "__main__":
    print("[podcasts] polling subscribed shows…")
    result = poll_and_process(max_new_per_show=2)
    print(f"[podcasts] done — {result}")
    for ep in list_episodes(limit=5):
        title = ep["title"][:70]
        state = ep["state"]
        summary = (ep.get("intel") or {}).get("summary_plain", "")[:80]
        print(f"  {state:18} | {title} | {summary}")
