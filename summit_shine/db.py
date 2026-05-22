"""SQLite layer. One file, parametrised queries, foreign keys on."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

DB_PATH = os.environ.get("SUMMIT_DB", "summit_shine.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    address       TEXT,
    property_type TEXT,         -- residential / commercial / office / other
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id     INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    description   TEXT,
    status        TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled / in_progress / done / cancelled
    assigned_to   TEXT,
    scheduled_for TEXT,         -- ISO date (YYYY-MM-DD)
    price         REAL,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at  TEXT
);

CREATE TABLE IF NOT EXISTS quotes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    number       TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'draft',  -- draft / sent / accepted / declined
    notes        TEXT,
    valid_until  TEXT,
    tax_rate     REAL NOT NULL DEFAULT 0,
    subtotal     REAL NOT NULL DEFAULT 0,
    tax          REAL NOT NULL DEFAULT 0,
    total        REAL NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at      TEXT,
    accepted_at  TEXT
);

CREATE TABLE IF NOT EXISTS quote_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id    INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    quantity    REAL NOT NULL DEFAULT 1,
    unit_price  REAL NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invoices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    quote_id    INTEGER REFERENCES quotes(id),
    number      TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'draft',  -- draft / sent / paid / overdue
    notes       TEXT,
    due_date    TEXT,
    tax_rate    REAL NOT NULL DEFAULT 0,
    subtotal    REAL NOT NULL DEFAULT 0,
    tax         REAL NOT NULL DEFAULT 0,
    total       REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at     TEXT,
    paid_at     TEXT
);

CREATE TABLE IF NOT EXISTS invoice_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    quantity    REAL NOT NULL DEFAULT 1,
    unit_price  REAL NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quote_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT,
    phone         TEXT,
    address       TEXT,
    property_type TEXT,
    service_type  TEXT,
    frequency     TEXT,
    details       TEXT,
    status        TEXT NOT NULL DEFAULT 'new',  -- new / contacted / quoted / converted / dismissed
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    client_id     INTEGER REFERENCES clients(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_client     ON jobs(client_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_quotes_client   ON quotes(client_id);
CREATE INDEX IF NOT EXISTS idx_invoices_client ON invoices(client_id);
CREATE INDEX IF NOT EXISTS idx_requests_status ON quote_requests(status);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with connect() as conn:
        cur = conn.execute(sql, tuple(params))
        return cur.lastrowid


def next_number(prefix: str, table: str) -> str:
    """Generate the next sequential reference number for the current year, e.g. Q-2026-0007."""
    year = __import__("datetime").datetime.utcnow().strftime("%Y")
    like = f"{prefix}-{year}-%"
    row = query_one(
        f"SELECT number FROM {table} WHERE number LIKE ? ORDER BY id DESC LIMIT 1",
        (like,),
    )
    if row:
        try:
            n = int(row["number"].rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            n = 1
    else:
        n = 1
    return f"{prefix}-{year}-{n:04d}"


def recompute_totals(table: str, parent_id: int) -> None:
    """Recompute subtotal/tax/total for a quote or invoice from its line items."""
    items_table = "quote_items" if table == "quotes" else "invoice_items"
    fk = "quote_id" if table == "quotes" else "invoice_id"
    with connect() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(quantity * unit_price), 0) AS subtotal FROM {items_table} WHERE {fk} = ?",
            (parent_id,),
        ).fetchone()
        subtotal = float(row["subtotal"] or 0)
        rate = conn.execute(f"SELECT tax_rate FROM {table} WHERE id = ?", (parent_id,)).fetchone()
        tax_rate = float(rate["tax_rate"] or 0) if rate else 0
        tax = round(subtotal * tax_rate, 2)
        total = round(subtotal + tax, 2)
        conn.execute(
            f"UPDATE {table} SET subtotal = ?, tax = ?, total = ? WHERE id = ?",
            (subtotal, tax, total, parent_id),
        )
