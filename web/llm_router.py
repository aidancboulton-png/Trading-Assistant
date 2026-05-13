"""
LLM Router — hybrid Claude + Gemini pipeline.

Division of labor:
  - Gemini Flash → bulk scanning, long-context (full transcripts, large dumps),
    web grounding, ticker-match filtering. Cheap and fast.
  - Claude Opus  → final Conviction Capital house-voice writeups, the layered
    take a paying user reads. Brand voice, structural reasoning.

Both helpers NEVER raise. They return None on failure so callers can degrade
gracefully (skip the item, don't crash the dashboard).
"""
import os
import json
import re
import time
from collections import deque
from typing import Optional

import requests

UA = {"User-Agent": "conviction-capital/1.0"}

GEMINI_FAST_MODEL = os.environ.get("GEMINI_FAST_MODEL", "gemini-2.5-flash")
GEMINI_PRO_MODEL  = os.environ.get("GEMINI_PRO_MODEL",  "gemini-2.5-pro")
CLAUDE_MODEL      = os.environ.get("INTELLIGENCE_MODEL", "claude-opus-4-6")


# ── Telemetry (powers the Mainframe dashboard) ─────────────────────────────
# Ring-buffer of recent LLM calls + running counters. In-memory; resets on
# server restart. The Mainframe UI polls /api/mainframe to render this live.
_CALL_LOG: deque = deque(maxlen=200)
_STATS: dict = {
    "gemini_calls":   0,
    "gemini_errors":  0,
    "gemini_tokens":  0,
    "claude_calls":   0,
    "claude_errors":  0,
    "claude_tokens":  0,
    "started_at":     time.time(),
}


def _record(provider: str, model: str, label: str,
            tokens_in: int, tokens_out: int, ok: bool,
            error: str = "", elapsed_ms: int = 0,
            preview: str = "") -> None:
    """Push one event into the telemetry ring + bump counters."""
    _CALL_LOG.appendleft({
        "ts":         time.time(),
        "provider":   provider,
        "model":      model,
        "label":      label,
        "tokens_in":  tokens_in,
        "tokens_out": tokens_out,
        "ok":         ok,
        "error":      error[:200],
        "elapsed_ms": elapsed_ms,
        "preview":    preview[:240],
    })
    key = provider
    _STATS[f"{key}_calls"] = _STATS.get(f"{key}_calls", 0) + 1
    if not ok:
        _STATS[f"{key}_errors"] = _STATS.get(f"{key}_errors", 0) + 1
    _STATS[f"{key}_tokens"] = _STATS.get(f"{key}_tokens", 0) + tokens_in + tokens_out


def telemetry() -> dict:
    """Snapshot for the Mainframe UI."""
    return {
        "stats": dict(_STATS),
        "recent": list(_CALL_LOG)[:60],
        "uptime_s": int(time.time() - _STATS["started_at"]),
    }


# ── Gemini ──────────────────────────────────────────────────────────────────
def gemini_call(prompt: str, model: Optional[str] = None,
                max_tokens: int = 2048, grounding: bool = False,
                label: str = "gemini") -> Optional[str]:
    """
    Low-level Gemini call. Returns raw text or None.
    Set grounding=True to enable Google Search grounding (live web facts).
    `label` shows up in the Mainframe telemetry feed (e.g. 'movers:score').
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        _record("gemini", model or GEMINI_FAST_MODEL, label, 0, 0,
                ok=False, error="GEMINI_API_KEY not set")
        return None
    model = model or GEMINI_FAST_MODEL
    t0 = time.time()

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body: dict = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.3,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if grounding:
        body["tools"] = [{"google_search": {}}]

    try:
        r = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "X-goog-api-key": api_key,
            },
            json=body,
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip() or None
        usage = data.get("usageMetadata", {}) or {}
        _record("gemini", model, label,
                tokens_in=int(usage.get("promptTokenCount", 0) or 0),
                tokens_out=int(usage.get("candidatesTokenCount", 0) or 0),
                ok=bool(text),
                elapsed_ms=int((time.time() - t0) * 1000),
                preview=(text or "")[:200])
        return text
    except Exception as e:
        _record("gemini", model, label, 0, 0, ok=False, error=str(e),
                elapsed_ms=int((time.time() - t0) * 1000))
        print(f"[llm_router] gemini error: {e}")
        return None


def gemini_json(prompt: str, model: Optional[str] = None,
                max_tokens: int = 2048, label: str = "gemini:json") -> Optional[dict]:
    """Gemini call that returns parsed JSON (trims to first { … last })."""
    text = gemini_call(prompt, model=model, max_tokens=max_tokens, label=label)
    if not text:
        return None
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        return None
    try:
        return json.loads(text[a:b+1])
    except Exception:
        return None


# ── Claude (with Gemini Pro fallback for free-mode) ────────────────────────
def claude_call(prompt: str, model: Optional[str] = None,
                max_tokens: int = 2000) -> Optional[str]:
    """
    Low-level Claude call. Returns raw text or None.
    If ANTHROPIC_API_KEY is missing, transparently falls back to Gemini Pro
    so the platform stays fully functional in free-tier mode.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        # Free-mode: route to Gemini Flash (Pro has stricter free-tier rate limits).
        # When Anthropic key is added later, this branch is skipped automatically.
        return gemini_call(prompt, model=GEMINI_FAST_MODEL, max_tokens=max_tokens)
    model = model or CLAUDE_MODEL

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
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        return "".join(b.get("text", "") for b in data.get("content", [])).strip() or None
    except Exception as e:
        print(f"[llm_router] claude error: {e}")
        return None


def claude_json(prompt: str, model: Optional[str] = None,
                max_tokens: int = 2000) -> Optional[dict]:
    """Claude call that returns parsed JSON."""
    text = claude_call(prompt, model=model, max_tokens=max_tokens)
    if not text:
        return None
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        return None
    try:
        return json.loads(text[a:b+1])
    except Exception:
        return None


# ── High-level helpers ──────────────────────────────────────────────────────
def filter_for_tickers(content: str, watchlist: list[str],
                       source_label: str = "") -> Optional[dict]:
    """
    First-pass triage. Uses Gemini (cheap, long context) to decide:
      - Does this content mention or materially affect any watchlist ticker?
      - What's the signal_score?
      - Which tickers are involved?
    Returns dict like:
      { "signal_score": 0-10, "tickers": [...], "themes": [...],
        "one_liner": "..." }
    or None if Gemini can't be reached.
    """
    tickers_str = ", ".join(watchlist)
    prompt = f"""You triage content for Conviction Capital, a paid market-intel platform.

Watchlist: {tickers_str}

Source: {source_label}

Task: Read the content. Decide whether it contains material market signal for ANY ticker on the watchlist (directly mentioned OR materially affected via sector/macro/correlation).

Output ONLY this JSON:

{{
  "signal_score": 0-10 integer (0 = no market signal, 10 = high-conviction actionable),
  "tickers": ["TICKER1", ...],         // ONLY watchlist tickers actually relevant
  "themes": ["Fed policy", "AI capex", ...],
  "one_liner": "ONE sentence: what is the market-relevant takeaway? Empty string if no signal."
}}

Rules:
- Be ruthless. If it's life advice, motivation, or off-topic, return signal_score 0.
- Only include tickers from the watchlist. Don't invent tickers.
- one_liner must be specific (named ticker / number / event) or empty.

Content:
---
{content[:80_000]}
---
"""
    return gemini_json(prompt, max_tokens=600)


def write_layered_take(content: str, triage: dict, source_label: str = "") -> Optional[dict]:
    """
    Second-pass writeup. Uses Claude (brand voice) to produce the layered
    Conviction Capital intel card. Only call this when triage['signal_score'] >= 5.

    Returns structured intel dict, same shape as the existing podcast intel.
    """
    tickers = triage.get("tickers", []) or []
    themes  = triage.get("themes", []) or []

    prompt = f"""You are the intelligence writer for Conviction Capital.

Source: {source_label}
Relevant tickers (pre-filtered): {", ".join(tickers) if tickers else "(none)"}
Themes (pre-filtered): {", ".join(themes) if themes else "(none)"}
Triage one-liner: {triage.get("one_liner", "")}

Write a layered intel card. Output ONLY this JSON:

{{
  "headline":       "One-line plain-English statement of the market-relevant thing.",
  "summary_why":    "One sentence: why a trader/investor should care.",
  "summary_impact": "One sentence: most likely impact — with sectors, tickers, or levels.",
  "tickers":        {json.dumps(tickers)},
  "sectors":        ["Energy", ...],
  "themes":         {json.dumps(themes)},
  "catalysts":      [{{"date": "YYYY-MM-DD or 'unknown'", "event": "...", "matters_because": "..."}}],
  "key_quotes":     [{{"speaker": "name", "quote": "verbatim..."}}],
  "layered_take":   "4-6 sentences, Conviction Capital house voice: plain fact -> structural why -> who wins/loses (named) -> how this shows up in a regular portfolio.",
  "watch_list":     ["specific ticker / level / date / spread to monitor"],
  "short_hooks":    ["hook 1 with specific number/name", "hook 2 contrarian", "hook 3 what they're not telling you"]
}}

Rules:
- Output JSON only. No markdown fences.
- Empty arrays/strings are FINE — never pad with platitudes.
- Quotes must be verbatim from the content. If none qualify, return [].
- Every short hook must contain a number, ticker, name, or date.

Content:
---
{content[:30_000]}
---
"""
    return claude_json(prompt, max_tokens=1500)


def ground_ticker(ticker: str) -> Optional[str]:
    """
    Live web grounding for a single ticker. Uses Gemini with Google Search.
    Returns a one-paragraph 'what's happening with $TICKER right now' summary.
    """
    prompt = (f"What is the most important market-relevant news about ${ticker} "
              f"in the last 24 hours? Reply in 2-3 sentences with specifics "
              f"(price moves, named catalysts, dates, dollar amounts). "
              f"If nothing material happened, say so.")
    return gemini_call(prompt, grounding=True, max_tokens=400)


# ── Diagnostics ─────────────────────────────────────────────────────────────
def health() -> dict:
    """Quick health check for both providers."""
    gem_ok = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    cla_ok = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return {
        "gemini": {"key_set": gem_ok, "model": GEMINI_FAST_MODEL},
        "claude": {"key_set": cla_ok, "model": CLAUDE_MODEL},
    }


if __name__ == "__main__":
    print("[llm_router] health:", health())
    print("[llm_router] gemini ping:", gemini_call("Say 'pong' and nothing else."))
