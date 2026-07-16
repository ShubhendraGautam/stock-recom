"""Portfolio persistence (SQLite) and per-holding advice."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "portfolio.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol  TEXT NOT NULL,
    action  TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
    units   REAL NOT NULL CHECK (units > 0),
    price   REAL NOT NULL CHECK (price >= 0),
    ts      TEXT NOT NULL
);
"""


@dataclass
class Holding:
    symbol: str
    units: float
    avg_price: float

    @property
    def invested(self) -> float:
        return self.units * self.avg_price


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_SCHEMA)
    return conn


def record(symbol: str, action: str, units: float, price: float) -> None:
    symbol = symbol.upper().strip()
    with _connect() as conn:
        if action == "SELL":
            held = holdings().get(symbol)
            if held is None or held.units < units - 1e-9:
                have = held.units if held else 0
                raise ValueError(
                    f"cannot sell {units:g} units of {symbol}: you hold {have:g}"
                )
        conn.execute(
            "INSERT INTO transactions (symbol, action, units, price, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (symbol, action, units, price,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def holdings() -> dict[str, Holding]:
    """Current positions with weighted-average buy price (FIFO-free average)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT symbol, action, units, price FROM transactions ORDER BY id"
        ).fetchall()

    pos: dict[str, list[float]] = {}  # symbol -> [units, cost]
    for symbol, action, units, price in rows:
        u, cost = pos.get(symbol, [0.0, 0.0])
        if action == "BUY":
            pos[symbol] = [u + units, cost + units * price]
        else:  # SELL reduces units at current average cost
            avg = cost / u if u else 0.0
            pos[symbol] = [u - units, max(0.0, cost - units * avg)]

    return {
        s: Holding(s, u, cost / u)
        for s, (u, cost) in pos.items()
        if u > 1e-9
    }


def transactions() -> list[tuple]:
    with _connect() as conn:
        return conn.execute(
            "SELECT id, symbol, action, units, price, ts "
            "FROM transactions ORDER BY id DESC"
        ).fetchall()


def advise(score: float, pnl_pct: float) -> tuple[str, str]:
    """Advice for an existing holding from composite score + unrealized P&L.

    Returns (action, reason). Actions: BUY MORE / HOLD / SELL.
    """
    if score >= 62:
        if pnl_pct < -5:
            return "BUY MORE", "strong outlook; averaging down looks justified"
        return "BUY MORE", "strong technical + fundamental + sentiment outlook"
    if score >= 45:
        if pnl_pct <= -15:
            return "HOLD", "outlook is average; loss is deep - avoid panic selling, review next results"
        return "HOLD", "outlook is average; no edge in adding or exiting now"
    # weak score
    if pnl_pct <= -12:
        return "SELL", "weak outlook and position is losing - consider cutting the loss"
    if pnl_pct >= 15:
        return "SELL", "weak outlook - consider booking your profit"
    return "SELL", "weak technicals/fundamentals - consider exiting on strength"
