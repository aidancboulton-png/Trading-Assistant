"""
Jarvis Script Writer
Transforms clustered global news into Jarvis-voiced daily briefings.

Voice profile: J.A.R.V.I.S. — Just A Rather Very Intelligent System.
Precise, calm, slightly British, never alarmist. Speaks with total confidence.
Uses clean transitions. Exposes what the mainstream narrative omits.
"""
from __future__ import annotations

import os, json, logging, re
from pathlib import Path
from typing import Optional

import anthropic

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

def _load_anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    cfg_path = Path.home() / "trading-assistant" / "config.json"
    try:
        return json.loads(cfg_path.read_text())["anthropic_api_key"]
    except Exception as e:
        raise RuntimeError(f"No Anthropic API key found: {e}")


# ── Jarvis system prompt ──────────────────────────────────────────────────────

JARVIS_SYSTEM = """You are J.A.R.V.I.S. — the AI briefing system from Iron Man.
Your voice is: calm, precise, slightly British, highly intelligent.
You never sensationalise. You never hedge endlessly. You state what the data shows.
You address the listener as "Sir" or occasionally "Miss" — naturally, not robotically.

Your job today is to write a daily world news briefing script.

RULES:
1. You are NOT a US media outlet. You represent the full picture — all sides.
2. For each story, you MUST include what Western mainstream outlets are saying AND what the rest of the world sees differently.
3. You call out omissions plainly — "What's being left out of the American narrative is..."
4. You never take a political side. You expose the information asymmetry.
5. Plain English. No jargon. No hedge-everything language.
6. End every story with: "What this means for you:" — one direct sentence.
7. Script should feel like a professional intelligence briefing, not a news segment.
8. Use natural Jarvis transitions: "Moving on, Sir.", "The second development worth your attention.", "This one requires context.", etc.
9. Each story: ~150 words. Full briefing: 5 stories max.

FORMAT (use exactly this structure):
[INTRO]
...opening line...

[STORY: {category}]
HEADLINE: {clean one-line headline}
THE STORY: ...2-3 sentence factual summary...
WESTERN VIEW: ...what US/UK/EU media is saying...
COUNTER NARRATIVE: ...what Eastern, Middle Eastern, Latin, or African outlets are reporting differently...
WHAT'S MISSING: ...one sentence on what's being left unsaid...
WHAT THIS MEANS FOR YOU: ...direct personal impact statement...

[OUTRO]
...closing line with today's key theme..."""


JARVIS_VOICE_EXAMPLES = """
Examples of correct Jarvis phrasing:

✓ "Sir, three developments require your attention this morning."
✓ "The Western framing here is incomplete. Here's what the data actually shows."
✓ "What you won't hear on CNN: the economic pressure behind this decision."
✓ "Moving on. This next story has been underreported for a reason."
✓ "To be direct: this is not a military development. This is an economic one."
✓ "What this means for you: energy prices in the US will reflect this within 30 days."

✗ NEVER say: "It's important to note..." / "On the one hand..." / "Some experts believe..."
✗ NEVER hedge. State the picture clearly.
"""


# ── Script generator ──────────────────────────────────────────────────────────

def build_briefing_prompt(stories: list[dict], date_str: str) -> str:
    """Convert clustered stories into a structured prompt."""
    lines = [
        f"Today is {date_str}.",
        f"You have {len(stories)} stories to brief on.",
        "Here is the raw intelligence from across 40 global news outlets:\n",
    ]

    for i, story in enumerate(stories[:5], 1):
        lines.append(f"── STORY {i}: {story['headline']} ──")
        lines.append(f"Category: {story['category']}")
        lines.append(f"Covered by {story['source_count']} outlets from {story['perspective_count']} distinct viewpoints")
        lines.append("")
        for p in story.get("perspectives", []):
            lines.append(f"[{p['label']} — {p['source']}]")
            lines.append(f"Their headline: {p['title']}")
            lines.append(f"Their summary: {p['summary']}")
            lines.append("")
        lines.append("")

    lines.append("Now write the full Jarvis daily briefing script. Use the voice profile. Cover all 5 stories.")
    return "\n".join(lines)


def generate_script(stories: list[dict], date_str: Optional[str] = None) -> dict:
    """
    Call Claude to generate a Jarvis briefing script from clustered stories.
    Returns: { script, word_count, story_headlines, model_used }
    """
    from datetime import date
    if not date_str:
        date_str = date.today().strftime("%A, %B %d %Y")

    key = _load_anthropic_key()
    client = anthropic.Anthropic(api_key=key)

    prompt = build_briefing_prompt(stories, date_str)

    log.info("[scriptwriter] Generating Jarvis script via Claude…")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2500,
        system=JARVIS_SYSTEM + "\n\n" + JARVIS_VOICE_EXAMPLES,
        messages=[{"role": "user", "content": prompt}],
    )

    script = message.content[0].text
    word_count = len(script.split())

    # Extract headlines from [STORY: ...] blocks
    headlines = re.findall(r"HEADLINE:\s*(.+)", script)

    return {
        "script":          script,
        "word_count":      word_count,
        "story_headlines": headlines,
        "model":           message.model,
        "input_tokens":    message.usage.input_tokens,
        "output_tokens":   message.usage.output_tokens,
        "date":            date_str,
    }


# ── Short-form cutter (Shorts / Reels) ────────────────────────────────────────

SHORTS_SYSTEM = """You are J.A.R.V.I.S. cutting a 60-second video script from a longer briefing.

Rules:
- Pick the SINGLE most globally significant story from the briefing.
- 60 seconds = ~150 words spoken at a calm pace.
- Open with a hook line that makes someone stop scrolling.
- Include the counter-narrative angle — that's the differentiator.
- End with the "what this means for you" line.
- Jarvis voice throughout: calm, precise, British, authoritative.
- No hashtags. No emojis. Just the script.

FORMAT:
[HOOK]
...one punchy opening line...
[BRIEF]
...~100 words of the story with counter-narrative...
[CLOSE]
...what this means for you..."""


def generate_short(full_script: str) -> dict:
    """Cut a 60-second Shorts/Reels script from the full briefing."""
    key = _load_anthropic_key()
    client = anthropic.Anthropic(api_key=key)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=SHORTS_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Here is today's full briefing:\n\n{full_script}\n\nNow cut the 60-second short.",
        }],
    )

    return {
        "short_script": message.content[0].text,
        "word_count":   len(message.content[0].text.split()),
    }


# ── TTS prep (ElevenLabs-compatible) ─────────────────────────────────────────

ELEVENLABS_VOICE_NOTES = """
Recommended ElevenLabs settings for Jarvis voice:
- Voice: "Daniel" or "George" (British male)
- Model: eleven_multilingual_v2
- Stability: 0.55
- Similarity Boost: 0.80
- Style: 0.20
- Speaking Rate: 0.95 (slightly slower than default = more authoritative)

To use: pip install elevenlabs
Then call: tts_export(script, api_key, output_path)
"""

def tts_export(script: str, elevenlabs_key: str, output_path: str = "briefing.mp3",
               voice_id: str = "onwK4e9ZLuTAKqWW03F9") -> bool:
    """
    Export script to audio via ElevenLabs.
    voice_id default = 'Daniel' (British male, authoritative).
    Returns True on success.
    """
    try:
        import requests as req
        clean = re.sub(r"\[.*?\]", "", script)          # remove stage markers
        clean = re.sub(r"\n{3,}", "\n\n", clean).strip()

        r = req.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": elevenlabs_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": clean,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.55,
                    "similarity_boost": 0.80,
                    "style": 0.20,
                    "use_speaker_boost": True,
                },
            },
            timeout=60,
        )
        if r.status_code == 200:
            Path(output_path).write_bytes(r.content)
            log.info("[tts] Saved to %s (%d KB)", output_path, len(r.content) // 1024)
            return True
        else:
            log.error("[tts] ElevenLabs error %d: %s", r.status_code, r.text[:200])
            return False
    except Exception as e:
        log.error("[tts] Export failed: %s", e)
        return False


# ── Full pipeline convenience function ────────────────────────────────────────

def run_full_pipeline(stories: list[dict], elevenlabs_key: Optional[str] = None,
                      output_dir: str = ".") -> dict:
    """
    Run full pipeline: script → short → optional TTS.
    Returns all outputs.
    """
    from datetime import date
    date_str = date.today().strftime("%A, %B %d %Y")

    print("[scriptwriter] Generating full Jarvis briefing…")
    full = generate_script(stories, date_str)

    print("[scriptwriter] Cutting short-form version…")
    short = generate_short(full["script"])

    result = {**full, **short}

    if elevenlabs_key:
        mp3_path = f"{output_dir}/jarvis_briefing_{date.today().isoformat()}.mp3"
        print(f"[scriptwriter] Generating TTS audio → {mp3_path}")
        result["audio_exported"] = tts_export(full["script"], elevenlabs_key, mp3_path)
        result["audio_path"] = mp3_path if result["audio_exported"] else None
    else:
        result["audio_exported"] = False
        result["audio_path"] = None

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from jarvis.newsengine import aggregate

    print("[pipeline] Running full Jarvis daily brief pipeline…\n")
    news = aggregate(translate=True)
    stories = news["stories"]
    result = run_full_pipeline(stories)

    print("\n" + "═" * 80)
    print(f"  JARVIS DAILY BRIEFING  |  {result['date']}")
    print("═" * 80)
    print(result["script"])
    print("\n" + "─" * 80)
    print("  60-SECOND SHORT:")
    print("─" * 80)
    print(result["short_script"])
    print(f"\n[stats] Full: {result['word_count']} words | Short: {result['short_word_count']} words")
    print(f"[stats] Tokens used: {result.get('input_tokens',0)} in / {result.get('output_tokens',0)} out")
