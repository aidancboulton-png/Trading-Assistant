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

YOUR FIRST JOB IS TO FILTER OUT FLUFF.

Before extracting anything, mentally classify every section of the transcript as
SIGNAL or FLUFF. Then ONLY extract from SIGNAL sections.

FLUFF (ignore completely — never include in any output):
- Ad reads / sponsor segments ("This episode brought to you by…")
- Show intros / outros ("Welcome to Bloomberg News Now…")
- Host pleasantries, weather chatter, "thanks for listening"
- Promo CTAs (subscribe, follow, like)
- Repetition where the same fact is restated without new context
- Generic market-color filler ("stocks moved today on light volume")
- Vague macro takes with no specifics ("the economy is uncertain")
- Anything you could find on the front page of any news site

SIGNAL (extract from these):
- Specific facts: dollar amounts, percentages, dates, named parties
- Concrete actions: who did what, what was announced, what was signed
- Causal claims: "X happened because Y" with stated reasoning
- Forward-looking specifics: deadlines, scheduled events, named risks
- Contrarian or non-obvious takes from named analysts/officials
- Real quotes from real people (not the host's transitional patter)

If after filtering there is NOT ENOUGH SIGNAL to fill the schema honestly,
return signal_score ≤ 3 and leave fields with sparse content empty.
DO NOT FABRICATE to fill the schema.

Return ONLY valid JSON with this shape:

{
  "signal_score":    0-10 integer — how much real signal was in the transcript. 0 = pure fluff, 10 = dense actionable intel.
  "fluff_skipped":   "1-2 sentences naming what you filtered out (e.g. '2-min Indeed ad read; 30s intro pleasantries').",
  "summary_plain":   "One plain-English sentence: what's the news? (only from SIGNAL)",
  "summary_why":     "One sentence: why does this matter beyond the headline?",
  "summary_impact":  "One sentence: most likely market/economic impact — with specifics if available.",
  "tickers":         ["TICKER1", "TICKER2"],
  "themes":          ["Fed policy", "geopolitics", ...],
  "key_quotes": [
    {"speaker": "name or unknown", "quote": "...verbatim, non-trivial..."}
  ],
  "layered_take":    "4-5 sentence Conviction Capital layered explainer: plain English fact → structural why → who wins/loses → how this affects a regular person's wallet. No hype, no fear-porn, no generic platitudes.",
  "short_hooks": [
    "Short hook 1 — hard claim with a specific number, name, or date",
    "Short hook 2 — contrarian framing of the same news",
    "Short hook 3 — 'here's what they're not telling you' angle"
  ],
  "long_form_outline": [
    "Section 1: ...", "Section 2: ...", "Section 3: ..."
  ],
  "ig_caption":      "Instagram caption — punchy first line, then 2-3 short lines, then 5 hashtags"
}

Rules:
- Output ONLY the JSON object. No markdown fences, no preamble.
- If a field would only have fluff, return [] or "" — empty is honest, padded is harmful.
- Tickers must be REAL companies/ETFs from the SIGNAL (never invent).
- Quotes must be VERBATIM. If you can't find a non-trivial verbatim quote, return [].
- The layered_take ALWAYS ends with how this affects a regular person's life.
- Short hooks must each contain a specific number, name, date, or claim — never generic ("Here's what happened today" is FLUFF; "Iran just rejected a 12-month nuclear deal — what it means for oil" is SIGNAL).

Transcript:
---
{TRANSCRIPT}
---
"""


_INTEL_PROMPT_WISDOM = """You are the intelligence extractor for Conviction Capital — a platform
helping people level up their understanding of money, business, health, and life.

This is a long-form interview podcast (Diary of a CEO style). These episodes
run 1-3 hours and are MOSTLY FLUFF. Your most important job is to find the
20% that's signal and ignore the rest.

YOUR FIRST JOB IS TO FILTER OUT FLUFF.

FLUFF — IGNORE COMPLETELY (do not extract anything from these sections):
- Sponsor / ad reads ("This episode is brought to you by Huel / Whoop / Shopify…")
- Show intros, outros, "if you enjoyed this please leave a 5-star review"
- Host pleasantries, "thank you so much for being here", "before we start"
- Guest's CV / book promotion / "you can find me on Instagram at…"
- Restating the same belief 5 different ways with no new information
- Personal anecdotes that don't conclude with a transferable principle
- Generic motivational platitudes — "follow your dreams", "believe in yourself",
  "consistency is key", "the journey is the reward"
- Vague advice with no mechanism — "be a better person", "communicate more"
- Filler exchanges — "yeah", "100%", "wow that's interesting", "tell me more"
- Therapist-mode emotional venting with no extractable lesson

SIGNAL — EXTRACT FROM THESE:
- Specific, replicable behaviors with mechanisms ("I cold plunge at 50°F for 3 minutes every morning because…")
- Counterintuitive claims that challenge conventional wisdom and explain why
- Data points, studies, dollar amounts, exact ages, exact timeframes
- A story where the lesson is CRYSTAL CLEAR by the end and the listener can act on it
- Frameworks, decision rules, or mental models the guest names explicitly
- Verbatim quotes that would make someone STOP scrolling — earned through specificity or contrarianism, not theatrics

THE QUALITY BAR:
For EVERY lesson, hook, and quote you include, you must be able to answer YES to BOTH:
  1. "If a 28-year-old read this on Instagram tomorrow, would they screenshot it?"
  2. "Does this name a specific behavior, mechanism, number, or framework?"
If the answer is NO to either, do not include it.

If after filtering there is NOT ENOUGH SIGNAL to fill the schema honestly,
return signal_score ≤ 3 and leave the sparse fields empty. Do NOT pad with
platitudes. Empty is honest. Generic content is harmful to the brand.

Return ONLY valid JSON with this shape:

{
  "signal_score":    0-10 integer — how much real signal was in this episode. 0 = useless, 10 = densely actionable.
  "fluff_skipped":   "1-2 sentences naming the big fluff sections you filtered (e.g. 'Two ad reads totaling ~6 min; 5-min intro pleasantries; long tangent about guest's childhood that didn't conclude with a lesson').",
  "guest":           "Guest name or 'unknown'",
  "guest_credibility": "1 sentence: WHY should anyone listen to this person? Cite their specific expertise/track record (not just job title).",
  "summary_plain":   "One plain-English sentence: what is this episode actually about? (only signal, no setup)",
  "summary_why":     "One sentence: why does this matter to a regular person right now?",
  "summary_impact":  "One sentence: what's the single most actionable takeaway?",
  "tickers":         [],
  "themes":          ["mental health", "entrepreneurship", "longevity", ...],
  "key_quotes": [
    {"speaker": "guest or host", "quote": "...verbatim quote that passes the screenshot test..."}
  ],
  "lessons": [
    "Lesson 1: a specific actionable behavior or mental model with a mechanism. Format: '[Do X] because [Y mechanism] which causes [Z outcome].'",
    "Lesson 2: ...",
    "Lesson 3: ...",
    "Lesson 4: ...",
    "Lesson 5: ..."
  ],
  "layered_take":    "5-6 sentences: lead with the most counterintuitive specific thing said in the episode (not a vague claim) → why it's true with the mechanism → what most people get wrong about it → the ONE specific thing someone could change this week → how that change compounds over 1, 5, 10 years. No hype. No motivation-speak.",
  "short_hooks": [
    "Hook 1 — contrarian one-liner with a specific behavior, number, or mechanism",
    "Hook 2 — striking statistic or named-framework reveal from the episode",
    "Hook 3 — 'you might be doing this wrong' opener that names what 'this' actually is"
  ],
  "long_form_outline": [
    "Section 1: the hook — the specific counterintuitive claim",
    "Section 2: the evidence — the named study, story, or data the guest cited",
    "Section 3: the mechanism — WHY this works (the chain of cause and effect)",
    "Section 4: the application — the literal step-by-step a viewer can run this week",
    "Section 5: the trap — what most people do wrong when they try this and what to do instead",
    "Section 6: the compound — what this looks like over 1, 5, 10 years"
  ],
  "ig_caption":      "Instagram caption — first line is a hard specific claim, then 3-4 short lines of mechanism + takeaway, then 5 hashtags"
}

Rules:
- Output ONLY the JSON object. No markdown fences, no preamble.
- NEVER include platitudes. 'Sleep more', 'be consistent', 'follow your passion' = INSTANT REJECT.
- Every lesson must contain a specific verb + a mechanism. 'Sleep 8 hours' fails. 'Stop eating 3 hours before bed because nighttime glucose spikes fragment REM' passes.
- Quotes MUST be verbatim. If no quote passes the screenshot test, return [].
- Tickers should be [] unless the guest discusses specific stocks/crypto with reasoning.
- The layered_take must end with how the change compounds over time.

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
