"""Persistent research memory.

Every deep run is stored in SQLite. Because the tool is run on sparse days,
each run adds an independent observation; recommendations then favor
CONVICTION - stocks that score well repeatedly across runs - over one-day
wonders, and show how a stock's score moved since the last run.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Separate tier from portfolio.db: run outcomes are valuable but rebuildable;
# transactions are precious. Caches stay in .cache/ (disposable).
DB_PATH = Path(__file__).resolve().parent.parent / "research.db"

LOOKBACK_RUNS = 8          # how many past runs feed conviction
PAST_WEIGHT = 0.25         # conviction = 0.75*today + 0.25*avg(past runs)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    universe TEXT
);
CREATE TABLE IF NOT EXISTS analyses (
    run_id        INTEGER NOT NULL REFERENCES runs(id),
    symbol        TEXT NOT NULL,
    price         REAL,
    composite     REAL,
    technical     REAL,
    risk          REAL,
    fundamental   REAL,
    quality       REAL,
    sentiment     REAL,
    institutional REAL,
    verdict       TEXT,
    PRIMARY KEY (run_id, symbol)
);
CREATE TABLE IF NOT EXISTS outcomes (
    run_id   INTEGER NOT NULL,
    symbol   TEXT NOT NULL,
    rec_price REAL, price_after REAL, days INTEGER, ret_pct REAL,
    PRIMARY KEY (run_id, symbol)
);
"""

OUTCOME_MIN_DAYS = 25      # calendar days before a recommendation "matures"
OUTCOME_MAX_DAYS = 60


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(analyses)")}
    if "prediction" not in cols:
        conn.execute("ALTER TABLE analyses ADD COLUMN prediction REAL")
    return conn


def save_run(universe: str, results: list) -> int:
    """Persist one deep run's results. Returns the run id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (ts, universe) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), universe),
        )
        run_id = cur.lastrowid
        for r in results:
            c = r.components
            conn.execute(
                "INSERT OR REPLACE INTO analyses "
                "(run_id, symbol, price, composite, technical, risk, "
                " fundamental, quality, sentiment, institutional, verdict, "
                " prediction) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, r.symbol, r.price, r.composite,
                 c.get("technical"), c.get("risk"), c.get("fundamental"),
                 c.get("quality"), c.get("sentiment"), c.get("institutional"),
                 r.verdict, c.get("prediction")),
            )
        return run_id


def last_run() -> dict | None:
    """{'id', 'ts', 'universe', 'age_days'} of the most recent run, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, ts, universe FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    ts = datetime.fromisoformat(row[1])
    age = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    return {"id": row[0], "ts": row[1], "universe": row[2], "age_days": age}


def past_stats(lookback: int = LOOKBACK_RUNS) -> dict[str, dict]:
    """Per-symbol stats over the last `lookback` runs.

    {symbol: {avg, last, last_price, runs}} - `last` is the composite from
    the most recent run that included the symbol.
    """
    with _connect() as conn:
        run_ids = [r[0] for r in conn.execute(
            "SELECT id FROM runs ORDER BY id DESC LIMIT ?", (lookback,)
        ).fetchall()]
        if not run_ids:
            return {}
        marks = ",".join("?" * len(run_ids))
        rows = conn.execute(
            f"SELECT symbol, composite, price, run_id FROM analyses "
            f"WHERE run_id IN ({marks}) ORDER BY run_id DESC",
            run_ids,
        ).fetchall()

    stats: dict[str, dict] = {}
    for symbol, composite, price, _run_id in rows:
        if composite is None:
            continue
        s = stats.setdefault(
            symbol, {"scores": [], "last": composite, "last_price": price})
        s["scores"].append(composite)
    return {
        sym: {
            "avg": sum(s["scores"]) / len(s["scores"]),
            "last": s["last"],
            "last_price": s["last_price"],
            "runs": len(s["scores"]),
        }
        for sym, s in stats.items()
    }


def fill_outcomes(current_prices: dict[str, float]) -> None:
    """Record what actually happened ~20 trading days after past runs.

    Called on every report: any recommendation 25-60 calendar days old
    without a recorded outcome gets one from today's price. This closes the
    feedback loop (self-evaluation, future weight tuning).
    """
    now = datetime.now(timezone.utc)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT a.run_id, a.symbol, a.price, r.ts FROM analyses a "
            "JOIN runs r ON r.id = a.run_id "
            "LEFT JOIN outcomes o ON o.run_id = a.run_id AND o.symbol = a.symbol "
            "WHERE o.run_id IS NULL AND a.price IS NOT NULL"
        ).fetchall()
        for run_id, symbol, rec_price, ts in rows:
            days = (now - datetime.fromisoformat(ts)).days
            price_now = current_prices.get(symbol)
            if OUTCOME_MIN_DAYS <= days <= OUTCOME_MAX_DAYS and price_now:
                conn.execute(
                    "INSERT OR IGNORE INTO outcomes VALUES (?,?,?,?,?,?)",
                    (run_id, symbol, rec_price, price_now, days,
                     (price_now / rec_price - 1) * 100))


def outcome_summary() -> dict | None:
    """Track record of matured BUY/STRONG BUY recommendations."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT o.ret_pct FROM outcomes o JOIN analyses a "
            "ON a.run_id = o.run_id AND a.symbol = o.symbol "
            "WHERE a.verdict IN ('BUY', 'STRONG BUY')").fetchall()
    if not rows:
        return None
    rets = [r[0] for r in rows]
    return {"n": len(rets), "avg_pct": sum(rets) / len(rets),
            "win_rate": sum(1 for r in rets if r > 0) / len(rets)}


def attach_conviction(result, past: dict[str, dict]) -> None:
    """Set conviction / score_delta / runs_seen on a DeepResult in place."""
    p = past.get(result.symbol)
    if p is None:
        result.conviction = result.composite
        result.score_delta = None
        result.runs_seen = 0
    else:
        result.conviction = ((1 - PAST_WEIGHT) * result.composite
                             + PAST_WEIGHT * p["avg"])
        result.score_delta = result.composite - p["last"]
        result.runs_seen = p["runs"]
