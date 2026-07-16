"""Paper trading: forward-test the tool's picks with virtual money.

Shadow DB (paper.db) - prod portfolio.db is never touched. Every simulated
trade pays realistic costs (brokerage, STT, slippage) so the measured edge
is NET. Mechanical rules, no discretion:
  entry: top picks >= conviction bar, ~1% equity risk per trade via ATR stop
  exit:  stop hit, target hit, verdict decay (<45), or ~20 trading days
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import data
from .universe import to_yahoo

DB_PATH = Path(__file__).resolve().parent.parent / "paper.db"

BROKERAGE = 20.0        # flat per order
STT = 0.00025           # sell side
SLIP = 0.0005           # each side
RISK_FRAC = 0.01        # equity risked per trade
MAX_POS = 8
MAX_ALLOC = 0.20        # max fraction of equity in one position
TIMEOUT_DAYS = 30       # calendar ~ 20 trading days
STCG = 0.20             # short-term capital-gains tax provisioned on realized gains
GAP_WARN_DAYS = 7       # manual runs: warn if stops went unmonitored this long
PHASE_DAYS = 123        # 4-month test phase (go/no-go)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    budget REAL, cash REAL, nifty_start REAL, started TEXT);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY, units REAL, entry REAL, stop REAL,
    target REAL, opened TEXT);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, units REAL,
    entry REAL, exit_price REAL, pnl REAL, reason TEXT,
    opened TEXT, closed TEXT);
CREATE TABLE IF NOT EXISTS equity_log (
    day TEXT PRIMARY KEY, equity REAL);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def account() -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT budget, cash, nifty_start, started "
                           "FROM account WHERE id=1").fetchone()
    if row is None:
        return None
    return {"budget": row[0], "cash": row[1],
            "nifty_start": row[2], "started": row[3]}


def init(budget: float) -> None:
    nifty = data.last_price("^NSEI")
    with _connect() as conn:
        conn.execute("DELETE FROM account")
        conn.execute("DELETE FROM positions")
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM equity_log")
        conn.execute("INSERT INTO account VALUES (1, ?, ?, ?, ?)",
                     (budget, budget, nifty, _now()))


def positions() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT symbol, units, entry, stop, target, "
                            "opened FROM positions").fetchall()
    keys = ("symbol", "units", "entry", "stop", "target", "opened")
    return [dict(zip(keys, r)) for r in rows]


def _sell_net(units: float, px: float) -> float:
    return units * px * (1 - SLIP) * (1 - STT) - BROKERAGE


def _close(conn, pos: dict, px: float, reason: str) -> dict:
    proceeds = _sell_net(pos["units"], px)
    cost = pos["units"] * pos["entry"] * (1 + SLIP) + BROKERAGE
    pnl = proceeds - cost
    conn.execute("INSERT INTO trades (symbol, units, entry, exit_price, pnl,"
                 " reason, opened, closed) VALUES (?,?,?,?,?,?,?,?)",
                 (pos["symbol"], pos["units"], pos["entry"], px, pnl,
                  reason, pos["opened"], _now()))
    conn.execute("DELETE FROM positions WHERE symbol=?", (pos["symbol"],))
    conn.execute("UPDATE account SET cash = cash + ?", (proceeds,))
    return {"symbol": pos["symbol"], "exit": px, "pnl": pnl, "reason": reason}


def process(by_sym: dict, picks: list, stale: bool = False) -> dict:
    """Mark open positions to market (stops/targets vs actual OHLC since
    opened), then enter new picks. Returns {closed, opened, skipped}.

    stale=True (holiday / market data not fresh): defense only — exits
    still run (news can decay a verdict), but no new positions are opened
    on a day without its own closing prices."""
    closed, opened = [], []
    pos_list = positions()

    # ---- exits ----
    if pos_list:
        hist = data.fetch_history([to_yahoo(p["symbol"]) for p in pos_list],
                                  period="3mo")
        with _connect() as conn:
            for pos in pos_list:
                df = data.ohlcv_frame(hist, to_yahoo(pos["symbol"]))
                if df is None:
                    continue
                window = df[df.index > pos["opened"][:10]]
                if window.empty:
                    continue
                last = float(window["Close"].iloc[-1])
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(pos["opened"])).days
                r = by_sym.get(pos["symbol"])
                if float(window["Low"].min()) <= pos["stop"]:
                    closed.append(_close(conn, pos, pos["stop"], "stop"))
                elif float(window["High"].max()) >= pos["target"]:
                    closed.append(_close(conn, pos, pos["target"], "target"))
                elif age >= TIMEOUT_DAYS:
                    closed.append(_close(conn, pos, last, "timeout"))
                elif r is not None and r.composite < 45:
                    closed.append(_close(conn, pos, last, "verdict decay"))

    # ---- entries ----
    if stale:
        return {"closed": closed, "opened": opened}
    acct = account()
    held = {p["symbol"] for p in positions()}
    invested = sum(p["units"] * p["entry"] for p in positions())
    equity = acct["cash"] + invested
    with _connect() as conn:
        for r in picks:
            if len(held) >= MAX_POS or r.symbol in held:
                continue
            plan = r.details.get("plan")
            if not plan or not r.price:
                continue
            risk = r.price - plan["stop"]
            if risk <= 0:
                continue
            units = int(equity * RISK_FRAC / risk)
            max_units = int(equity * MAX_ALLOC / r.price)
            units = min(units, max_units)
            cost = units * r.price * (1 + SLIP) + BROKERAGE
            if units < 1 or cost > acct["cash"]:
                continue
            # net-edge gate: profit at target, after round-trip costs and
            # STCG, must retain >=0.9x the rupees risked at the stop
            # (default 2.5:2.0 ATR plans net ~0.96x; cost-dominated or
            # thin-target trades fall below and are skipped)
            gross = units * (plan["target"] - r.price)
            rt_costs = (2 * BROKERAGE + units * r.price * SLIP
                        + units * plan["target"] * (SLIP + STT))
            if (gross - rt_costs) * (1 - STCG) < 0.9 * units * risk:
                continue
            conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?)",
                         (r.symbol, units, r.price, plan["stop"],
                          plan["target"], _now()))
            conn.execute("UPDATE account SET cash = cash - ?", (cost,))
            acct["cash"] -= cost
            held.add(r.symbol)
            opened.append({"symbol": r.symbol, "units": units,
                           "entry": r.price, "stop": plan["stop"],
                           "target": plan["target"]})
    return {"closed": closed, "opened": opened}


def summary(log: bool = False) -> dict | None:
    """Full paper account state + closed-trade statistics.

    log=True (once per --test run) snapshots today's equity into equity_log,
    the curve behind max-drawdown and the run-gap warning.
    """
    acct = account()
    if acct is None:
        return None
    pos_list = positions()
    prices = {}
    if pos_list:
        hist = data.fetch_history([to_yahoo(p["symbol"]) for p in pos_list],
                                  period="3mo")
        for p in pos_list:
            df = data.ohlcv_frame(hist, to_yahoo(p["symbol"]))
            if df is not None:
                prices[p["symbol"]] = float(df["Close"].iloc[-1])
    open_value = sum(p["units"] * prices.get(p["symbol"], p["entry"])
                     for p in pos_list)
    equity = acct["cash"] + open_value
    today = _now()[:10]
    with _connect() as conn:
        trades = conn.execute(
            "SELECT symbol, units, entry, exit_price, pnl, reason, closed "
            "FROM trades ORDER BY id DESC").fetchall()
        if log:
            conn.execute("INSERT OR REPLACE INTO equity_log VALUES (?, ?)",
                         (today, equity))
        curve_rows = conn.execute(
            "SELECT day, equity FROM equity_log ORDER BY day").fetchall()

    wins = [t for t in trades if t[4] > 0]
    losses = [t for t in trades if t[4] <= 0]
    realized = sum(t[4] for t in trades)
    tax = STCG * max(realized, 0.0)
    net_equity = equity - tax

    curve = [r[1] for r in curve_rows] + ([] if log else [equity])
    peak, max_dd = (curve[0] if curve else equity), 0.0
    for v in curve:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak * 100)

    from datetime import date
    prev_runs = [r[0] for r in curve_rows if r[0] < today]
    gap_days = ((date.today() - date.fromisoformat(prev_runs[-1])).days
                if prev_runs else None)
    n_days = (date.today() - date.fromisoformat(acct["started"][:10])).days

    nifty_now = data.last_price("^NSEI")
    nifty_ret = ((nifty_now / acct["nifty_start"] - 1) * 100
                 if nifty_now and acct.get("nifty_start") else None)
    return {
        "acct": acct, "positions": pos_list, "prices": prices,
        "equity": equity, "return_pct": (equity / acct["budget"] - 1) * 100,
        "realized": realized, "tax": tax,
        "net_return_pct": (net_equity / acct["budget"] - 1) * 100,
        "max_dd": max_dd, "gap_days": gap_days, "n_days": n_days,
        "nifty_ret_pct": nifty_ret, "trades": trades,
        "n_trades": len(trades), "hit_rate": len(wins) / len(trades) if trades else None,
        "avg_win": sum(t[4] for t in wins) / len(wins) if wins else None,
        "avg_loss": sum(t[4] for t in losses) / len(losses) if losses else None,
    }
