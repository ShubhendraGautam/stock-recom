"""stockrec - deep Indian stock research for short-term investing.

    stockrec                     full report: deep scan + recommendations
                                 + your portfolio advice (the main command)
        --universe nifty50|nifty100|midcap150|smallcap250|all
        --top N                       max recommendations (default 8)
        --include-held                also recommend stocks you already own
        --test                        also trade the paper account
    stockrec SYMBOL              deep analysis of one NSE stock
    stockrec buy SYMBOL QTY PRICE    record a purchase
    stockrec sell SYMBOL QTY PRICE   record a sale
    stockrec log                 transaction history
    stockrec test init 100000    create paper-trading account (virtual ₹)
    stockrec test [portfolio]    paper holdings, P&L, hit rate vs NIFTY
    stockrec watch               quick market-hours check vs stops/targets

    Each report also writes order_plan.json (entry/stop/target per pick)
    for GTT orders or an external executor.

Every report is saved; later runs show score changes and rank by
conviction (consistent strength across your past runs).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Windows consoles often default to cp1252, which cannot encode ₹.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import json

from . import data, engine, market, nse, paper, portfolio, predict, research
from . import universe as universe_mod
from .universe import UNIVERSE_NAMES, to_yahoo

PLAN_PATH = "order_plan.json"

console = Console()

WORTH_IT = 55        # only recommend stocks scoring at least this

DISCLAIMER = (
    "Not financial advice. Scores are heuristic, data comes from public "
    "sources and may be delayed. Do your own research before trading."
)

VERDICT_STYLE = {
    "STRONG BUY": "bold green", "S.BUY": "bold green", "BUY": "green",
    "HOLD": "yellow", "WEAK": "dark_orange", "AVOID": "red",
    "BUY MORE": "green", "SELL": "red",
}

SHORT_VERDICT = {"STRONG BUY": "S.BUY"}


def _styled(text: str) -> str:
    style = VERDICT_STYLE.get(text, "white")
    return f"[{style}]{text}[/{style}]"


def _fmt(x, pattern="{:,.1f}", dash="-"):
    return pattern.format(x) if isinstance(x, (int, float)) else dash


# --------------------------------------------------------------------------
# The report (default command)
# --------------------------------------------------------------------------

def _flows_line() -> str | None:
    rows = nse.fii_dii()
    if not rows:
        return None
    parts = []
    when = ""
    for row in rows:
        cat = str(row.get("category", "?")).replace("/FPI", "")
        net = float(row.get("netValue", 0) or 0)
        style = "green" if net >= 0 else "red"
        parts.append(f"{cat} [{style}]{net:+,.0f} cr[/{style}]")
        when = row.get("date", "")
    return f"institutional flows ({when}): " + "   ".join(parts)


def _regime_lines() -> list[str]:
    """Market context: NIFTY trend, India VIX, sector rotation."""
    ctx = market.context()
    lines: list[str] = []

    bits = []
    if ctx.get("nifty") is not None:
        trend = ctx.get("trend")
        style = "green" if trend == "up" else "red"
        bits.append(
            f"NIFTY {ctx['nifty']:,.0f} "
            f"[{style}]{ctx['nifty_vs_200dma_pct']:+.1f}% vs 200DMA[/{style}]"
        )
    if ctx.get("vix") is not None:
        pct = ctx.get("vix_pctile") or 0
        mood = "calm" if pct < 40 else "normal" if pct < 70 else "fearful"
        bits.append(f"India VIX {ctx['vix']:.1f} ({mood}, {pct:.0f}th pctile 1y)")
    if bits:
        lines.append("   ".join(bits))

    rotation = ctx.get("rotation") or {}
    ranked = sorted(
        ((label, e["rs_3m_pct"]) for label, e in rotation.items()
         if e.get("rs_3m_pct") is not None),
        key=lambda kv: kv[1], reverse=True,
    )
    if len(ranked) >= 4:
        lead = "  ".join(f"{lb} [green]{rs:+.0f}%[/green]" for lb, rs in ranked[:3])
        lag = "  ".join(f"{lb} [red]{rs:+.0f}%[/red]" for lb, rs in ranked[-3:])
        lines.append(f"sectors vs NIFTY (3m):  leading {lead}   |   lagging {lag}")
    return lines


def cmd_report(args) -> None:
    symbols = universe_mod.resolve(args.universe)
    if not symbols:
        console.print(f"[red]could not resolve universe '{args.universe}'[/red]")
        sys.exit(1)
    held, sector_weights = engine.portfolio_context()
    last = research.last_run()

    with console.status("[cyan]starting deep scan...[/cyan]") as status:
        say = lambda msg: status.update(f"[cyan]{msg}[/cyan]")  # noqa: E731
        results = engine.deep_scan(symbols, progress=say,
                                   sector_weights=sector_weights)
        by_sym = {r.symbol: r for r in results}
        # holdings outside the scanned universe still need analysis
        for sym in sorted(held - by_sym.keys()):
            say(f"analyzing your holding {sym}...")
            r = engine.analyze_one(sym)
            if r is not None:
                by_sym[sym] = r

    past = research.past_stats()
    for r in by_sym.values():
        research.attach_conviction(r, past)
    # stale run (holiday / EOD files never posted): market data is the
    # previous trading day's, already recorded — saving it again would
    # double-weight that day in conviction and outcome history
    run_id = (None if args.stale
              else research.save_run(args.universe, list(by_sym.values())))

    # ---- header -----------------------------------------------------------
    console.print()
    header = f"[bold]Market report - {date.today():%d %b %Y}[/bold]"
    if last:
        age = last["age_days"]
        ago = "earlier today" if age < 1 else f"{age:.0f} day(s) ago"
        header += f"   [dim](previous run: {ago})[/dim]"
    else:
        header += "   [dim](first run - conviction builds from your next run)[/dim]"
    console.print(header)
    for line in _regime_lines():
        console.print(line)
    flows = _flows_line()
    if flows:
        console.print(flows)
    console.print()

    # ---- recommendations --------------------------------------------------
    ranked = sorted(results, key=lambda r: r.conviction, reverse=True)
    if not args.include_held:
        ranked = [r for r in ranked if r.symbol not in held]
    banned = nse.fo_ban_list()
    for r in ranked:
        if r.symbol in banned:
            r.notes.append("in F&O ban period (surveillance) - excluded")
    picks = [r for r in ranked
             if r.conviction >= WORTH_IT and r.symbol not in banned][: args.top]

    if picks:
        table = Table(
            title=f"Worth buying now - {args.universe.upper()}, "
                  "weeks-to-months horizon"
        )
        table.add_column("#", justify="right")
        table.add_column("Symbol", style="bold cyan", no_wrap=True)
        table.add_column("Price ₹", justify="right", no_wrap=True)
        table.add_column("Conv", justify="right")
        table.add_column("Δ", justify="right")
        table.add_column("T/P/R/F/Q/S/I", justify="center", no_wrap=True)
        table.add_column("Verdict", no_wrap=True)

        def comp(r, name):
            v = r.components.get(name)
            return "--" if v is None else f"{v:.0f}"

        for i, r in enumerate(picks, 1):
            delta = ("[dim]new[/dim]" if r.score_delta is None
                     else f"{round(r.score_delta):+d}")
            verdict = engine.verdict_of(r.conviction)
            table.add_row(
                str(i), r.symbol, _fmt(r.price, "{:,.0f}"),
                f"{r.conviction:.0f}", delta,
                "/".join(comp(r, n) for n in
                         ("technical", "prediction", "risk", "fundamental",
                          "quality", "sentiment", "institutional")),
                _styled(SHORT_VERDICT.get(verdict, verdict)),
            )
        console.print(table)
        console.print(
            "[dim]Conv = conviction (today blended with your past runs)   "
            "Δ = change vs previous run   P = empirical 20d analog score[/dim]"
        )
        noted = [r for r in picks if r.notes]
        for r in noted:
            console.print(f"[dim]note {r.symbol}: {'; '.join(r.notes)}[/dim]")

        console.print("\n[bold]20-day trade plans[/bold] "
                      "[dim](from 5y historical analogs + 2xATR stops)[/dim]")
        for r in picks:
            a, plan = r.details.get("analog"), r.details.get("plan")
            bits = [f"  [bold cyan]{r.symbol}[/bold cyan]:"]
            if a:
                ws = "green" if a["win_rate"] >= 0.55 else \
                     "yellow" if a["win_rate"] >= 0.45 else "red"
                bits.append(
                    f"[{ws}]{a['win_rate']:.0%} win[/{ws}] over 20d "
                    f"(n={a['n']}), median {a['median_pct']:+.1f}%, "
                    f"worst-5% {a['p5_pct']:+.1f}%"
                )
            if plan:
                bits.append(
                    f"| stop ₹{plan['stop']:,.0f} ({plan['stop_pct']:+.1f}%) "
                    f"target ₹{plan['target']:,.0f} ({plan['target_pct']:+.1f}%) "
                    f"RR {plan['rr']:.1f}"
                )
            if len(bits) > 1:
                console.print(" ".join(bits))
    else:
        console.print(
            f"[yellow]No stock clears the bar today (conviction ≥ {WORTH_IT}). "
            "Holding cash is a position too.[/yellow]"
        )
    if held and not args.include_held:
        shown = sorted(held & set(symbols))
        if shown:
            console.print(
                f"[dim]your holdings ({', '.join(shown)}) are ranked under "
                "'Your portfolio' below, not here[/dim]"
            )

    # ---- order plan (machine-readable, for GTT orders / future executor) --
    # order_plan.json = latest; plans/YYYY-MM-DD.json = per-day archive
    # (newest 30 kept — a stale plan is not actionable anyway)
    plan_doc = {
        "generated": f"{date.today()}",
        "picks": [{
            "symbol": r.symbol, "price": r.price,
            "conviction": round(r.conviction, 1),
            "stop": round(r.details["plan"]["stop"], 2)
                    if r.details.get("plan") else None,
            "target": round(r.details["plan"]["target"], 2)
                      if r.details.get("plan") else None,
        } for r in picks],
    }
    try:
        plans_dir = Path("plans")
        plans_dir.mkdir(exist_ok=True)
        for path in (Path(PLAN_PATH), plans_dir / f"{date.today()}.json"):
            path.write_text(json.dumps(plan_doc, indent=1), encoding="utf-8")
        old = sorted(plans_dir.glob("2*.json"))[:-30]
        for p in old:
            p.unlink()
    except OSError:
        pass

    # ---- paper trading ----------------------------------------------------
    if args.test:
        _paper_run(by_sym, picks, args.stale)

    # ---- portfolio --------------------------------------------------------
    console.print()
    _print_portfolio(by_sym)

    # ---- outcome self-tracking -------------------------------------------
    research.fill_outcomes({s: r.price for s, r in by_sym.items() if r.price})
    oc = research.outcome_summary()
    if oc:
        console.print(
            f"\n[dim]track record: {oc['n']} matured BUY recommendations, "
            f"avg {oc['avg_pct']:+.1f}% after ~1 month, "
            f"win rate {oc['win_rate']:.0%}[/dim]")

    ic_line = predict.ic_summary(predict.load_dataset())
    if ic_line:
        console.print(f"\n[dim]{ic_line}[/dim]")
    console.print(
        f"[dim]stale data - defense-only run, not saved to history[/dim]"
        if run_id is None else
        f"[dim]run #{run_id} saved ({len(by_sym)} stocks analyzed) - "
        f"future runs will use it for conviction[/dim]"
    )
    console.print(f"[dim]{DISCLAIMER}[/dim]")


def _print_portfolio(analyses: dict[str, engine.DeepResult]) -> None:
    held = portfolio.holdings()
    if not held:
        console.print("[dim]portfolio empty - record purchases with: "
                      "stockrec buy SYMBOL QTY PRICE[/dim]")
        return

    table = Table(title="Your portfolio")
    table.add_column("Symbol", style="bold cyan", no_wrap=True)
    table.add_column("Qty", justify="right")
    table.add_column("Avg ₹", justify="right", no_wrap=True)
    table.add_column("Now ₹", justify="right", no_wrap=True)
    table.add_column("Value ₹", justify="right", no_wrap=True)
    table.add_column("P&L %", justify="right", no_wrap=True)
    table.add_column("Score", justify="right")
    table.add_column("Advice", no_wrap=True)

    total_invested = total_value = 0.0
    reasons: list[str] = []
    for sym, h in sorted(held.items()):
        r = analyses.get(sym)
        price = r.price if r else data.last_price(to_yahoo(sym))
        value = h.units * price if price else None
        pnl_pct = (price / h.avg_price - 1) * 100 if price else 0.0
        score = r.composite if r else 50.0
        action, reason = portfolio.advise(score, pnl_pct)
        reasons.append(f"[bold]{sym}[/bold]: {action} - {reason}")

        total_invested += h.invested
        total_value += value or h.invested
        pnl_style = "green" if pnl_pct >= 0 else "red"
        table.add_row(
            sym, f"{h.units:g}", f"{h.avg_price:,.2f}",
            _fmt(price, "{:,.2f}"), _fmt(value, "{:,.0f}"),
            f"[{pnl_style}]{pnl_pct:+.1f}[/{pnl_style}]",
            f"{score:.0f}", _styled(action),
        )

    console.print(table)
    total_pnl = total_value - total_invested
    pnl_style = "green" if total_pnl >= 0 else "red"
    console.print(
        f"invested ₹{total_invested:,.0f}   value ₹{total_value:,.0f}   "
        f"P&L [{pnl_style}]₹{total_pnl:+,.0f} "
        f"({total_pnl / total_invested * 100 if total_invested else 0:+.1f}%)[/{pnl_style}]\n"
    )
    for reason in reasons:
        console.print(f"  {reason}")

    _, sector_weights = engine.portfolio_context()
    if sector_weights:
        alloc = "   ".join(
            f"{sec} {w:.0%}" for sec, w in
            sorted(sector_weights.items(), key=lambda kv: -kv[1])
        )
        console.print(f"\nsector allocation: {alloc}")
        heavy = [s for s, w in sector_weights.items() if w >= 0.30]
        if heavy:
            console.print(
                f"[yellow]concentration:[/yellow] {', '.join(heavy)} ≥30% of "
                "portfolio - new picks in these sectors are penalized"
            )


# --------------------------------------------------------------------------
# Paper trading
# --------------------------------------------------------------------------

def _paper_run(by_sym, picks, stale: bool = False) -> None:
    if paper.account() is None:
        console.print("\n[yellow]no paper account - create one with: "
                      "stockrec test init 100000[/yellow]")
        return
    events = paper.process(by_sym, picks, stale=stale)
    console.print("\n[bold]Paper trading[/bold] [dim](virtual money, "
                  "real costs: brokerage+STT+slippage)[/dim]")
    for e in events["closed"]:
        style = "green" if e["pnl"] >= 0 else "red"
        console.print(f"  closed {e['symbol']} @ ₹{e['exit']:,.2f} "
                      f"({e['reason']}) [{style}]P&L ₹{e['pnl']:+,.0f}[/{style}]")
    for e in events["opened"]:
        console.print(f"  opened {e['symbol']}: {e['units']} u @ "
                      f"₹{e['entry']:,.2f}  stop ₹{e['stop']:,.0f} "
                      f"target ₹{e['target']:,.0f}")
    if not events["closed"] and not events["opened"]:
        console.print("  [dim]no paper trades this run[/dim]")
    s = paper.summary(log=True)
    if s["gap_days"] is not None and s["gap_days"] > paper.GAP_WARN_DAYS:
        console.print(f"  [yellow]{s['gap_days']} days since the last paper "
                      "run - stops went unmonitored; run more often[/yellow]")
    console.print(f"  equity ₹{s['equity']:,.0f} ({s['return_pct']:+.2f}%, "
                  f"net of 20% STCG {s['net_return_pct']:+.2f}%)"
                  + (f"  vs NIFTY {s['nifty_ret_pct']:+.2f}%"
                     if s['nifty_ret_pct'] is not None else ""))


def cmd_test(rest: list[str]) -> None:
    sub = rest[0].lower() if rest else "portfolio"
    if sub == "init":
        try:
            budget = float(rest[1])
        except (IndexError, ValueError):
            console.print("[red]usage: stockrec test init BUDGET[/red]")
            sys.exit(2)
        paper.init(budget)
        console.print(f"[green]paper account created:[/green] ₹{budget:,.0f} "
                      "- run reports with --test to trade it")
        return
    s = paper.summary()
    if s is None:
        console.print("no paper account - run: stockrec test init 100000")
        return
    if sub == "reset":
        paper.init(s["acct"]["budget"])
        console.print("paper account reset")
        return
    # portfolio view
    table = Table(title=f"Paper portfolio (started {s['acct']['started'][:10]})")
    for col in ("Symbol", "Units", "Entry ₹", "Now ₹", "Stop ₹",
                "Target ₹", "P&L %"):
        table.add_column(col, justify="right", no_wrap=True)
    for p in s["positions"]:
        px = s["prices"].get(p["symbol"])
        pnl = (px / p["entry"] - 1) * 100 if px else 0.0
        style = "green" if pnl >= 0 else "red"
        table.add_row(p["symbol"], f"{p['units']:g}", f"{p['entry']:,.2f}",
                      _fmt(px, "{:,.2f}"), f"{p['stop']:,.0f}",
                      f"{p['target']:,.0f}",
                      f"[{style}]{pnl:+.1f}[/{style}]")
    console.print(table)
    console.print(
        f"cash ₹{s['acct']['cash']:,.0f}   equity ₹{s['equity']:,.0f}   "
        f"return {s['return_pct']:+.2f}%"
        + (f"   NIFTY same period {s['nifty_ret_pct']:+.2f}%"
           if s['nifty_ret_pct'] is not None else ""))
    if s["n_trades"]:
        console.print(
            f"closed trades: {s['n_trades']}   hit rate {s['hit_rate']:.0%}   "
            f"avg win ₹{s['avg_win'] or 0:+,.0f}   "
            f"avg loss ₹{s['avg_loss'] or 0:+,.0f}   "
            f"tax provision ₹{s['tax']:,.0f}")

    # 4-month test phase: fixed go/no-go criteria (PLAN.md) - never relaxed
    def _mark(ok: bool) -> str:
        return "[green]✓[/green]" if ok else "[red]✗[/red]"
    beat = (s["nifty_ret_pct"] is not None
            and s["net_return_pct"] > s["nifty_ret_pct"])
    console.print(
        f"\n[bold]Test phase[/bold] day {s['n_days']}/{paper.PHASE_DAYS}   "
        f"go/no-go: {_mark(s['n_trades'] >= 30)} 30+ trades "
        f"({s['n_trades']})   {_mark(beat)} beat NIFTY net of tax   "
        f"{_mark(s['max_dd'] <= 10)} max drawdown ≤10% ({s['max_dd']:.1f}%)")
    if s["n_trades"] < 30:
        console.print("[dim]judge nothing before ~30 closed trades[/dim]")


def cmd_watch() -> None:
    """Light market-hours check: holdings + paper positions vs plan, VIX."""
    plan = {}
    try:
        with open(PLAN_PATH, encoding="utf-8") as f:
            plan = {p["symbol"]: p for p in json.load(f).get("picks", [])}
    except (OSError, json.JSONDecodeError):
        pass
    watchlist: dict[str, dict] = {}
    for sym in portfolio.holdings():
        watchlist[sym] = {}
    for p in paper.positions():
        watchlist[p["symbol"]] = {"stop": p["stop"], "target": p["target"]}
    for sym, p in plan.items():
        watchlist.setdefault(sym, {"entry": p.get("price"), "stop": p.get("stop")})
    if not watchlist:
        console.print("nothing to watch (no holdings, paper positions, or plan)")
        return
    tickers = [to_yahoo(s) for s in watchlist] + ["^INDIAVIX"]
    hist = data.fetch_history(tickers, period="3mo")
    alerts = 0
    for sym, w in sorted(watchlist.items()):
        df = data.ohlcv_frame(hist, to_yahoo(sym))
        if df is None or len(df) < 2:
            continue
        last, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
        chg = (last / prev - 1) * 100
        flags = []
        if abs(chg) >= 2.5:
            flags.append(f"[red]moved {chg:+.1f}% today[/red]")
        if w.get("stop") and last <= w["stop"]:
            flags.append(f"[red]below stop ₹{w['stop']:,.0f}[/red]")
        if w.get("target") and last >= w["target"]:
            flags.append(f"[green]target ₹{w['target']:,.0f} hit[/green]")
        if flags:
            alerts += 1
            console.print(f"  [bold]{sym}[/bold] ₹{last:,.2f}: " + ", ".join(flags))
        else:
            console.print(f"  [dim]{sym} ₹{last:,.2f} ({chg:+.1f}%) ok[/dim]")
    vix = data.close_series(hist, "^INDIAVIX")
    if vix is not None and len(vix) >= 2:
        vchg = (float(vix.iloc[-1]) / float(vix.iloc[-2]) - 1) * 100
        style = "red" if vchg > 8 else "dim"
        console.print(f"  [{style}]India VIX {float(vix.iloc[-1]):.1f} "
                      f"({vchg:+.1f}%)[/{style}]")
    if alerts == 0:
        console.print("[green]no alerts[/green]")


# --------------------------------------------------------------------------
# Single-stock deep dive
# --------------------------------------------------------------------------

def cmd_analyze(symbol: str) -> None:
    with console.status(f"[cyan]deep-analyzing {symbol.upper()}...[/cyan]"):
        s = engine.analyze_one(symbol)
    if s is None:
        console.print(f"[red]no data for '{symbol}' - is it a valid NSE symbol?[/red]")
        sys.exit(1)
    research.attach_conviction(s, research.past_stats())

    d = s.details
    c = s.components

    def sc(name):
        v = c.get(name)
        return "--" if v is None else f"{v:.0f}"

    trend_line = ""
    if s.runs_seen:
        trend_line = (f"   [dim]seen in {s.runs_seen} past run(s), "
                      f"score change {_fmt(s.score_delta, '{:+.0f}')} "
                      f"since last[/dim]")

    lines = [
        f"[bold]{d.get('name', s.symbol)}[/bold]  ({s.symbol})   "
        f"sector: {d.get('sector') or '-'}{trend_line}",
        "",
        "[bold]Trend & momentum[/bold]",
        f"  Price ₹{_fmt(s.price, '{:,.2f}')}    50DMA ₹{_fmt(d.get('sma50'), '{:,.0f}')}"
        f"    200DMA ₹{_fmt(d.get('sma200'), '{:,.0f}')}"
        f"    52w position {_fmt(d.get('pos_52w_pct'), '{:.0f}')}%",
        f"  RSI {_fmt(d.get('rsi'), '{:.0f}')}    ADX {_fmt(d.get('adx'), '{:.0f}')}"
        f"    MACD hist {_fmt(d.get('macd_hist_pct'), '{:+.2f}')}%"
        f"    Boll %B {_fmt(d.get('pct_b'), '{:.2f}')}"
        f"    ATR {_fmt(d.get('atr_pct'), '{:.1f}')}%",
        f"  Momentum 3m {_fmt(d.get('mom_3m_pct'), '{:+.1f}')}%"
        f"   6m {_fmt(d.get('mom_6m_pct'), '{:+.1f}')}%"
        f"    OBV {'accumulating' if d.get('obv_accum') else 'distributing'}"
        f"    volume 20d/90d x{_fmt(d.get('vol_ratio'), '{:.2f}')}",
        f"  Sector ({d.get('sector_index') or '-'}) vs NIFTY 3m: "
        f"{_fmt(d.get('sector_rs_pct'), '{:+.1f}')}%"
        + ("  [green](tailwind)[/green]" if (d.get('sector_rs_pct') or 0) > 2
           else "  [red](headwind)[/red]" if (d.get('sector_rs_pct') or 0) < -2
           else ""),
        "",
        "[bold]Risk (2y daily, vs NIFTY)[/bold]",
        f"  Return {_fmt(d.get('ann_return_pct'), '{:+.1f}')}%/y"
        f"    vol {_fmt(d.get('ann_vol_pct'), '{:.1f}')}%"
        f"    Sharpe {_fmt(d.get('sharpe'), '{:.2f}')}"
        f"    Sortino {_fmt(d.get('sortino'), '{:.2f}')}"
        f"    max drawdown {_fmt(d.get('max_dd_pct'), '{:.1f}')}%",
        f"  Beta {_fmt(d.get('beta'), '{:.2f}')}"
        f"    alpha {_fmt(d.get('alpha_pct'), '{:+.1f}')}%/y",
        f"  Monte Carlo (1y, 4,000 paths): P(gain) {_fmt(d.get('mc_p_gain'), '{:.0%}')}"
        f"    median {_fmt(d.get('mc_median_pct'), '{:+.1f}')}%"
        f"    5% VaR {_fmt(d.get('mc_var5_pct'), '{:.1f}')}%",
        "",
        "[bold]Empirical 20-day outlook[/bold] [dim](nearest historical "
        "analogs, 5y universe dataset)[/dim]",
    ]
    a, plan = d.get("analog"), d.get("plan")
    if a:
        lines.append(
            f"  {a['n']} similar setups: win {a['win_rate']:.0%}"
            f"    median {a['median_pct']:+.1f}%"
            f"    mean {a['mean_pct']:+.1f}%"
            f"    worst-5% {a['p5_pct']:+.1f}%"
            f"    best-5% {a['p95_pct']:+.1f}%"
        )
    else:
        lines.append("  [dim]no analog dataset yet - run a full report "
                     "(stockrec) first to build it[/dim]")
    if plan:
        lines.append(
            f"  Trade plan: stop ₹{plan['stop']:,.2f} ({plan['stop_pct']:+.1f}%)"
            f"    target ₹{plan['target']:,.2f} ({plan['target_pct']:+.1f}%)"
            f"    risk:reward {plan['rr']:.1f}"
        )
    lines += [
        "",
        "[bold]Fundamentals & quality[/bold]",
        f"  P/E {_fmt(d.get('pe'))}    ROE {_fmt(d.get('roe'), '{:.1%}')}"
        f"    D/E {_fmt(d.get('debt_to_equity'), '{:.0f}')}%"
        f"    margins {_fmt(d.get('margins'), '{:.1%}')}",
        f"  Piotroski F-Score {d.get('f_score') if d.get('f_score') is not None else '--'}/9"
        f"    Altman Z {_fmt(d.get('altman_z'), '{:.2f}')}"
        f" {('(' + d['altman_note'] + ')') if d.get('altman_note') else ''}",
        f"  Revenue CAGR {_fmt(d.get('rev_cagr'), '{:+.1%}')}"
        f"    profit CAGR {_fmt(d.get('ni_cagr'), '{:+.1%}')}"
        f"    cash conversion {_fmt(d.get('cash_conversion'), '{:.2f}')}"
        f"    margin trend {_fmt(d.get('margin_trend_pp'), '{:+.1f}')}pp",
        "",
        "[bold]Institutional & street[/bold]",
        f"  NSE delivery {_fmt(d.get('delivery_pct'), '{:.0f}')}%"
        f"    analyst: {d.get('recommendation') or '-'}"
        f"    target upside {_fmt(d.get('target_upside_pct'), '{:+.1f}')}%",
        f"  Derivatives: {d.get('fo_buildup') or 'not F&O-listed'}"
        + (f"    fut OI {_fmt(d.get('fo_oi_chg_pct'), '{:+.1f}')}%"
           if d.get('fo_oi_chg_pct') is not None else "")
        + (f"    PCR {_fmt(d.get('fo_pcr'), '{:.2f}')}"
           if d.get('fo_pcr') is not None else ""),
    ]
    if d.get("bulk_net_qty") is not None:
        side = "net BUY" if d["bulk_net_qty"] >= 0 else "net SELL"
        top = d.get("bulk_deals") or []
        who = f" (top: {top[0]['client'][:40]})" if top else ""
        lines.append(
            f"  Bulk/block deals: {side} {abs(d['bulk_net_qty']):,.0f} shares{who}")
    lines += [
        "",
        f"[bold]Scores[/bold]  tech {sc('technical')} | predict {sc('prediction')}"
        f" | risk {sc('risk')}"
        f" | fund {sc('fundamental')} | quality {sc('quality')}"
        f" | sentiment {sc('sentiment')} ({d.get('headlines', 0)} headlines)"
        f" | institutional {sc('institutional')}",
        f"[bold]Composite  {s.composite:.0f}/100[/bold]  ->  {_styled(s.verdict)}",
    ]
    titles = d.get("headline_titles") or []
    if titles:
        lines += ["", "[bold]Recent headlines:[/bold]"]
        lines += [f"  • {t}" for t in titles]

    console.print(Panel("\n".join(lines), title=f"Deep analysis: {s.symbol}"))
    console.print(f"[dim]{DISCLAIMER}[/dim]")


# --------------------------------------------------------------------------
# Trades & log
# --------------------------------------------------------------------------

def cmd_trade(action: str, rest: list[str]) -> None:
    try:
        symbol, units, price = rest[0], float(rest[1]), float(rest[2])
    except (IndexError, ValueError):
        console.print(f"[red]usage: stockrec {action.lower()} SYMBOL QTY PRICE[/red]")
        sys.exit(2)
    try:
        portfolio.record(symbol, action, units, price)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    console.print(
        f"[green]recorded:[/green] {action} {units:g} x {symbol.upper()} "
        f"@ ₹{price:,.2f}"
    )


def cmd_log() -> None:
    rows = portfolio.transactions()
    if not rows:
        console.print("no transactions yet")
        return
    table = Table(title="Transaction history")
    for col in ("ID", "Symbol", "Action", "Qty", "Price ₹", "When (UTC)"):
        table.add_column(col)
    for id_, sym, action, units, price, ts in rows:
        table.add_row(str(id_), sym, _styled(action) if action == "SELL" else action,
                      f"{units:g}", f"{price:,.2f}", ts)
    console.print(table)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

USAGE = __doc__


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        if argv and argv[0].lower() in ("-h", "--help", "help"):
            console.print(USAGE)
        elif argv and argv[0].lower() in ("buy", "sell"):
            cmd_trade(argv[0].upper(), argv[1:])
        elif argv and argv[0].lower() == "log":
            cmd_log()
        elif argv and argv[0].lower() == "test" and (
                len(argv) == 1 or argv[1].lower() in ("init", "reset", "portfolio")):
            cmd_test(argv[1:])
        elif argv and argv[0].lower() == "watch":
            cmd_watch()
        elif argv and not argv[0].startswith("-"):
            cmd_analyze(argv[0])
        else:
            p = argparse.ArgumentParser(prog="stockrec", add_help=False)
            p.add_argument("--universe", choices=UNIVERSE_NAMES,
                           default="nifty100")
            p.add_argument("--top", type=int, default=8)
            p.add_argument("--include-held", action="store_true")
            p.add_argument("--test", action="store_true",
                           help="also run the paper-trading account")
            p.add_argument("--stale", action="store_true",
                           help="market data is not today's (holiday): "
                                "manage exits only, open nothing new")
            cmd_report(p.parse_args(argv))
    except KeyboardInterrupt:
        console.print("\n[dim]cancelled[/dim]")
        sys.exit(130)


if __name__ == "__main__":
    main()
