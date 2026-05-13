"""
Movers Engine — the convergence intelligence layer.

Finds the "Next NVDA"-class candidates across 7 lenses (Energy, BTC Mining,
Agriculture, Land/Real-Estate, Infrastructure, Defense, Convergence) by
scoring tickers across 12 vectors that matter for the AGI + crypto +
reindustrialization decade.

GEMINI does the heavy lifting (universe discovery, vector scoring, smart-money
sweeps, catalyst calendar). CLAUDE is only invoked when a user clicks a
ticker for the layered thesis writeup — and only if cached output is stale.

Everything is cached on disk. Background bake runs every 6 hours and the
HTTP layer reads pre-baked JSON in <100ms. NEVER raises — degrades gracefully.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Lazy LLM imports — keep this module importable even if llm_router fails to load.
def _llm():
    from web import llm_router
    return llm_router


# ── Storage ────────────────────────────────────────────────────────────────
_BASE = Path(__file__).parent.parent
_DATA = _BASE / "data" / "movers"
_RANKS_DIR = _DATA / "ranks"           # one JSON per sector — pre-baked
_THESIS_DIR = _DATA / "thesis"         # one JSON per ticker — Claude writeups
_SMART_DIR = _DATA / "smartmoney"      # one JSON per operator
_META = _DATA / "meta.json"            # last-bake timestamps

for d in (_RANKS_DIR, _THESIS_DIR, _SMART_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ── Sector definitions ─────────────────────────────────────────────────────
SECTORS = {
    "energy": {
        "label": "Energy",
        "emoji": "⚡",
        "discovery_prompt": (
            "Top 30 publicly-traded US/EU companies in oil, natural gas, "
            "uranium, utilities, and traditional energy ranked by 12-month "
            "structural momentum (production growth, reserve replacement, "
            "regulatory tailwind, smart-money buying)."
        ),
    },
    "btc_mining": {
        "label": "BTC Mining",
        "emoji": "⛏️",
        "discovery_prompt": (
            "Top 20 publicly-traded Bitcoin mining companies ranked by hash "
            "rate growth, owned power capacity (MW), low-cost electricity "
            "contracts, AI/HPC pivot optionality, and balance sheet strength."
        ),
    },
    "agriculture": {
        "label": "Agriculture",
        "emoji": "🌾",
        "discovery_prompt": (
            "Top 25 publicly-traded ag-related companies covering fertilizer, "
            "seed/crop protection, equipment, processors, water rights, "
            "farmland REITs, and ag-tech, ranked by structural momentum and "
            "smart-money positioning (Bill Gates / Cascade farmland exposure, "
            "MacKenzie Scott donations, foreign ag-land buys)."
        ),
    },
    "land": {
        "label": "Land / Real Estate",
        "emoji": "🏞️",
        "discovery_prompt": (
            "Top 25 publicly-traded companies with concentrated land, "
            "real-estate, or water-rights exposure ranked by data-center / "
            "AI-infrastructure optionality, smart-money buying (Gates, Malone, "
            "Kroenke, Bezos, Ackman, Wilks Bros), and proximity to demand "
            "corridors. Include REITs, homebuilders, land-holders (TPL, JOE, "
            "HHH, WY, RYN), farmland REITs (FPI, LAND), and data-center REITs."
        ),
    },
    "infrastructure": {
        "label": "Infrastructure",
        "emoji": "🔋",
        "discovery_prompt": (
            "Top 25 publicly-traded companies providing the picks-and-shovels "
            "for the AGI buildout: grid/transmission, nuclear (incl. SMR), "
            "hyperscaler datacenter REITs, fiber/connectivity, power "
            "generation with already-permitted GW capacity, and water "
            "infrastructure. Ranked by catalyst density and structural moat."
        ),
    },
    "defense": {
        "label": "Defense",
        "emoji": "🛡️",
        "discovery_prompt": (
            "Top 20 publicly-traded defense and dual-use companies (primes, "
            "drone makers, hypersonics, AI/military intersection, space) "
            "ranked by contract win momentum, congressional support, "
            "structural geopolitical tailwind, and insider/institutional buying."
        ),
    },
    "convergence": {
        "label": "Convergence",
        "emoji": "🔗",
        "discovery_prompt": (
            "Top 30 publicly-traded companies that sit at the intersection "
            "of 3+ of the following physical bottleneck vectors: LAND, POWER, "
            "WATER, GRID-RIGHTS, CRITICAL-MINERALS, DATA-CENTER-PROXIMITY, "
            "AGRI-LAND, AI-PIVOT-OPTIONALITY. These are companies the market "
            "has NOT yet fully priced as AI-infrastructure plays but which "
            "structurally are. Examples to consider but not be limited by: "
            "CIFR, IREN, APLD, WULF, TPL, JOE, HHH, CEG, VST, TLN, FPI, LAND, "
            "BAM, BEP, PCYO, CDZI."
        ),
    },
}


# The 12 vectors of the new economy. Used in scoring + UI display.
VECTORS = [
    ("water",        "Water rights / reclaimed capacity"),
    ("power",        "Power generation / contracted MW"),
    ("land",         "Land / real-estate holdings"),
    ("grid_rights",  "Grid interconnect / permitted GW"),
    ("ai_compute",   "AI compute / chips / racks"),
    ("data_moat",    "Proprietary data / defensible dataset"),
    ("connectivity", "Fiber / undersea / edge"),
    ("minerals",     "Critical minerals (Li, Cu, REE, U)"),
    ("reshoring",    "Industrial automation / reshoring"),
    ("defense",      "Defense / dual-use"),
    ("policy",       "Policy / regulatory arbitrage"),
    ("smart_money",  "Smart-money / influence-network buying"),
]


# Smart-money operators we track. Used by smartmoney_sweep().
SMART_MONEY = [
    "Warren Buffett (Berkshire)",
    "Stanley Druckenmiller (Duquesne)",
    "Ray Dalio (Bridgewater)",
    "Paul Tudor Jones",
    "David Tepper (Appaloosa)",
    "Howard Marks (Oaktree)",
    "Ken Griffin (Citadel)",
    "Bill Ackman (Pershing Square)",
    "Michael Burry (Scion)",
    "Chamath Palihapitiya (Social Capital)",
    "Leopold Aschenbrenner (Situational Awareness)",
    "Cathie Wood (ARK)",
    "Peter Thiel (Founders Fund)",
    "BlackRock 13F top adds",
    "Vanguard 13F top adds",
    "Bill Gates / Cascade Investments (farmland LLCs)",
    "MacKenzie Scott (donation flows)",
    "John Malone (Liberty)",
    "Stan Kroenke (Kroenke Holdings)",
    "Wilks Brothers (Permian land)",
    "Nancy Pelosi (congressional trades)",
    "Dan Crenshaw / Tommy Tuberville (congressional trades)",
]


# ── Universe discovery (Gemini grounded) ───────────────────────────────────
def _discover_tickers(sector_id: str) -> list[str]:
    """Ask Gemini (with web grounding) for the current top tickers in a sector."""
    sector = SECTORS.get(sector_id)
    if not sector:
        return []
    prompt = (
        f"{sector['discovery_prompt']}\n\n"
        "Output ONLY a JSON array of ticker symbols, no commentary. "
        'Example: ["NVDA","CIFR","TPL"]'
    )
    llm = _llm()
    raw = llm.gemini_call(prompt, grounding=True, max_tokens=800)
    if not raw:
        return []
    # Trim to first [ … last ]
    a, b = raw.find("["), raw.rfind("]")
    if a < 0 or b < 0:
        return []
    try:
        arr = json.loads(raw[a:b+1])
        return [str(t).upper().strip() for t in arr if isinstance(t, str)][:35]
    except Exception:
        return []


# ── Vector scoring (Gemini, batched) ───────────────────────────────────────
def _score_tickers(tickers: list[str], sector_id: str) -> list[dict]:
    """
    Batched: ask Gemini to score N tickers across the 12 vectors at once.
    Returns list of dicts: { ticker, scores: {vector: 0-5}, total, one_liner }
    """
    if not tickers:
        return []

    vectors_list = "\n".join(f"  - {k}: {label}" for k, label in VECTORS)
    tickers_csv = ", ".join(tickers)

    prompt = f"""You are scoring stocks for Conviction Capital's Convergence engine.

Score each ticker on the 12 vectors below. Each vector: 0 = no exposure, 5 = dominant exposure.

Vectors:
{vectors_list}

Tickers to score (sector = {sector_id}): {tickers_csv}

For each ticker output a JSON object with this shape:
{{
  "ticker": "XYZ",
  "scores": {{
    "water": 0-5, "power": 0-5, "land": 0-5, "grid_rights": 0-5,
    "ai_compute": 0-5, "data_moat": 0-5, "connectivity": 0-5,
    "minerals": 0-5, "reshoring": 0-5, "defense": 0-5,
    "policy": 0-5, "smart_money": 0-5
  }},
  "one_liner": "1 sentence: WHY this scores where it does — name the specific asset/contract/holding.",
  "catalyst_30d": "Named catalyst in the next 30-60 days or empty string."
}}

Output ONLY a JSON array of such objects. No markdown fences. No commentary.
Be honest — most tickers score 0 on most vectors. Only mark high scores when you have a specific verifiable reason."""

    llm = _llm()
    # Use grounding so scoring reflects current contracts/holdings, not stale training data.
    raw = llm.gemini_call(prompt, grounding=True, max_tokens=4096)
    if not raw:
        return []

    a, b = raw.find("["), raw.rfind("]")
    if a < 0 or b < 0:
        return []
    try:
        arr = json.loads(raw[a:b+1])
    except Exception:
        return []

    out: list[dict] = []
    for row in arr:
        if not isinstance(row, dict):
            continue
        tk = str(row.get("ticker", "")).upper().strip()
        scores = row.get("scores", {}) or {}
        if not tk or not isinstance(scores, dict):
            continue
        # Force every vector key to exist with int(0-5) clamp.
        clean_scores = {}
        for k, _ in VECTORS:
            v = scores.get(k, 0)
            try:
                v = int(v)
            except Exception:
                v = 0
            clean_scores[k] = max(0, min(5, v))
        total = sum(clean_scores.values())  # 0-60
        out.append({
            "ticker":      tk,
            "scores":      clean_scores,
            "total":       total,
            "one_liner":   str(row.get("one_liner", ""))[:300],
            "catalyst_30d": str(row.get("catalyst_30d", ""))[:200],
        })
    # Sort by total desc, then by ticker for stability
    out.sort(key=lambda r: (-r["total"], r["ticker"]))
    return out


# ── Smart-money overlay (Gemini grounded) ──────────────────────────────────
def smartmoney_sweep(force: bool = False) -> dict:
    """
    For each tracked operator, get a current snapshot of their notable
    recent moves. Cached 24h. Returns { operator: { summary, moves: [...] } }
    """
    cache_path = _SMART_DIR / "_index.json"
    if cache_path.exists() and not force:
        try:
            data = json.loads(cache_path.read_text())
            if time.time() - data.get("ts", 0) < 86400:
                return data
        except Exception:
            pass

    llm = _llm()
    result: dict = {"ts": time.time(), "operators": {}}

    for op in SMART_MONEY:
        prompt = (
            f"What are the notable recent investment moves by {op} in the "
            f"last 90 days that are publicly reported (13F filings, Form 4s, "
            f"public statements, LLC filings, donation flows)? Reply in 2-3 "
            f"sentences naming specific tickers, dollar amounts or share "
            f"counts, and dates. If nothing material is publicly known, say so."
        )
        summary = llm.gemini_call(prompt, grounding=True, max_tokens=500) or ""
        result["operators"][op] = {"summary": summary, "ts": time.time()}

    cache_path.write_text(json.dumps(result, indent=2))
    return result


# ── Public API: ranks ──────────────────────────────────────────────────────
RANK_TTL = 6 * 3600  # 6 hours


def _rank_path(sector_id: str) -> Path:
    return _RANKS_DIR / f"{sector_id}.json"


def get_rank(sector_id: str, force: bool = False) -> dict:
    """
    Return cached ranks for a sector. If stale or missing, bake now.
    Output shape:
      { sector, label, emoji, ts, vectors: [[key,label],...], rows: [...] }
    """
    if sector_id not in SECTORS:
        return {"error": "unknown sector"}
    path = _rank_path(sector_id)
    if path.exists() and not force:
        try:
            data = json.loads(path.read_text())
            if time.time() - data.get("ts", 0) < RANK_TTL:
                return data
        except Exception:
            pass

    tickers = _discover_tickers(sector_id)
    rows = _score_tickers(tickers, sector_id) if tickers else []
    sector = SECTORS[sector_id]
    data = {
        "sector":  sector_id,
        "label":   sector["label"],
        "emoji":   sector["emoji"],
        "ts":      time.time(),
        "vectors": VECTORS,
        "rows":    rows,
    }
    path.write_text(json.dumps(data, indent=2))
    return data


def bake_all(force: bool = False) -> dict:
    """Pre-bake every sector. Run on a background timer or manual trigger."""
    out = {}
    for sid in SECTORS:
        out[sid] = {"count": len(get_rank(sid, force=force).get("rows", []))}
    smartmoney_sweep(force=force)
    _META.write_text(json.dumps({"baked_at": time.time(), "result": out}, indent=2))
    return out


# ── Public API: per-ticker thesis (CLAUDE — only on demand) ────────────────
THESIS_TTL = 24 * 3600


def _thesis_path(ticker: str) -> Path:
    return _THESIS_DIR / f"{ticker.upper()}.json"


def get_thesis(ticker: str, force: bool = False) -> dict:
    """
    Layered Conviction Capital thesis for a single ticker.
    GEMINI assembles the data dossier (grounded news + filings + catalyst).
    CLAUDE writes the brand-voice take.
    """
    tk = ticker.upper().strip()
    path = _thesis_path(tk)
    if path.exists() and not force:
        try:
            data = json.loads(path.read_text())
            if time.time() - data.get("ts", 0) < THESIS_TTL:
                return data
        except Exception:
            pass

    llm = _llm()

    # ── Step 1: Gemini gathers the dossier (grounded) ────────────────────
    dossier_prompt = (
        f"Compile a current intelligence dossier on ${tk}. Reply in JSON:\n"
        "{\n"
        '  "snapshot": "2 sentences: business model + current narrative",\n'
        '  "structural_advantages": ["3-5 named moats — specific assets/contracts/IP"],\n'
        '  "smart_money": "1-2 sentences on notable institutional/insider positioning",\n'
        '  "catalysts_60d": ["named events in next 60 days with dates"],\n'
        '  "risks": ["3 concrete risks, not generic"],\n'
        '  "convergence_notes": "Which of these vectors does this hit? LAND/POWER/WATER/GRID-RIGHTS/MINERALS/DATA-CENTER-PROXIMITY/AI-PIVOT/DEFENSE/POLICY/SMART-MONEY"\n'
        "}\n"
        "Output ONLY the JSON. Be specific. If something is unknown, say so."
    )
    dossier = llm.gemini_json(dossier_prompt, max_tokens=1500) or {}

    # ── Step 2: Claude writes the layered take ───────────────────────────
    claude_prompt = f"""You are the intelligence writer for Conviction Capital.

Ticker: ${tk}
Dossier (from Gemini grounded search):
{json.dumps(dossier, indent=2)}

Write the layered Conviction Capital take. Output ONLY this JSON:

{{
  "headline":        "One sentence: the single most important reason ${tk} matters right now.",
  "plain_fact":      "What the company actually does, in plain English.",
  "structural_why":  "Why this matters structurally — the bottleneck or moat or convergence.",
  "winners_losers":  "Named: who wins if this plays out, who loses.",
  "your_wallet":     "How this hits a regular investor's portfolio over 1, 3, 5 years.",
  "conviction":      "LOW / MEDIUM / HIGH / VERY HIGH — with one-sentence justification.",
  "watch_levels":    ["Specific price level, ratio, or event to monitor"],
  "short_hooks":     ["3 hooks with specific number / ticker / date"]
}}

Rules:
- Output JSON only. No markdown fences.
- Be specific. No platitudes. No hedging language.
- If the dossier is thin, lower the conviction — don't fabricate."""

    take = llm.claude_json(claude_prompt, max_tokens=1500) or {}

    data = {
        "ticker":  tk,
        "ts":      time.time(),
        "dossier": dossier,
        "take":    take,
    }
    path.write_text(json.dumps(data, indent=2))
    return data


# ── CLI for manual runs ────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "bake":
        print("[movers] baking all sectors…")
        print(json.dumps(bake_all(force=True), indent=2))
    elif len(sys.argv) > 2 and sys.argv[1] == "rank":
        print(json.dumps(get_rank(sys.argv[2], force=True), indent=2))
    elif len(sys.argv) > 2 and sys.argv[1] == "thesis":
        print(json.dumps(get_thesis(sys.argv[2], force=True), indent=2))
    else:
        print("Usage: python3 -m web.movers bake | rank <sector> | thesis <ticker>")
        print("Sectors:", ", ".join(SECTORS.keys()))
