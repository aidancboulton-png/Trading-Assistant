"""
Conviction Capital — AGI Orchestrator

The autonomous loop that runs the entire system without human intervention.

Every cycle (default 15 min):
  1. PERCEIVE    — pull fresh prices, news, fear/greed
  2. PRIORITIZE  — rank every signal HIGH/MEDIUM/NOISE
  3. DETECT      — identify market regime
  4. REASON      — Claude synthesizes WHY, generates the brief
  5. CREATE      — Gemini writes scripts if HIGH signals present
  6. DISTRIBUTE  — post to X; YouTube/TikTok when video pipeline ready
  7. REMEMBER    — log everything, track what fired

The orchestrator exposes its full state via /api/agi/status so the
war room can show what the system is doing in real time.
"""
from __future__ import annotations
import asyncio, time, json, hashlib
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

from web.signal_priority import (
    classify_batch, get_high_signals, detect_regime, enrich_signal,
    PRIORITY_HIGH, PRIORITY_MEDIUM, Signal,
)
from web.llm_router import claude_call, gemini_call, gemini_json

# ── State ─────────────────────────────────────────────────────────────────────

@dataclass
class CycleResult:
    cycle_id: int
    ts: float
    duration_ms: int
    regime: str
    regime_confidence: int
    high_signal_count: int
    top_signal: str
    brief_headline: str
    brief_body: str
    scripts_generated: int
    posted_to_x: bool
    x_url: str
    errors: list = field(default_factory=list)


@dataclass
class AGIState:
    running: bool = False
    cycle_count: int = 0
    last_cycle_ts: float = 0
    next_cycle_ts: float = 0
    current_phase: str = "idle"   # perceive|prioritize|detect|reason|create|distribute|idle
    regime: str = "UNKNOWN"
    regime_confidence: int = 0
    regime_key_signals: list = field(default_factory=list)
    high_signals_today: int = 0
    briefs_generated: int = 0
    scripts_generated: int = 0
    posts_sent: int = 0
    last_brief: str = ""
    last_top_signal: str = ""
    recent_cycles: list = field(default_factory=list)  # last 10 CycleResults
    errors_today: int = 0


_STATE = AGIState()
_SEEN_SIGNAL_IDS: set = set()
_HIGH_SIGNAL_QUEUE: list = []   # signals waiting to be enriched + distributed


def get_state() -> dict:
    d = asdict(_STATE)
    d["recent_cycles"] = d["recent_cycles"][-10:]
    return d


# ── Main orchestrator loop ────────────────────────────────────────────────────

async def run_agi_loop(
    get_snap_fn,
    get_news_fn,
    get_fg_fn,
    get_analysis_fn,
    scripts_cache: dict,
    jarvis_briefs: list,
    interval_s: int = 900,
):
    """
    Main async loop. Call once from app startup.
    Imports kept local to avoid circular imports.
    """
    _STATE.running = True
    await asyncio.sleep(45)  # let other systems warm up first

    while True:
        cycle_start = time.time()
        _STATE.current_phase = "perceive"
        _STATE.cycle_count += 1
        cycle_id = _STATE.cycle_count
        errors = []

        try:
            # ── 1. PERCEIVE ──────────────────────────────────────────────────
            snap    = get_snap_fn()   or {}
            news    = get_news_fn()   or []
            fg_data = get_fg_fn()     or {}
            fear_greed = int(fg_data.get("value", 50) if isinstance(fg_data, dict) else 50)
            analysis = get_analysis_fn() or {}

            # ── 2. PRIORITIZE ────────────────────────────────────────────────
            _STATE.current_phase = "prioritize"
            all_signals = classify_batch(news)
            new_high = []
            for sig in all_signals:
                if sig.id not in _SEEN_SIGNAL_IDS:
                    _SEEN_SIGNAL_IDS.add(sig.id)
                    if sig.priority == PRIORITY_HIGH:
                        new_high.append(sig)
                        _STATE.high_signals_today += 1

            top_signal = all_signals[0] if all_signals else None
            _STATE.last_top_signal = top_signal.headline if top_signal else ""

            # ── 3. DETECT REGIME ─────────────────────────────────────────────
            _STATE.current_phase = "detect"
            regime_data = detect_regime(snap, analysis, fear_greed)
            _STATE.regime = regime_data["regime"]
            _STATE.regime_confidence = regime_data["confidence"]
            _STATE.regime_key_signals = regime_data["key_signals"]

            # ── 4. REASON — generate brief ───────────────────────────────────
            _STATE.current_phase = "reason"
            mood        = analysis.get("mood", "")
            summary     = analysis.get("simple_summary", "")
            signals_txt = analysis.get("signals", [])
            top_news    = [s.headline for s in all_signals[:12]]

            prices = {}
            for sym in ["ES", "BTC", "VIX", "NVDA", "GC", "DXY"]:
                if sym in snap:
                    p = snap[sym]
                    c = p.get("current") or p.get("price", 0)
                    chg = p.get("change_pct") or p.get("chg_pct", 0)
                    if c:
                        prices[sym] = f"{c:,.2f} ({chg:+.2f}%)"

            high_headlines = "\n".join(f"‼ {s.headline}" for s in new_high[:5])
            signal_titles  = "\n".join(f"- {s.get('title','')}" for s in signals_txt[:4] if isinstance(s, dict))

            reason_prompt = (
                f"You are the Conviction Capital intelligence engine.\n"
                f"Regime: {_STATE.regime} ({_STATE.regime_confidence}% confidence)\n"
                f"Market: {mood}. {summary}\n"
                f"Prices: {json.dumps(prices)}\n"
                f"Key signals:\n{signal_titles}\n"
                + (f"Breaking signals:\n{high_headlines}\n" if high_headlines else "")
                + f"Top headlines:\n" + "\n".join(f"- {h}" for h in top_news[:10]) + "\n\n"
                "Write a 3-paragraph market brief in the Conviction Capital voice:\n"
                "Para 1: What is the regime and WHY — name specific mechanisms, not events.\n"
                "Para 2: What is the single most important signal RIGHT NOW and what does it mean for the next 4 hours?\n"
                "Para 3: What should a serious trader be watching and why — one specific thing, actionable.\n"
                "No fluff. No hedging. No 'could potentially'. Direct causal reasoning only."
            )

            brief_raw = await asyncio.to_thread(
                claude_call, reason_prompt, max_tokens=600
            ) or await asyncio.to_thread(
                gemini_call, reason_prompt, None, 600, False, "agi:reason"
            )

            brief_headline = ""
            brief_body = ""
            if brief_raw:
                lines = brief_raw.strip().split("\n")
                brief_headline = lines[0].strip()
                brief_body = "\n".join(lines[1:]).strip()
                _STATE.last_brief = brief_headline
                _STATE.briefs_generated += 1

                # Inject into jarvis_briefs list (shared with /api/jarvis/briefs)
                jarvis_briefs.insert(0, {
                    "id": int(cycle_start),
                    "task_type": "agi_cycle",
                    "label": f"AGI Cycle #{cycle_id} · {_STATE.regime}",
                    "ts": cycle_start,
                    "elapsed_ms": 0,
                    "headline": brief_headline,
                    "body": brief_body,
                    "mood": mood,
                    "prices": prices,
                    "regime": _STATE.regime,
                    "high_signals": len(new_high),
                })
                if len(jarvis_briefs) > 30:
                    jarvis_briefs.pop()

            # ── 5. CREATE — scripts if HIGH signals ──────────────────────────
            _STATE.current_phase = "create"
            scripts_generated = 0

            if new_high or (cycle_id % 3 == 0):  # Every 3rd cycle always generates scripts
                script_prompt = (
                    f"You write 30-second vertical-video scripts for Conviction Capital.\n"
                    f"Today's regime: {_STATE.regime}. Mood: {mood}.\n"
                    f"Top story: {brief_headline}\n"
                    f"Breaking signals: {', '.join(s.headline[:60] for s in new_high[:3]) or 'none'}\n\n"
                    "Generate TWO scripts on the most important market story right now.\n"
                    "Each must have: a specific number, a named ticker, a real catalyst.\n"
                    "No generic market commentary. Real edge only.\n\n"
                    'Output ONLY valid JSON: {"scripts": [{"topic":"","hook":"","body":"","cta":"","tickers":[],"caption":"","hashtags":[]}]}'
                )
                script_result = await asyncio.to_thread(
                    gemini_json, script_prompt, None, 2000, "agi:scripts"
                )
                if script_result and script_result.get("scripts"):
                    scripts = script_result["scripts"]
                    scripts_cache["payload"] = {"scripts": scripts, "ts": cycle_start}
                    scripts_cache["ts"] = cycle_start
                    scripts_generated = len(scripts)
                    _STATE.scripts_generated += scripts_generated

            # ── 6. DISTRIBUTE ─────────────────────────────────────────────────
            _STATE.current_phase = "distribute"
            posted_to_x = False
            x_url = ""

            if brief_headline and brief_body:
                try:
                    from web.social import post_brief_to_x
                    brief_obj = {
                        "headline": brief_headline,
                        "body": brief_body[:400],
                        "label": f"Regime: {_STATE.regime}",
                    }
                    result = await asyncio.to_thread(post_brief_to_x, brief_obj)
                    if result.get("ok"):
                        posted_to_x = True
                        x_url = result.get("url", "")
                        _STATE.posts_sent += 1
                except Exception as xe:
                    # Only log if it's a real error, not "keys not configured"
                    if "not configured" not in str(xe):
                        errors.append(f"X post: {xe}")

        except Exception as e:
            errors.append(str(e))
            _STATE.errors_today += 1
            print(f"[agi] cycle {cycle_id} error: {e}")

        finally:
            duration_ms = int((time.time() - cycle_start) * 1000)
            _STATE.current_phase = "idle"
            _STATE.last_cycle_ts = cycle_start
            _STATE.next_cycle_ts = cycle_start + interval_s

            cycle_result = CycleResult(
                cycle_id=cycle_id,
                ts=cycle_start,
                duration_ms=duration_ms,
                regime=_STATE.regime,
                regime_confidence=_STATE.regime_confidence,
                high_signal_count=len(new_high) if 'new_high' in dir() else 0,
                top_signal=_STATE.last_top_signal,
                brief_headline=_STATE.last_brief,
                brief_body=brief_body if 'brief_body' in dir() else "",
                scripts_generated=scripts_generated if 'scripts_generated' in dir() else 0,
                posted_to_x=posted_to_x if 'posted_to_x' in dir() else False,
                x_url=x_url if 'x_url' in dir() else "",
                errors=errors,
            )
            _STATE.recent_cycles.append(asdict(cycle_result))
            if len(_STATE.recent_cycles) > 20:
                _STATE.recent_cycles.pop(0)

            print(f"[agi] cycle {cycle_id} complete — {duration_ms}ms | regime={_STATE.regime} | high={cycle_result.high_signal_count} | brief={'yes' if _STATE.last_brief else 'no'}")

        await asyncio.sleep(interval_s)
