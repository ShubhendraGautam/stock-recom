# stockrec

Deep Indian stock research for **short-term investing** (weeks to months,
never intraday). Built to be run on sparse days: every run is a full deep
analysis, results are saved, and later runs rank by **conviction** — stocks
that stay strong across your runs beat one-day wonders.

## Usage — one main command

```
stockrec                       # THE command: deep scan + recommendations
                               #   + your portfolio advice, all in one report
   --universe nifty50|nifty100 #   (default nifty100)
   --top N                     #   max recommendations (default 8)
   --include-held              #   also recommend stocks you already own

stockrec TCS                   # deep analysis of one NSE symbol
stockrec buy RELIANCE 10 2950  # record a purchase (qty, price)
stockrec sell RELIANCE 5 3100  # record a sale
stockrec log                   # transaction history
```

(`stockrec` = the `stockrec.cmd` wrapper in this folder.)

## Setup

```
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## What a report does

1. **Deep-analyzes every stock** in the universe (plus your holdings), each
   component scored 0-100:
   - **Technical (18%)** — 50/200-DMA trend, MACD, ADX, RSI, Bollinger %B,
     ATR, OBV accumulation, volume expansion, momentum, sector-rotation
     tailwind (±5).
   - **Prediction (20%)** — empirical 20-day analog forecast: today's
     factor state is matched to its 250 nearest historical states in a
     5-year walk-forward dataset (~9-19k samples); their ACTUAL forward
     returns give a win rate, median outcome, and tail risk.
   - **Risk (10%)** — Sharpe, Sortino, max drawdown, beta/alpha vs NIFTY,
     Monte Carlo bootstrap of 1-year returns.
   - **Fundamentals (12%)** — P/E, ROE, debt/equity, margins, growth.
   - **Quality (14%)** — from actual annual statements: Piotroski F-Score,
     Altman Z-Score, revenue/profit CAGR, cash conversion, margin trend.
   - **Sentiment (13%)** — VADER over Google News + Yahoo headlines,
     recency-weighted (7-day half-life).
   - **Institutional (13%)** — NSE delivery %, analyst target upside,
     F&O positioning (OI buildup, put-call ratio), bulk/block deals.
2. **Prints a 20-day trade plan per pick** — analog win rate and outcome
   distribution, plus a 2×ATR stop, target, and risk-reward, because at a
   days-to-weeks holding pace exits matter as much as entries.
3. **Validates its own signals** — the report footer shows each factor's
   walk-forward information coefficient in this universe, so you can see
   what is (and isn't) predictive. (Notably: 3m momentum has been mildly
   NEGATIVE at the 20d horizon in large-cap India - the analog model
   accounts for this.)
4. **Saves the run** to SQLite and blends today's score with your past runs
   into a conviction rank (75% today / 25% history), showing each pick's
   score change since your previous run.
5. **Recommends only what clears the bar** (conviction ≥ 55). If nothing
   does, it says so — no padding with mediocre picks.
6. **Respects your portfolio** — held stocks are advised on (BUY MORE /
   HOLD / SELL with reasons), not re-recommended; sectors ≥30% of your
   portfolio value penalize new picks; sector allocation is shown.
7. Shows **market regime + FII/DII flows** for context.

## Data sources (all free / open / official)

| Source | Provides |
|---|---|
| Yahoo Finance (yfinance) | prices, fundamentals, annual statements, analyst targets, news |
| Yahoo index data | India VIX (fear gauge), NIFTY sectoral indices for sector rotation |
| Google News RSS | broad Indian news with timestamps for recency-weighted sentiment |
| NSE equity bhavcopy | official daily delivery % for every symbol |
| NSE F&O bhavcopy | futures open-interest buildup + put/call ratio per F&O stock |
| NSE bulk/block deals | big-ticket negotiated trades (smart-money footprints) |
| NSE site API | FII/DII daily cash-market flows |

### Signals derived from them

- **Market regime** (report header): NIFTY vs its 200DMA, India VIX with
  1-year percentile, 3-month sector rotation vs NIFTY, FII/DII flows.
- **Sector tailwind**: each stock's technical score is adjusted (±5) by its
  sector index's 3-month relative strength vs NIFTY.
- **Derivatives positioning** (institutional score): futures OI change
  classified as long buildup / short buildup / short covering / long
  unwinding; options put-call ratio extremes.
- **Bulk/block deals** (institutional score): net big-money buy/sell on the
  latest trading day, with the top counterparty shown in `stockrec SYMBOL`.

## Files

- `portfolio.db` — your transactions + all saved analysis runs (SQLite)
- `.cache/` — market-data cache, safe to delete (info/news 4h, delivery 6h,
  statements 24h)
- `stockrec/universe.py` — NIFTY 50/Next-50 symbol lists; index constituents
  change a few times a year, edit freely (Tata Motors = TMPV/TMCV
  post-demerger)

A full deep run takes a few minutes cold (network-bound); re-runs the same
day are much faster thanks to the cache.

> **Disclaimer:** not financial advice. Scores are heuristics over
> possibly-delayed public data. Do your own research.
