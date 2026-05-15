"""
ElevenLabs TTS pipeline for Conviction Capital.

Script → MP3 audio → ready to layer over video for Shorts/Reels/TikTok.

Voice profile: "Conviction Capital" — authoritative, calm, direct.
Default voice: Adam (ElevenLabs built-in, no custom voice needed to start).

Usage:
    audio_path = speak_script(script_text)   # returns path to MP3
    result = generate_script_audio(script)   # takes a script dict, returns {ok, path, duration_s}

Keys needed in config.json:
    "elevenlabs_api_key": "your_key_here"
    "elevenlabs_voice_id": "pNInz6obpgDQGcFmaJgB"  # Adam (default, can change)

Get key: elevenlabs.io → Profile → API Key
Get voice IDs: elevenlabs.io → Voices
"""
from __future__ import annotations
import os, json, time, hashlib
from pathlib import Path
from typing import Optional

import requests

_BASE = Path(__file__).parent.parent

def _cfg() -> dict:
    try:
        return json.load(open(_BASE / "config.json"))
    except Exception:
        return {}

def _key(env: str, cfg_key: str) -> str:
    cfg = _cfg()
    return os.environ.get(env, "").strip() or cfg.get(cfg_key, "")

# Default voice: Adam — clear, authoritative, American English
DEFAULT_VOICE_ID = "pNInz6obpgDQGcFmaJgB"

# Output directory
AUDIO_DIR = _BASE / "web" / "static" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def speak_script(text: str, voice_id: Optional[str] = None) -> Optional[str]:
    """
    Convert text to speech via ElevenLabs.
    Returns path to saved MP3 file, or None on failure.
    """
    api_key = _key("ELEVENLABS_API_KEY", "elevenlabs_api_key")
    if not api_key:
        print("[voice] ELEVENLABS_API_KEY not configured")
        return None

    voice = voice_id or _key("ELEVENLABS_VOICE_ID", "elevenlabs_voice_id") or DEFAULT_VOICE_ID

    # Deduplicate: same text → same file, skip API call
    text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
    out_path = AUDIO_DIR / f"conviction_{text_hash}.mp3"
    if out_path.exists():
        return str(out_path)

    try:
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": "eleven_turbo_v2_5",  # fastest, lowest cost
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                    "style": 0.2,
                    "use_speaker_boost": True,
                },
            },
            timeout=60,
        )
        r.raise_for_status()
        out_path.write_bytes(r.content)
        return str(out_path)
    except Exception as e:
        print(f"[voice] ElevenLabs error: {e}")
        return None


def generate_script_audio(script: dict) -> dict:
    """
    Generate audio for a full content script.
    Combines hook + body + cta into one clean audio file.
    Returns {ok, path, url, duration_estimate_s, error}
    """
    hook = script.get("hook", "")
    body = script.get("body", "")
    cta  = script.get("cta", "Check the link in bio for the full breakdown.")

    # Build the spoken text — slight pauses via punctuation
    spoken = f"{hook}. {body} {cta}"
    # Clean up any markdown or special chars
    spoken = spoken.replace("**", "").replace("*", "").replace("#", "").strip()

    path = speak_script(spoken)
    if not path:
        api_key = _key("ELEVENLABS_API_KEY", "elevenlabs_api_key")
        return {
            "ok": False,
            "error": "ELEVENLABS_API_KEY not configured" if not api_key else "TTS generation failed",
            "path": None,
            "url": None,
        }

    # Estimate duration: average 140 words/min for this voice style
    word_count = len(spoken.split())
    duration_s = round(word_count / 140 * 60)

    # Build URL (served as static file)
    filename = Path(path).name
    url = f"/audio/{filename}"

    return {
        "ok": True,
        "path": path,
        "url": url,
        "duration_estimate_s": duration_s,
        "word_count": word_count,
        "error": None,
    }


def get_available_voices() -> list[dict]:
    """List all voices on the account."""
    api_key = _key("ELEVENLABS_API_KEY", "elevenlabs_api_key")
    if not api_key:
        return []
    try:
        r = requests.get(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": api_key},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("voices", [])
    except Exception:
        return []


def elevenlabs_configured() -> bool:
    return bool(_key("ELEVENLABS_API_KEY", "elevenlabs_api_key"))
