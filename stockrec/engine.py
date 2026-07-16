"""Deep scan engine.

Every run analyzes EVERY stock in the universe with the full deep.py
pipeline - annual statements, Piotroski/Altman, risk stats vs NIFTY, Monte
Carlo, recency-weighted news, NSE delivery %. Network-bound; per-ticker
data is cached on disk, so a re-run the same day is fast.

Portfolio awareness: held stocks are analyzed too (their scores drive the
portfolio advice) but are filtered out of recommendations by the CLI, and
sectors already overweight in the portfolio are penalized.

Weights are tuned for a short-term (weeks to months) holding horizon:
trend/momentum and risk carry more weight than they would for a
buy-and-forget portfolio, but statement quality still gates out junk.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import data, deep, market, news, nse, portfolio, predict
from .universe import from_yahoo, to_yahoo

SECTOR_RS_CAP = 5.0     # max +/- technical adj from sector rotation
RISK_WINDOW = 504       # risk stats use the last ~2y of the 5y history

SECTOR_OVERWEIGHT = 0.30      # >30% of portfolio value in one sector
SECTOR_PENALTY = 5.0

DEEP_WEIGHTS = {
    "technical": 0.18,
    "prediction": 0.20,     # empirical 20d analog forecast (predict.py)
    "risk": 0.10,
    "fundamental": 0.12,
    "quality": 0.14,
    "sentiment": 0.13,
    "institutional": 0.13,
}


def verdict_of(score: float) -> str:
    if score >= 65:
        return "STRONG BUY"
    if score >= 55:
        return "BUY"
    if score >= 45:
        return "HOLD"
    if score >= 35:
        return "WEAK"
    return "AVOID"


@dataclass
class DeepResult:
    symbol: str
    price: float | None = None
    components: dict[str, float | None] = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    penalty: float = 0.0
    notes: list[str] = field(default_factory=list)
    # research.attach_conviction fills these from past runs:
    conviction: float | None = None
    score_delta: float | None = None
    runs_seen: int = 0

    @property
    def composite(self) -> float:
        total = weight = 0.0
        for name, w in DEEP_WEIGHTS.items():
            score = self.components.get(name)
            if score is not None:
                total += score * w
                weight += w
        base = total / weight if weight else 50.0
        return max(0.0, base - self.penalty)

    @property
    def verdict(self) -> str:
        return verdict_of(self.composite)


# --------------------------------------------------------------------------
# Portfolio awareness
# --------------------------------------------------------------------------

def portfolio_context() -> tuple[set[str], dict[str, float]]:
    """(held symbols, sector -> weight of portfolio value)."""
    held = portfolio.holdings()
    if not held:
        return set(), {}
    values: dict[str, float] = {}
    sectors: dict[str, float] = {}
    for sym, h in held.items():
        info = data.fetch_info(to_yahoo(sym))
        price = info.get("currentPrice") or h.avg_price
        value = h.units * float(price)
        values[sym] = value
        sector = info.get("sector") or "Unknown"
        sectors[sector] = sectors.get(sector, 0.0) + value
    total = sum(values.values()) or 1.0
    return set(held), {s: v / total for s, v in sectors.items()}


def _apply_portfolio_awareness(result: DeepResult,
                               sector_weights: dict[str, float]) -> None:
    sector = result.details.get("sector")
    weight = sector_weights.get(sector, 0.0)
    if weight >= SECTOR_OVERWEIGHT:
        result.penalty += SECTOR_PENALTY
        result.notes.append(
            f"sector '{sector}' is already {weight:.0%} of your portfolio"
        )


# --------------------------------------------------------------------------
# Deep pipeline
# --------------------------------------------------------------------------

def _fetch_extras() -> dict:
    """Market-wide datasets fetched once per scan (all cached on disk)."""
    return {
        "fo": nse.fo_positioning(),
        "deals": nse.bulk_deals(),
        "ctx": market.context(),
    }


def _deep_one(symbol: str, ohlcv, index_closes, extras: dict) -> DeepResult:
    """Full deep analysis of one stock. Local math + per-ticker fetches."""
    ticker = to_yahoo(symbol)
    r = DeepResult(symbol=symbol)

    t_score, t_details = deep.advanced_technicals(ohlcv)
    r.details.update(t_details)
    r.price = t_details["price"]

    idx_tail = index_closes.tail(RISK_WINDOW) if index_closes is not None else None
    k_score, k_details = deep.risk_stats(ohlcv["Close"].tail(RISK_WINDOW), idx_tail)
    r.components["risk"] = k_score if k_details else None
    r.details.update(k_details)

    # Empirical 20d analog forecast from the walk-forward dataset
    feats = predict.features_today(ohlcv, index_closes)
    forecast = predict.analog_forecast(extras.get("ds"), feats)
    r.components["prediction"] = predict.prediction_score(forecast)
    if forecast:
        r.details["analog"] = forecast
    plan = predict.trade_plan(r.price, t_details.get("atr_pct"), forecast)
    if plan:
        r.details["plan"] = plan

    info = data.fetch_info(ticker)
    f_score, f_details = deep.fundamental_score(info)
    r.components["fundamental"] = f_score
    r.details.update(f_details)
    r.details["name"] = info.get("longName") or symbol
    r.details["sector"] = info.get("sector")

    # Sector rotation: a leading sector is a tailwind for the stock's trend
    label, rs = market.sector_rs(info, extras.get("ctx") or {})
    r.details["sector_index"] = label
    r.details["sector_rs_pct"] = rs
    if rs is not None:
        t_score += max(-SECTOR_RS_CAP, min(SECTOR_RS_CAP, rs * 0.7))
        t_score = max(0.0, min(100.0, t_score))
    r.components["technical"] = t_score

    stmts = data.fetch_statements(ticker)
    q_score, q_details = deep.quality_score(
        stmts, info.get("marketCap"), info.get("sector"))
    r.components["quality"] = q_score
    r.details.update(q_details)

    yahoo_titles = data.fetch_news_titles(ticker)
    s_score, s_details = news.sentiment(r.details["name"], symbol, yahoo_titles)
    r.components["sentiment"] = s_score
    r.details["headlines"] = s_details["headlines"]
    r.details["headline_titles"] = s_details["titles"]

    delivery = nse.delivery_pct(symbol)
    i_score, i_details = deep.institutional_score(
        delivery, info, r.price,
        fo=(extras.get("fo") or {}).get(symbol),
        deals=(extras.get("deals") or {}).get(symbol),
    )
    r.components["institutional"] = i_score
    r.details.update(i_details)

    return r


def deep_scan(symbols: list[str], progress=None,
              sector_weights: dict[str, float] | None = None) -> list[DeepResult]:
    """Deep-analyze every stock in `symbols`. Returns results, best first."""
    say = progress or (lambda msg: None)
    sector_weights = sector_weights or {}
    tickers = [to_yahoo(s) for s in symbols]

    # Monthly full 5y rebuild of the analog dataset; otherwise reuse the
    # persisted one and download only 2y (current indicators + risk stats).
    ds = predict.load_dataset(max_age=predict.REBUILD_SECONDS)
    if ds is not None and ds.get("n_symbols", 0) < 0.8 * len(tickers):
        ds = None                       # universe grew - rebuild
    period = "2y" if ds is not None else "5y"

    say(f"downloading {period} price history for {len(tickers)} stocks...")
    hist = data.fetch_history(tickers, period=period)
    index_closes = data.fetch_index_closes(period)

    say("fetching market context (VIX, sectors, F&O positioning, bulk deals)...")
    extras = _fetch_extras()

    candidates: list[tuple[str, object]] = []
    for ticker in tickers:
        ohlcv = data.ohlcv_frame(hist, ticker)
        if ohlcv is None:
            continue
        # liquidity gate (matters for mid/small caps): median daily turnover
        turnover = float((ohlcv["Close"] * ohlcv["Volume"]).tail(60).median())
        if turnover < 5e7:              # ₹5 crore
            continue
        candidates.append((from_yahoo(ticker), ohlcv))

    if ds is None:
        say(f"building walk-forward dataset (5y, {len(candidates)} stocks)...")
        ds = predict.build_dataset(dict(candidates), index_closes)
    extras["ds"] = ds

    say(f"deep-analyzing {len(candidates)} stocks "
        "(statements, risk, news, delivery %)...")
    done = 0
    results: list[DeepResult] = []

    def work(item):
        nonlocal done
        symbol, ohlcv = item
        try:
            res = _deep_one(symbol, ohlcv, index_closes, extras)
        except Exception:
            res = None
        done += 1
        if done % 10 == 0:
            say(f"deep-analyzing... {done}/{len(candidates)} done")
        return res

    with ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(work, candidates):
            if res is not None:
                results.append(res)

    for r in results:
        _apply_portfolio_awareness(r, sector_weights)

    results.sort(key=lambda r: r.composite, reverse=True)
    return results


def analyze_one(symbol: str) -> DeepResult | None:
    """Deep single-stock analysis (uses the last report's analog dataset)."""
    ticker = to_yahoo(symbol)
    hist = data.fetch_history([ticker], period="2y")
    ohlcv = data.ohlcv_frame(hist, ticker)
    if ohlcv is None:
        return None
    index_closes = data.fetch_index_closes("2y")
    extras = _fetch_extras()
    extras["ds"] = predict.load_dataset()
    return _deep_one(from_yahoo(ticker), ohlcv, index_closes, extras)
