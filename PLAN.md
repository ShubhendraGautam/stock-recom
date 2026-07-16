# stockrec — future plan

**Status (16 Jul 2026):** DONE: items 1 (paper trader + costs), 2
(incremental dataset), 3 (research.db split + outcomes), 4 (midcap150/
smallcap250/all universes + ₹5cr liquidity gate), 5 (watch mode +
order_plan.json; Pi/BSE-feed parts pending), 8 partially (F&O ban-list
exclusion; ASM/GSM + pledge + divergence detectors pending).
REMAINING: 6 (ML ladder), 7 partially (costs done; tax provision reporting
pending), 9 (weight tuning from outcomes).

## 4-month testing phase (Jul 16 → Nov 16, 2026) — go/no-go for real money

Success criteria, fixed NOW so goalposts can't move later:
- ≥ 30 closed paper trades (~2 months in; 4 months gives 60+).
- Paper equity beats NIFTY over the same window, NET of costs + 20% STCG.
- Max drawdown ≤ 10% of budget; hit_rate × avg_win vs avg_loss stable
  across months (an edge that only existed in month 1 is noise).
- Zero missed runs due to crashes (reliability is a flagship feature).
If criteria fail: iterate and restart the clock — do not deploy anyway.

Month 1 — discipline + finish measurement:
- Hosted on GitHub Actions (Oracle VM dropped — needs card details).
  Private repo IS the storage: each run commits the DBs + plans/ back,
  so git history doubles as backup. Mon-Fri 18:00 IST cron + 22:30
  fallback trigger; waits for today's NSE EOD files before analyzing
  (deadline 21:30 → defense-only --stale run: exits allowed, no new
  entries, no duplicate history). See deploy/README.md.
  [workflow DONE 16 Jul; repo creation + secrets by user]
- The tool still logs an equity snapshot per --test run (equity_log) and
  warns when the gap between logged runs exceeds a week — now a crashed-
  scheduler alarm rather than a laptop-discipline nudge. [DONE 16 Jul]
- Item 7 done 16 Jul: 20% STCG provision, max drawdown from the equity
  curve, and go/no-go progress line in `stockrec test`.
- Pull a backups/ folder off the VM to the laptop occasionally.

Month 2 — self-measurement (outcomes mature at ~25 days):
- Item 9: tune component weights from live track record (walk-forward).
- Re-measure factor ICs per universe (midcap/smallcap momentum likely
  flips sign vs NIFTY50 mean-reversion).
- Optional A/B: second paper account with different universe/weights.

Month 3 — item 6 ML ladder:
- Regularized logistic on factors → LightGBM, purged walk-forward CV;
  promote only what beats the analog baseline out-of-sample.

Month 4 — hardening + deployment prereqs:
- Item 8: ASM/GSM surveillance lists, promoter pledge, delivery-divergence
  detector (mandatory before real small-cap money).
- Broker integration research: Zerodha Kite Connect (GTT orders from
  order_plan.json) — build the executor only if criteria pass.
- Pi/always-on watch + phone notifications.

---

Agreed roadmap from discussion on 16 Jul 2026. Nothing here is implemented
yet; items are ordered by expected value.

## 1. Paper-trading test runner (`--test`) — build first

Forward-testing on virtual money: stronger evidence than any backtest
(cannot peek at the future), and the gate for investing real money.

- `stockrec test init 100000` — allocate a virtual budget.
- `stockrec --test` — same report the user sees, but additionally executes
  a mechanical paper strategy:
  1. Mark existing paper positions to market. Check stop/target hits
     against actual daily highs/lows since the last run (positions can be
     stopped out "between" sparse runs — no cheating).
  2. Exit rules: stop hit, target hit, verdict decays below HOLD, or
     20-trading-day timeout.
  3. Entry rules: top picks above conviction bar, position size from the
     ATR stop so each trade risks ~1% of budget; respect sector caps.
- Shadow DBs with the same schema (`paper_portfolio.db`,
  `paper_research.db`) — prod DBs never touched.
- `stockrec --test portfolio` — paper holdings, realized + unrealized P&L,
  hit rate, avg win vs avg loss, max drawdown, and return vs NIFTY over
  the same period (the real benchmark).
- Confidence rule: judge nothing before ~30 closed paper trades
  (~2-3 months at 20 runs/month). Small samples lie.

## 2. Incremental analog dataset (kill the 5y re-download)

Decision: keep 5 years of history for the analog/walk-forward model (it
needs multiple regimes; 1y would overfit the current mood and cut samples
~5x). But stop rebuilding it from scratch every run:

- Persist the walk-forward dataset once built.
- Each run: download only ~1y (enough for current indicators), append new
  weekly samples to the dataset incrementally.
- Full 5y refresh only when the stored dataset is stale (monthly) or the
  universe changed.
- Expected effect: ~80% less download per run, identical model quality.

## 3. DB layout: three tiers by preciousness (not "distributed")

Single-file SQLite per tier is right for a single-user CLI; the win is
separation, so caches can be wiped without touching trades and backups
stay tiny.

- `portfolio.db` — user transactions only. Precious. Back up.
- `research.db` — run outcomes: scores, verdicts, prices at run time,
  PLUS (new) recommendation outcomes: the actual 20d return after each
  past recommendation, filled in by later runs. This closes the feedback
  loop and later enables evidence-based re-weighting of components.
- `.cache/` + analog dataset — disposable, rebuildable, never backed up.
- Raw historical price data stays OUT of all DBs (cache only) — DBs store
  outcomes of runs, not market-data slop.

## 4. Mid & small cap universes

- Add `midcap150` and `smallcap250` universes; prefer fetching constituent
  lists from NSE's official index CSVs over hardcoding (small-cap index
  churn is high).
- Mandatory guardrails that large caps didn't need:
  - Liquidity gate: minimum median daily traded value (e.g. ₹5-10 cr);
    skip anything the user's order size would move.
  - Wider ATR stops + smaller position sizing (fatter tails).
  - Expect missing signals: no F&O data (only ~180 stocks have it),
    unreliable delivery %, thin news coverage → components must degrade
    to None (weights renormalize) rather than fake neutral scores.
- Research note: momentum ICs should be re-measured per universe — the
  mild 20d mean-reversion found in NIFTY 50 large caps likely flips to
  positive momentum in small caps.

## 5. Shock handling + `watch` mode

The fastest reputable source is price itself; free news is structurally
minutes-to-hours late and institutional algos cannot be out-read.

- `stockrec watch` — lightweight (seconds, quotes + India VIX only, no deep
  scan): alert on >2.5σ moves in holdings, VIX spikes, gap opens vs stops.
  Cron-able every 15 min during market hours.
- Deployment: PC Task Scheduler first (1 deep run post-close + watch).
  A Raspberry Pi is justified ONLY as the always-on watch monitor with
  phone notifications — never for "all-day analysis" (data cadence, not
  compute, is the constraint).
- Faster official sources: BSE corporate announcements feed (results,
  pledges, orders — more accessible than NSE's bot-protected equivalent).
- Primary shock defense stays mechanical: pre-set ATR stops = zero
  reaction time by design.

## 6. ML ladder (in order; never skip walk-forward discipline)

- k-NN analogs (done) → regularized logistic on factors → gradient-boosted
  trees (LightGBM) → ensemble analog + GBM.
- Non-negotiable: purged/embargoed walk-forward CV (plain CV leaks future
  and produces fake accuracy); every model must beat a naive momentum
  baseline out-of-sample on IC/hit-rate or is rejected.
- Highest-value new features to validate: earnings-date proximity,
  delivery % trend, FII-flow trend, volatility regime, gap behavior.
  Validated features > more heuristics.

## 7. Cost-aware profitability (prerequisite for scaling capital)

- Paper trader must charge every simulated trade: brokerage (~₹20/order),
  STT (0.025% sell), ~0.05-0.1% slippage, and provision 20% STCG tax.
- Confidence gate = paper edge survives NET of all costs vs NIFTY.
  A gross +1.5%/trade edge can be net-negative; verify before scaling.
- Drawdown control (fixed-fraction risk sizing) matters more with larger
  capital than pick accuracy does.

## 8. Manipulation / surveillance guards (mandatory before small caps)

- Ingest NSE official surveillance lists: ASM/GSM stages (hard-flag or
  exclude), F&O ban list.
- Signature detectors from existing data: price up on falling delivery %
  (speculative pump), repeated circuits on thin volume, round-tripping
  bulk-deal counterparties, volume spike with no news.
- Promoter pledge % from filings (high pledge = forced-selling risk).
- Skip SME boards entirely.

## 9. Later / after enough runs accumulate

- Evidence-based component weights: once research.db holds outcomes for
  10-15 runs, score the tool's own past recommendations and tune the
  composite weights from its live track record (walk-forward, not
  in-sample).
- Regime-conditional weighting (signals that work above the 200DMA differ
  from below it).
