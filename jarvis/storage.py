"""SQLite store for predictions, incidents, rules, bankroll history."""
from __future__ import annotations
import sqlite3, json, os, time
from contextlib import contextmanager
from datetime import datetime
from typing import Any

DB_PATH = os.path.join(os.path.dirname(__file__), "jarvis.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  domain TEXT NOT NULL,                 -- 'prediction_market' | 'equity'
  market TEXT NOT NULL,                 -- slug or symbol
  question TEXT,
  side TEXT,                            -- 'YES' / 'NO' / 'LONG' / 'SHORT'
  market_price REAL,                    -- 0..1 for PM, $ for equity
  raw_prob REAL,
  true_prob REAL,
  edge_pct REAL,
  confidence REAL,
  size_usd REAL,
  features_json TEXT,
  agents_json TEXT,
  status TEXT DEFAULT 'open',           -- open | resolved | cancelled
  resolution TEXT,                      -- WIN | LOSS | PUSH
  pnl_usd REAL,
  resolved_ts REAL
);

CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  prediction_id INTEGER,
  root_cause TEXT,
  agent_findings_json TEXT,
  rules_added_json TEXT,
  FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);

CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  rule_key TEXT UNIQUE,                 -- e.g. 'block_if_geopolitical_flag'
  rule_json TEXT NOT NULL,              -- {scope, condition, action, reason}
  active INTEGER DEFAULT 1,
  source_incident_id INTEGER
);

CREATE TABLE IF NOT EXISTS bankroll_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  bankroll_usd REAL NOT NULL,
  delta_usd REAL,
  reason TEXT
);

CREATE TABLE IF NOT EXISTS alerts_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  channel TEXT,                         -- sms | twitter | rss | youtube
  source TEXT,
  payload_json TEXT,
  consumed INTEGER DEFAULT 0
);
"""


def init_db(path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
        c.commit()


@contextmanager
def conn(path: str = DB_PATH):
    init_db(path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


# ---- predictions ---------------------------------------------------------

def save_prediction(p: dict[str, Any]) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO predictions
               (ts, domain, market, question, side, market_price,
                raw_prob, true_prob, edge_pct, confidence, size_usd,
                features_json, agents_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(), p["domain"], p["market"], p.get("question"),
                p.get("side"), p.get("market_price"), p.get("raw_prob"),
                p.get("true_prob"), p.get("edge_pct"), p.get("confidence"),
                p.get("size_usd"),
                json.dumps(p.get("features", {})),
                json.dumps(p.get("agents", {})),
            ),
        )
        return cur.lastrowid


def resolve_prediction(pred_id: int, resolution: str, pnl_usd: float) -> None:
    with conn() as c:
        c.execute(
            "UPDATE predictions SET status='resolved', resolution=?, pnl_usd=?, resolved_ts=? WHERE id=?",
            (resolution, pnl_usd, time.time(), pred_id),
        )


def get_prediction(pred_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM predictions WHERE id=?", (pred_id,)).fetchone()
        return dict(r) if r else None


def open_predictions() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM predictions WHERE status='open' ORDER BY ts DESC").fetchall()
        return [dict(r) for r in rows]


# ---- incidents -----------------------------------------------------------

def save_incident(prediction_id: int, root_cause: str,
                  agent_findings: dict, rules_added: list[str]) -> int:
    with conn() as c:
        cur = c.execute(
            """INSERT INTO incidents (ts, prediction_id, root_cause,
               agent_findings_json, rules_added_json)
               VALUES (?,?,?,?,?)""",
            (time.time(), prediction_id, root_cause,
             json.dumps(agent_findings), json.dumps(rules_added)),
        )
        return cur.lastrowid


# ---- rules ---------------------------------------------------------------

def add_rule(key: str, rule: dict, source_incident_id: int | None = None) -> None:
    with conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO rules (ts, rule_key, rule_json, active, source_incident_id)
               VALUES (?,?,?,1,?)""",
            (time.time(), key, json.dumps(rule), source_incident_id),
        )


def active_rules() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM rules WHERE active=1").fetchall()
        out = []
        for r in rows:
            d = dict(r); d["rule"] = json.loads(d.pop("rule_json"))
            out.append(d)
        return out


# ---- bankroll ------------------------------------------------------------

def record_bankroll(bankroll_usd: float, delta_usd: float = 0.0, reason: str = "") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO bankroll_history (ts, bankroll_usd, delta_usd, reason) VALUES (?,?,?,?)",
            (time.time(), bankroll_usd, delta_usd, reason),
        )


def latest_bankroll() -> float | None:
    with conn() as c:
        r = c.execute("SELECT bankroll_usd FROM bankroll_history ORDER BY ts DESC LIMIT 1").fetchone()
        return r["bankroll_usd"] if r else None


def daily_pnl() -> float:
    """Sum pnl_usd resolved within last 24h."""
    cutoff = time.time() - 86400
    with conn() as c:
        r = c.execute(
            "SELECT COALESCE(SUM(pnl_usd),0) AS p FROM predictions "
            "WHERE status='resolved' AND resolved_ts > ?", (cutoff,),
        ).fetchone()
        return r["p"] or 0.0


# ---- alerts log ----------------------------------------------------------

def log_alert(channel: str, source: str, payload: dict) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO alerts_log (ts, channel, source, payload_json) VALUES (?,?,?,?)",
            (time.time(), channel, source, json.dumps(payload)),
        )
        return cur.lastrowid


def unconsumed_alerts(limit: int = 100) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM alerts_log WHERE consumed=0 ORDER BY ts DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
