"""
Sports lines + props engine.

Pulls live odds from two free sources and normalizes them into a single
shape for the /sports.html UI:

  - ESPN core/site API  → game schedule + ESPN BET lines + futures
    (totally open, no auth, never blocked)
  - DraftKings unofficial sportsbook API → DK lines + player props
    (open, no auth, but can be CDN-blocked from datacenter IPs)

When DK fails (403/timeout) we fall back to ESPN-only so the page still
renders. All helpers NEVER raise — they return [] or {} on failure.
"""
from __future__ import annotations

import time
from typing import Optional

import requests

# ── Browser-ish UA — DraftKings filters by UA on this endpoint ────────────────
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}

# ── League catalogue ─────────────────────────────────────────────────────────
# ESPN uses sport/league pairs; DraftKings uses an opaque "eventGroup" id.
# These DK ids drift between seasons — verify if you see empty results.
LEAGUES: dict[str, dict] = {
    "nfl": {
        "label": "NFL",
        "emoji": "🏈",
        "espn_sport":  "football",
        "espn_league": "nfl",
        "dk_group":    88808,
    },
    "nba": {
        "label": "NBA",
        "emoji": "🏀",
        "espn_sport":  "basketball",
        "espn_league": "nba",
        "dk_group":    42648,
    },
    "mlb": {
        "label": "MLB",
        "emoji": "⚾",
        "espn_sport":  "baseball",
        "espn_league": "mlb",
        "dk_group":    84240,
    },
    "nhl": {
        "label": "NHL",
        "emoji": "🏒",
        "espn_sport":  "hockey",
        "espn_league": "nhl",
        "dk_group":    42133,
    },
    "ncaaf": {
        "label": "NCAAF",
        "emoji": "🏈",
        "espn_sport":  "football",
        "espn_league": "college-football",
        "dk_group":    87637,
    },
    "ncaab": {
        "label": "NCAAB",
        "emoji": "🏀",
        "espn_sport":  "basketball",
        "espn_league": "mens-college-basketball",
        "dk_group":    92483,
    },
}

# ── American odds ↔ implied probability ──────────────────────────────────────
def american_to_prob(odds: Optional[float | int]) -> Optional[float]:
    """+150 → 0.40 ; -200 → 0.667. Returns None on bad input."""
    if odds is None:
        return None
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    if o == 0:
        return None
    if o > 0:
        return 100.0 / (o + 100.0)
    return abs(o) / (abs(o) + 100.0)


def fmt_american(odds) -> str:
    if odds is None:
        return "—"
    try:
        o = int(float(odds))
    except (TypeError, ValueError):
        return str(odds)
    return f"+{o}" if o > 0 else str(o)


# ── ESPN: scoreboard + ESPN BET lines ────────────────────────────────────────
def _espn_scoreboard(league_id: str) -> list[dict]:
    """Return normalized games from ESPN scoreboard for one league."""
    league = LEAGUES.get(league_id)
    if not league:
        return []
    url = (f"https://site.api.espn.com/apis/site/v2/sports/"
           f"{league['espn_sport']}/{league['espn_league']}/scoreboard")
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[sports] espn scoreboard {league_id}: {e}")
        return []

    games: list[dict] = []
    for ev in data.get("events", []) or []:
        comp = (ev.get("competitions") or [{}])[0]
        teams = comp.get("competitors") or []
        if len(teams) < 2:
            continue
        # ESPN orders competitors home-first sometimes, away-first other times
        home = next((t for t in teams if t.get("homeAway") == "home"), teams[0])
        away = next((t for t in teams if t.get("homeAway") == "away"), teams[1])

        odds_list = comp.get("odds") or []
        # Prefer ESPN BET (providerId 58); fall back to whatever is first
        espn_bet = next((o for o in odds_list
                         if str(o.get("provider", {}).get("id")) == "58"), None)
        primary = espn_bet or (odds_list[0] if odds_list else {})

        details = primary.get("details")        # e.g. "KC -3.5"
        ou      = primary.get("overUnder")
        home_ml = primary.get("homeTeamOdds", {}).get("moneyLine")
        away_ml = primary.get("awayTeamOdds", {}).get("moneyLine")

        games.append({
            "league":    league_id,
            "espn_id":   str(ev.get("id") or ""),
            "start":     ev.get("date"),
            "status":    ((ev.get("status") or {}).get("type") or {}).get("description"),
            "home": {
                "name":  home.get("team", {}).get("displayName"),
                "abbr":  home.get("team", {}).get("abbreviation"),
                "logo":  home.get("team", {}).get("logo"),
                "score": home.get("score"),
            },
            "away": {
                "name":  away.get("team", {}).get("displayName"),
                "abbr":  away.get("team", {}).get("abbreviation"),
                "logo":  away.get("team", {}).get("logo"),
                "score": away.get("score"),
            },
            "espn_bet": {
                "details":  details,
                "overUnder": ou,
                "home_ml":  home_ml,
                "away_ml":  away_ml,
                "provider": (primary.get("provider") or {}).get("name") or "—",
            },
        })
    return games


# ── DraftKings unofficial sportsbook endpoint ────────────────────────────────
def _dk_eventgroup(league_id: str) -> dict:
    """Raw DK eventgroup payload, or {} on failure."""
    league = LEAGUES.get(league_id)
    if not league:
        return {}
    url = (f"https://sportsbook.draftkings.com/sites/US-SB/api/v5/"
           f"eventgroups/{league['dk_group']}?format=json")
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        if r.status_code != 200:
            print(f"[sports] DK {league_id} returned HTTP {r.status_code}")
            return {}
        return r.json() or {}
    except Exception as e:
        print(f"[sports] DK {league_id}: {e}")
        return {}


def _dk_game_lines(payload: dict) -> dict[str, dict]:
    """
    Build a {event_id: {spread, total, home_ml, away_ml, home_name, away_name}}
    map from the DK eventgroup payload.
    """
    out: dict[str, dict] = {}
    eg = payload.get("eventGroup") or {}
    events = {str(e.get("eventId")): e for e in (eg.get("events") or [])}

    for cat in eg.get("offerCategories") or []:
        # The "Game Lines" category holds spread / total / moneyline
        if (cat.get("name") or "").lower() not in ("game lines", "main", "popular"):
            continue
        for sub in cat.get("offerSubcategoryDescriptors") or []:
            offer_groups = ((sub.get("offerSubcategory") or {}).get("offers")) or []
            for offer_group in offer_groups:
                for offer in offer_group or []:
                    event_id = str(offer.get("eventId") or "")
                    if not event_id:
                        continue
                    label = (offer.get("label") or "").lower()
                    outcomes = offer.get("outcomes") or []
                    row = out.setdefault(event_id, {})

                    if label == "spread":
                        for oc in outcomes:
                            side = "home" if oc.get("label") == \
                                events.get(event_id, {}).get("teamName1") else "away"
                            row[f"spread_{side}"] = oc.get("line")
                            row[f"spread_{side}_odds"] = oc.get("oddsAmerican")
                    elif label == "total":
                        if outcomes:
                            row["total"]      = outcomes[0].get("line")
                            row["over_odds"]  = outcomes[0].get("oddsAmerican")
                            row["under_odds"] = (outcomes[1] or {}).get("oddsAmerican")
                    elif label == "moneyline":
                        for oc in outcomes:
                            side = "home" if oc.get("label") == \
                                events.get(event_id, {}).get("teamName1") else "away"
                            row[f"{side}_ml"] = oc.get("oddsAmerican")

    # Attach team names too so we can join with ESPN by abbr/name
    for eid, ev in events.items():
        if eid in out:
            out[eid]["dk_home"] = ev.get("teamName1")
            out[eid]["dk_away"] = ev.get("teamName2")
            out[eid]["dk_url"]  = ev.get("eventPath") and \
                f"https://sportsbook.draftkings.com{ev['eventPath']}"
            out[eid]["start"]   = ev.get("startDate")
    return out


def _name_key(name: Optional[str]) -> str:
    """Loose key for matching DK team names to ESPN names."""
    if not name:
        return ""
    return "".join(c for c in name.lower() if c.isalnum())


def _merge_dk_into_espn(games: list[dict], dk_lines: dict[str, dict]) -> list[dict]:
    """For each ESPN game attach DK lines if we can match by team name."""
    if not dk_lines:
        for g in games:
            g["dk"] = None
        return games

    # Build name → DK row lookup using both home and away keys
    name_index: dict[str, dict] = {}
    for row in dk_lines.values():
        for k in (row.get("dk_home"), row.get("dk_away")):
            nk = _name_key(k)
            if nk:
                name_index[nk] = row

    for g in games:
        match = (
            name_index.get(_name_key(g["home"]["name"])) or
            name_index.get(_name_key(g["away"]["name"])) or
            name_index.get(_name_key(g["home"]["abbr"])) or
            name_index.get(_name_key(g["away"]["abbr"]))
        )
        g["dk"] = match
    return games


# ── Edge calculation ─────────────────────────────────────────────────────────
def _compute_edge(g: dict) -> dict:
    """
    Compare ESPN BET moneyline vs DK moneyline. Returns:
       { 'has_edge': bool, 'side': 'home'|'away'|None, 'pp': float, 'cheap_book': str }
    The "edge" is the absolute difference in implied YES% between the books on
    the same outcome — bet the cheaper side.
    """
    e  = g.get("espn_bet") or {}
    dk = g.get("dk") or {}
    espn_h = american_to_prob(e.get("home_ml"))
    espn_a = american_to_prob(e.get("away_ml"))
    dk_h   = american_to_prob(dk.get("home_ml"))
    dk_a   = american_to_prob(dk.get("away_ml"))

    def diff(a, b):
        if a is None or b is None:
            return None
        return (a - b) * 100.0  # percentage points

    h = diff(espn_h, dk_h)
    a = diff(espn_a, dk_a)

    candidates = []
    if h is not None:
        # If ESPN says home is more likely than DK does, DK home is cheap
        candidates.append(("home", h))
    if a is not None:
        candidates.append(("away", a))
    if not candidates:
        return {"has_edge": False, "side": None, "pp": 0.0, "cheap_book": None}

    side, pp = max(candidates, key=lambda x: abs(x[1]))
    cheap_book = "DraftKings" if pp > 0 else "ESPN BET"
    return {
        "has_edge":   abs(pp) >= 1.0,
        "side":       side,
        "pp":         round(abs(pp), 2),
        "cheap_book": cheap_book,
        "signed_pp":  round(pp, 2),
    }


# ── ESPN futures (MVP, ROY, champion etc.) ───────────────────────────────────
def _espn_futures(league_id: str) -> list[dict]:
    """
    ESPN exposes futures markets at:
      core.api.espn.com/v2/sports/{sport}/leagues/{league}/futures
    Each future has multiple competitors with odds. Returns a flat normalized
    list. Best-effort: silently returns [] on schema drift.
    """
    league = LEAGUES.get(league_id)
    if not league:
        return []
    url = (f"https://sports.core.api.espn.com/v2/sports/"
           f"{league['espn_sport']}/leagues/{league['espn_league']}/futures")
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:
        print(f"[sports] espn futures {league_id}: {e}")
        return []

    futures = []
    for item in data.get("items") or []:
        name = item.get("name") or "Future"
        for fut in item.get("futures") or []:
            provider = (fut.get("provider") or {}).get("name") or "—"
            books = fut.get("books") or []
            # Top 5 entrants per future
            entries = []
            for b in books[:25]:
                entries.append({
                    "team":  (b.get("athlete") or b.get("team") or {}).get("displayName"),
                    "value": b.get("value"),
                })
            entries = [e for e in entries if e["team"]]
            if entries:
                futures.append({
                    "name":     name,
                    "provider": provider,
                    "entries":  entries[:10],
                })
            if len(futures) >= 8:
                break
        if len(futures) >= 8:
            break
    return futures


# ── DK player props per event ────────────────────────────────────────────────
def _dk_props(payload: dict, event_id: str) -> list[dict]:
    """
    Pull player-prop offers for a single event from the DK payload.
    Returns a list of {market, player, line, over_odds, under_odds}.
    """
    out: list[dict] = []
    eg = payload.get("eventGroup") or {}
    seen_keys = set()

    for cat in eg.get("offerCategories") or []:
        cat_name = cat.get("name") or ""
        # We only want player-prop categories (NFL: "Touchdown Scorer",
        # "Passing Yards", NBA: "Player Points", "Player Rebounds", etc.)
        if not any(kw in cat_name.lower() for kw in (
                "player", "touchdown", "passing", "rushing", "receiving",
                "points", "rebounds", "assists", "strikeouts", "hits",
                "shots", "goals", "saves")):
            continue
        for sub in cat.get("offerSubcategoryDescriptors") or []:
            market = sub.get("name") or cat_name
            offer_groups = ((sub.get("offerSubcategory") or {}).get("offers")) or []
            for offer_group in offer_groups:
                for offer in offer_group or []:
                    if str(offer.get("eventId") or "") != event_id:
                        continue
                    outcomes = offer.get("outcomes") or []
                    # Typical shape: 2 outcomes (Over / Under) with same line
                    if len(outcomes) < 1:
                        continue
                    player = (offer.get("label") or
                              outcomes[0].get("participant") or
                              outcomes[0].get("label", "").split(" Over")[0])
                    over  = next((o for o in outcomes
                                  if (o.get("label") or "").lower().startswith("over")),
                                 outcomes[0])
                    under = next((o for o in outcomes
                                  if (o.get("label") or "").lower().startswith("under")),
                                 None)
                    line = over.get("line") if over else None
                    key = (market, player, line)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    out.append({
                        "market":     market,
                        "player":     player,
                        "line":       line,
                        "over_odds":  over.get("oddsAmerican") if over else None,
                        "under_odds": under.get("oddsAmerican") if under else None,
                    })
    # Sort by market then player for stable display
    out.sort(key=lambda p: (p["market"] or "", p["player"] or ""))
    return out[:80]   # cap to keep payload light


# ── Public, cached API ───────────────────────────────────────────────────────
_CACHE: dict = {}
_TTL = 60   # seconds — main lines move fast, no point caching longer

def _cached(key: str, builder):
    now = time.time()
    rec = _CACHE.get(key)
    if rec and now - rec["ts"] < _TTL:
        return rec["val"]
    val = builder()
    _CACHE[key] = {"ts": now, "val": val}
    return val


def league_board(league_id: str) -> dict:
    """
    Main payload for the /sports.html page. Returns:
      { league, games: [...], futures: [...], dk_ok: bool, last_updated }
    """
    if league_id not in LEAGUES:
        return {"error": "unknown league", "games": [], "futures": []}

    def _build():
        games = _espn_scoreboard(league_id)
        dk_payload = _dk_eventgroup(league_id)
        dk_lines   = _dk_game_lines(dk_payload) if dk_payload else {}
        games = _merge_dk_into_espn(games, dk_lines)
        # Attach edge calc + DK url + start
        for g in games:
            g["edge"] = _compute_edge(g)
            if g.get("dk") and g["dk"].get("dk_url"):
                g["dk_url"] = g["dk"]["dk_url"]
        # Sort: games with edge first (largest), then by start time
        games.sort(key=lambda g: (
            -(g["edge"]["pp"] if g["edge"]["has_edge"] else 0),
            g.get("start") or "",
        ))
        futures = _espn_futures(league_id)

        meta = LEAGUES[league_id]
        return {
            "league":       league_id,
            "label":        meta["label"],
            "emoji":        meta["emoji"],
            "games":        games,
            "futures":      futures,
            "dk_ok":        bool(dk_payload),
            "last_updated": int(time.time()),
            "_dk_raw":      dk_payload,   # stash for prop lookups
        }

    payload = _cached(f"league:{league_id}", _build)
    # Don't return the raw DK dump over the wire — strip it
    out = {k: v for k, v in payload.items() if k != "_dk_raw"}
    return out


def event_props(league_id: str, event_id: str) -> dict:
    """Per-game player props from DraftKings."""
    if league_id not in LEAGUES:
        return {"error": "unknown league", "props": []}
    # Force the league cache so we have the DK payload to dig through
    board = _cached(f"league:{league_id}", lambda: league_board(league_id))
    # league_board strips _dk_raw — re-fetch the cached internal version
    internal = _CACHE.get(f"league:{league_id}", {}).get("val") or {}
    dk_payload = internal.get("_dk_raw") or {}
    if not dk_payload:
        return {"props": [], "dk_ok": False}
    props = _dk_props(dk_payload, event_id)
    return {"props": props, "dk_ok": True, "count": len(props)}


def leagues_index() -> list[dict]:
    """Lightweight list for the league picker tiles in the UI."""
    return [
        {"id": lid, "label": meta["label"], "emoji": meta["emoji"]}
        for lid, meta in LEAGUES.items()
    ]


# ── CLI for quick smoke-test ─────────────────────────────────────────────────
if __name__ == "__main__":
    import json as _json, sys
    lid = sys.argv[1] if len(sys.argv) > 1 else "nba"
    board = league_board(lid)
    print(_json.dumps({
        "league":  board["label"],
        "games":   len(board["games"]),
        "futures": len(board["futures"]),
        "dk_ok":   board["dk_ok"],
        "sample":  board["games"][:1],
    }, indent=2, default=str))
