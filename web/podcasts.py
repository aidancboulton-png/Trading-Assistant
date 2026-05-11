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
# Each entry: id → { name, rss_url, kind, keep_audio (bool) }
# "kind" picks the right intelligence-extraction prompt:
#   - "news"   → financial/market news (Bloomberg News Now)
#   - "wisdom" → long-form interviews focused on life/business/health (DOAC)
SUBSCRIBED = {
    "bloomberg_news_now": {
        "name":       "Bloomberg News Now",
        "rss_url":    "https://www.omnycontent.com/d/playlist/e73c998e-6e60-432f-8610-ae210140c5b1/d9566f78-0464-4367-9dcc-b05700aeec6f/7f880b3c-7f67-4b4b-b520-b05700af9172/podcast.rss",
        "kind":       "news",
        "keep_audio": False,
    },
    "diary_of_a_ceo": {
        "name":       "The Diary of a CEO",
        "rss_url":    "https://rss2.flightcast.com/xmsftuzjjykcmqwolaqn6mdn",
        "kind":       "wisdom",
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
# Two prompt variants: news-driven shorts (Bloomberg News Now) and long-form
# wisdom interviews (Diary of a CEO). The wisdom prompt deliberately mirrors
# what DOAC does well — distill lessons, frame them as actionable life
# intelligence, extract emotional/quotable beats. Output schema is identical
# so the UI can render them the same way.

_INTEL_PROMPT_NEWS = """You are the intelligence extractor for Conviction Capital — a platform
that translates financial news into layered intelligence for retail investors.

Read this Bloomberg podcast transcript and return ONLY valid JSON with this shape:

{
  "summary_plain":   "One plain-English sentence: what's the news?",
  "summary_why":     "One sentence: why does this matter?",
  "summary_impact":  "One sentence: what's the most likely market/economic impact?",
  "tickers":         ["TICKER1", "TICKER2"],
  "themes":          ["Fed policy", "geopolitics", "..."],
  "key_quotes": [
    {"speaker": "name or unknown", "quote": "...exact quote..."}
  ],
  "layered_take":    "A 4-5 sentence Conviction Capital style layered explainer: lead with the plain English fact, then the structural why, then who wins/loses, then how it affects a regular person's wallet. No hype. No fear-porn.",
  "short_hooks": [
    "60-90s Short hook line option 1 (hard claim, plain English)",
    "60-90s Short hook line option 2",
    "60-90s Short hook line option 3"
  ],
  "long_form_outline": [
    "Section 1: ...",
    "Section 2: ...",
    "Section 3: ..."
  ],
  "ig_caption":      "Instagram caption — punchy first line, then 2-3 short lines, then 5 hashtags"
}

Rules:
- Output ONLY the JSON object. No markdown fences, no preamble.
- If a field doesn't apply, return an empty list or empty string — never null.
- Tickers must be real (NVDA, AAPL, SPY, etc.) — never invent.
- Quotes must be verbatim from the transcript.
- The layered_take ALWAYS ends with how this affects a regular person's life.

Transcript:
---
{TRANSCRIPT}
---
"""


_INTEL_PROMPT_WISDOM = """You are the intelligence extractor for Conviction Capital — a platform
helping people level up their understanding of money, business, health, and life.

This is a long-form interview podcast (Diary of a CEO style). Your job is to
distill it the way Steven Bartlett's team does: extract the hard-won lessons,
the contrarian beliefs, the actionable mental models, and the emotionally
quotable moments — and present them so a viewer walks away with something
they can DO differently in their own life.

Read the transcript and return ONLY valid JSON with this shape:

{
  "guest":           "Guest name or 'unknown'",
  "guest_credibility": "1-sentence: why should anyone listen to this person?",
  "summary_plain":   "One plain-English sentence: what is this episode actually about?",
  "summary_why":     "One sentence: why does this matter to a regular person right now?",
  "summary_impact":  "One sentence: what's the single most actionable takeaway?",
  "tickers":         [],
  "themes":          ["mental health", "entrepreneurship", "longevity", "..."],
  "key_quotes": [
    {"speaker": "guest or host", "quote": "...verbatim quote, emotionally or intellectually striking..."}
  ],
  "lessons": [
    "Lesson 1: actionable belief or mental model (one sentence each)",
    "Lesson 2: ...",
    "Lesson 3: ...",
    "Lesson 4: ...",
    "Lesson 5: ..."
  ],
  "layered_take":    "A 5-6 sentence Conviction Capital style explainer: lead with the most counterintuitive thing said, then why it's true, then what the audience tends to get wrong about it, then the one concrete change someone could make this week, then how that change compounds over a lifetime. No hype. No empty motivation.",
  "short_hooks": [
    "60-90s Short hook: contrarian one-liner from the episode",
    "60-90s Short hook: striking statistic or claim",
    "60-90s Short hook: emotionally resonant 'you might be doing this wrong' opener"
  ],
  "long_form_outline": [
    "Section 1: the hook — what the guest believes that most people don't",
    "Section 2: the evidence — story or data they cite",
    "Section 3: the application — how a viewer applies this in their life",
    "Section 4: the trap — what most people get wrong when they try this",
    "Section 5: the compound — what this looks like over 1, 5, 10 years"
  ],
  "ig_caption":      "Instagram caption — punchy first line, then 3-4 short lines of takeaway, then 5 hashtags"
}

Rules:
- Output ONLY the JSON object. No markdown fences, no preamble.
- If a field doesn't apply, return an empty list or empty string — never null.
- tickers should be [] unless the guest discusses specific stocks/crypto.
- Quotes MUST be verbatim. Do not paraphrase quotes.
- Lessons must be ACTIONABLE — not platitudes. "Sleep 8 hours" not "sleep is important".
- The layered_take must give the audience something they can DO this week.

Transcript:
---
{TRANSCRIPT}
---
"""


def _prompt_for_kind(kind: str) -> str:
    return _INTEL_PROMPT_WISDOM if kind == "wisdom" else _INTEL_PROMPT_NEWS


def extract_intel(ep: dict, transcript: str) -> Optional[dict]:
    """Run Claude over the transcript. Returns intel dict or None on failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print(f"[podcasts] ANTHROPIC_API_KEY not set — skipping intel extraction")
        return None
    if not transcript or len(transcript) < 100:
        return None

    out_path = _INTEL_DIR / f"{ep['episode_id']}.json"
    if out_path.exists():
        try:
            return json.loads(out_path.read_text())
        except Exception:
            pass

    model = os.environ.get("INTELLIGENCE_MODEL", "claude-opus-4-6")
    show = SUBSCRIBED.get(ep.get("show_id", ""), {})
    kind = show.get("kind", "news")
    prompt_tpl = _prompt_for_kind(kind)
    prompt = prompt_tpl.replace("{TRANSCRIPT}", transcript[:60_000])

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []))
        # Trim to first { … last }
        a, b = text.find("{"), text.rfind("}")
        if a < 0 or b < 0:
            return None
        intel = json.loads(text[a:b+1])
        out_path.write_text(json.dumps(intel, indent=2))
        return intel
    except Exception as e:
        print(f"[podcasts] intel extraction error {ep['episode_id']}: {e}")
        return None


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
