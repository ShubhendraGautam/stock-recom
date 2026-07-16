"""Deep analysis: institutional-grade metrics computed from raw data.

Components (each scored 0-100, blended in engine.deep_composite):
  technical   - trend (MAs, MACD, ADX), mean-reversion (%B, RSI), volume (OBV)
  risk        - Sharpe, Sortino, max drawdown, beta/alpha vs NIFTY,
                Monte Carlo bootstrap of 1-year forward returns
  fundamental - valuation & profitability from Yahoo info (P/E, ROE, ...)
  quality     - Piotroski F-Score, Altman Z-Score, multi-year CAGRs and
                cash-flow quality from actual annual statements
  sentiment   - recency-weighted news (news.py)
  institutional - NSE delivery %, analyst consensus & target upside

All math is numpy/pandas (vectorized C under the hood); a full universe
deep scan is network-bound, not compute-bound.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

RISK_FREE = 0.065          # ~India 10y G-sec yield
TRADING_DAYS = 252
MC_PATHS = 4000

FINANCIAL_SECTORS = {"Financial Services", "Financial"}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------
# Technical suite
# --------------------------------------------------------------------------

def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _adx(df: pd.DataFrame, window: int = 14) -> float:
    high, low, close = df["High"], df["Low"], df["Close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / window, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / window, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / window, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / window, adjust=False).mean()
    return float(adx.iloc[-1]) if not math.isnan(adx.iloc[-1]) else 0.0


def _atr_pct(df: pd.DataFrame, window: int = 14) -> float:
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / window, adjust=False).mean().iloc[-1]
    return float(atr / close.iloc[-1] * 100)


def advanced_technicals(df: pd.DataFrame) -> tuple[float, dict]:
    """Score 0-100 from a 2y OHLCV frame: trend + momentum + volume."""
    closes, volume = df["Close"], df["Volume"]
    price = float(closes.iloc[-1])

    sma50 = float(closes.rolling(50).mean().iloc[-1])
    sma200 = float(closes.rolling(200).mean().iloc[-1]) if len(closes) >= 200 else sma50

    # RSI(14), Wilder smoothing
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, min_periods=14).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14).mean().iloc[-1]
    rsi = 100.0 if loss == 0 else float(100 - 100 / (1 + gain / loss))

    # MACD(12,26,9) histogram, normalized by price
    macd = _ema(closes, 12) - _ema(closes, 26)
    hist = macd - _ema(macd, 9)
    macd_hist_pct = float(hist.iloc[-1] / price * 100)
    macd_rising = bool(hist.iloc[-1] > hist.iloc[-5]) if len(hist) > 5 else False

    adx = _adx(df)
    trending_up = price > sma50 > sma200

    # Bollinger %B (20, 2)
    mid = closes.rolling(20).mean().iloc[-1]
    sd = closes.rolling(20).std().iloc[-1]
    pct_b = float((price - (mid - 2 * sd)) / (4 * sd)) if sd else 0.5

    # OBV: is smart volume accumulating?
    obv = (np.sign(closes.diff().fillna(0)) * volume).cumsum()
    obv_accum = bool(obv.iloc[-1] > obv.rolling(20).mean().iloc[-1])

    # Volume expansion: last 20d vs prior 90d average
    v20 = float(volume.tail(20).mean())
    v90 = float(volume.tail(110).head(90).mean()) or 1.0
    vol_ratio = v20 / v90

    mom_3m = (price / float(closes.iloc[-63]) - 1) * 100 if len(closes) > 63 else 0.0
    mom_6m = (price / float(closes.iloc[-126]) - 1) * 100 if len(closes) > 126 else mom_3m
    yr = closes.tail(TRADING_DAYS)
    lo52, hi52 = float(yr.min()), float(yr.max())
    pos52 = (price - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else 50.0
    atr_pct = _atr_pct(df)

    adj = 0.0
    adj += 8 if price > sma50 else -8
    adj += 10 if price > sma200 else -10
    adj += 5 if sma50 > sma200 else -5
    # MACD: direction and slope
    adj += (4 if macd_hist_pct > 0 else -4) + (3 if macd_rising else -3)
    # ADX qualifies the trend: strong trend amplifies its direction
    if adx > 25:
        adj += 6 if trending_up else -6
    # %B extremes are stretched
    if pct_b > 1.0:
        adj -= 5
    elif pct_b < 0.0:
        adj -= 3   # falling knife, slight penalty despite "cheap"
    # Volume confirms
    adj += 4 if obv_accum else -4
    if vol_ratio > 1.4 and trending_up:
        adj += 3
    # Momentum
    adj += _clamp(mom_3m * 0.8, -10, 10)
    adj += _clamp(mom_6m * 0.4, -7, 7)
    if 40 <= rsi <= 65:
        adj += 5
    elif rsi > 75 or rsi < 25:
        adj -= 6
    if pos52 >= 60:
        adj += 3
    elif pos52 <= 20:
        adj -= 4

    score = _clamp(50 + adj * 0.7)
    details = {
        "price": price, "sma50": sma50, "sma200": sma200, "rsi": rsi,
        "macd_hist_pct": macd_hist_pct, "adx": adx, "pct_b": pct_b,
        "obv_accum": obv_accum, "vol_ratio": vol_ratio, "atr_pct": atr_pct,
        "mom_3m_pct": mom_3m, "mom_6m_pct": mom_6m, "pos_52w_pct": pos52,
    }
    return score, details


# --------------------------------------------------------------------------
# Risk / return statistics + Monte Carlo
# --------------------------------------------------------------------------

def risk_stats(closes: pd.Series, index_closes: pd.Series | None,
               seed: int = 7) -> tuple[float, dict]:
    """Score 0-100 from risk-adjusted returns over ~2y of daily closes."""
    ret = closes.pct_change().dropna()
    if len(ret) < 120:
        return 50.0, {}

    ann_ret = float((1 + ret.mean()) ** TRADING_DAYS - 1)
    ann_vol = float(ret.std() * math.sqrt(TRADING_DAYS)) or 1e-9
    sharpe = (ann_ret - RISK_FREE) / ann_vol
    downside = ret[ret < 0]
    dstd = float(downside.std() * math.sqrt(TRADING_DAYS)) if len(downside) else 1e-9
    sortino = (ann_ret - RISK_FREE) / dstd

    cum = (1 + ret).cumprod()
    max_dd = float((cum / cum.cummax() - 1).min()) * 100

    beta = alpha = None
    if index_closes is not None:
        idx_ret = index_closes.pct_change().dropna()
        joined = pd.concat([ret, idx_ret], axis=1, join="inner").dropna()
        if len(joined) > 120:
            s, m = joined.iloc[:, 0].to_numpy(), joined.iloc[:, 1].to_numpy()
            var_m = float(np.var(m))
            if var_m > 0:
                beta = float(np.cov(s, m)[0, 1] / var_m)
                idx_ann = float((1 + m.mean()) ** TRADING_DAYS - 1)
                alpha = (ann_ret - RISK_FREE - beta * (idx_ann - RISK_FREE)) * 100

    # Monte Carlo bootstrap: resample daily log returns into 1y paths.
    log_ret = np.log1p(ret.to_numpy())
    rng = np.random.default_rng(seed)
    paths = rng.choice(log_ret, size=(MC_PATHS, TRADING_DAYS)).sum(axis=1)
    finals = np.expm1(paths)
    p_gain = float((finals > 0).mean())
    mc_median = float(np.median(finals)) * 100
    var5 = float(np.percentile(finals, 5)) * 100   # 1y 5% value-at-risk

    six_m = float(closes.iloc[-1] / closes.iloc[-126] - 1) if len(closes) > 126 else 0.0
    ram = six_m / ann_vol   # risk-adjusted momentum

    adj = 0.0
    if sharpe > 1.2:
        adj += 15
    elif sharpe > 0.8:
        adj += 10
    elif sharpe > 0.4:
        adj += 5
    elif sharpe < 0:
        adj -= 10
    adj += _clamp((sortino - sharpe) * 3, -4, 4)
    if max_dd > -15:
        adj += 6
    elif max_dd < -35:
        adj -= 8
    adj += _clamp((p_gain - 0.5) * 40, -12, 12)
    adj += _clamp(ram * 12, -10, 10)

    details = {
        "ann_return_pct": ann_ret * 100, "ann_vol_pct": ann_vol * 100,
        "sharpe": sharpe, "sortino": sortino, "max_dd_pct": max_dd,
        "beta": beta, "alpha_pct": alpha,
        "mc_p_gain": p_gain, "mc_median_pct": mc_median, "mc_var5_pct": var5,
    }
    return _clamp(50 + adj), details


# --------------------------------------------------------------------------
# Snapshot fundamentals (Yahoo info fields; missing data -> neutral)
# --------------------------------------------------------------------------

def fundamental_score(info: dict) -> tuple[float, dict]:
    score = 50.0
    pe = info.get("trailingPE")
    roe = info.get("returnOnEquity")
    de = info.get("debtToEquity")
    margins = info.get("profitMargins")
    eg = info.get("earningsGrowth")
    rg = info.get("revenueGrowth")

    if pe is not None:
        if pe <= 0:
            score -= 12          # loss-making
        elif pe < 20:
            score += 10
        elif pe < 35:
            score += 4
        elif pe > 70:
            score -= 10
        elif pe > 50:
            score -= 5
    if roe is not None:
        if roe > 0.18:
            score += 10
        elif roe > 0.12:
            score += 5
        elif roe < 0.05:
            score -= 8
    if de is not None:          # Yahoo reports this as a percentage
        if de < 50:
            score += 6
        elif de > 200:
            score -= 8
        elif de > 120:
            score -= 4
    if margins is not None:
        if margins > 0.15:
            score += 6
        elif margins < 0.03:
            score -= 6
    if eg is not None:
        score += _clamp(eg * 40, -10, 10)
    if rg is not None:
        score += _clamp(rg * 30, -6, 6)

    details = {
        "pe": pe, "roe": roe, "debt_to_equity": de,
        "margins": margins, "earnings_growth": eg, "revenue_growth": rg,
    }
    return _clamp(score), details


# --------------------------------------------------------------------------
# Statement-based quality: Piotroski, Altman, growth, cash conversion
# --------------------------------------------------------------------------

def _row(df: pd.DataFrame, *names: str) -> pd.Series | None:
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            s = df.loc[n]
            return s.iloc[0] if isinstance(s, pd.DataFrame) else s
    return None


def _val(s: pd.Series | None, i: int) -> float | None:
    if s is None or i >= len(s):
        return None
    v = s.iloc[i]
    return None if pd.isna(v) else float(v)


def piotroski(stmts: dict) -> tuple[int | None, dict]:
    """Piotroski F-Score (0-9) from the two most recent fiscal years."""
    inc, bal, cfs = stmts.get("income"), stmts.get("balance"), stmts.get("cashflow")
    ni = _row(inc, "Net Income", "Net Income Common Stockholders")
    ta = _row(bal, "Total Assets")
    cfo = _row(cfs, "Operating Cash Flow", "Cash Flow From Continuing Operating Activities")
    ltd = _row(bal, "Long Term Debt", "Long Term Debt And Capital Lease Obligation")
    ca = _row(bal, "Current Assets")
    cl = _row(bal, "Current Liabilities")
    gp = _row(inc, "Gross Profit")
    rev = _row(inc, "Total Revenue", "Operating Revenue")
    shares = _row(bal, "Ordinary Shares Number", "Share Issued")

    checks: dict[str, bool | None] = {}

    def ratio(num, den, i):
        n, d = _val(num, i), _val(den, i)
        return n / d if n is not None and d else None

    roa0, roa1 = ratio(ni, ta, 0), ratio(ni, ta, 1)
    checks["ROA positive"] = roa0 > 0 if roa0 is not None else None
    cfo0 = _val(cfo, 0)
    checks["CFO positive"] = cfo0 > 0 if cfo0 is not None else None
    checks["ROA improving"] = (roa0 > roa1) if None not in (roa0, roa1) else None
    ni0 = _val(ni, 0)
    checks["CFO > net income"] = (cfo0 > ni0) if None not in (cfo0, ni0) else None
    lev0, lev1 = ratio(ltd, ta, 0), ratio(ltd, ta, 1)
    checks["Leverage falling"] = (lev0 <= lev1) if None not in (lev0, lev1) else None
    cr0 = ratio(ca, cl, 0)
    cr1 = ratio(ca, cl, 1)
    checks["Liquidity improving"] = (cr0 > cr1) if None not in (cr0, cr1) else None
    sh0, sh1 = _val(shares, 0), _val(shares, 1)
    checks["No dilution"] = (sh0 <= sh1 * 1.02) if None not in (sh0, sh1) else None
    gm0, gm1 = ratio(gp, rev, 0), ratio(gp, rev, 1)
    checks["Gross margin up"] = (gm0 > gm1) if None not in (gm0, gm1) else None
    at0, at1 = ratio(rev, ta, 0), ratio(rev, ta, 1)
    checks["Asset turnover up"] = (at0 > at1) if None not in (at0, at1) else None

    known = [v for v in checks.values() if v is not None]
    if len(known) < 5:      # too little data to be meaningful
        return None, {"checks": checks}
    return sum(known), {"checks": checks, "evaluated": len(known)}


def altman_z(stmts: dict, market_cap: float | None,
             sector: str | None) -> tuple[float | None, str]:
    """Altman Z-Score. Not meaningful for banks/NBFCs -> (None, reason)."""
    if sector in FINANCIAL_SECTORS:
        return None, "n/a for financials"
    inc, bal = stmts.get("income"), stmts.get("balance")
    ta = _val(_row(bal, "Total Assets"), 0)
    if not ta:
        return None, "no balance sheet"
    ca = _val(_row(bal, "Current Assets"), 0)
    cl = _val(_row(bal, "Current Liabilities"), 0)
    wc = _val(_row(bal, "Working Capital"), 0)
    if wc is None and None not in (ca, cl):
        wc = ca - cl
    re = _val(_row(bal, "Retained Earnings"), 0)
    ebit = _val(_row(inc, "EBIT", "Operating Income", "Pretax Income"), 0)
    tl = _val(_row(bal, "Total Liabilities Net Minority Interest",
                   "Total Liabilities"), 0)
    sales = _val(_row(inc, "Total Revenue", "Operating Revenue"), 0)
    if None in (wc, re, ebit, tl, sales) or not tl or not market_cap:
        return None, "incomplete data"
    z = (1.2 * wc / ta + 1.4 * re / ta + 3.3 * ebit / ta
         + 0.6 * market_cap / tl + 1.0 * sales / ta)
    return float(z), ""


def _cagr(series: pd.Series | None) -> float | None:
    """CAGR from newest-first annual series over the available span."""
    if series is None:
        return None
    vals = [(_val(series, i)) for i in range(len(series))]
    vals = [v for v in vals if v is not None]
    if len(vals) < 3 or vals[-1] <= 0 or vals[0] <= 0:
        return None
    years = len(vals) - 1
    return (vals[0] / vals[-1]) ** (1 / years) - 1


def quality_score(stmts: dict, market_cap: float | None,
                  sector: str | None) -> tuple[float, dict]:
    """Statement-based quality/health score 0-100."""
    f_score, f_details = piotroski(stmts)
    z, z_note = altman_z(stmts, market_cap, sector)

    inc, cfs = stmts.get("income"), stmts.get("cashflow")
    rev = _row(inc, "Total Revenue", "Operating Revenue")
    ni = _row(inc, "Net Income", "Net Income Common Stockholders")
    rev_cagr = _cagr(rev)
    ni_cagr = _cagr(ni)

    # Cash conversion: CFO / net income averaged over available years
    cfo = _row(cfs, "Operating Cash Flow",
               "Cash Flow From Continuing Operating Activities")
    ccs = []
    for i in range(4):
        c, n = _val(cfo, i), _val(ni, i)
        if c is not None and n:
            ccs.append(c / n)
    cash_conv = sum(ccs) / len(ccs) if ccs else None

    # Net margin trajectory (newest vs 2 years prior)
    margin_trend = None
    r0, n0 = _val(rev, 0), _val(ni, 0)
    r2, n2 = _val(rev, 2), _val(ni, 2)
    if None not in (r0, n0, r2, n2) and r0 and r2:
        margin_trend = (n0 / r0 - n2 / r2) * 100   # percentage points

    adj = 0.0
    if f_score is not None:
        adj += (f_score - 4.5) * 4
    if z is not None:
        if z > 3:
            adj += 8
        elif z < 1.8:
            adj -= 10
    if rev_cagr is not None:
        adj += _clamp(rev_cagr * 60, -8, 8)
    if ni_cagr is not None:
        adj += _clamp(ni_cagr * 50, -8, 8)
    if cash_conv is not None:
        if cash_conv > 1.0:
            adj += 6
        elif cash_conv < 0.6:
            adj -= 6
    if margin_trend is not None:
        if margin_trend > 1:
            adj += 4
        elif margin_trend < -1:
            adj -= 4

    details = {
        "f_score": f_score, "f_checks": f_details.get("checks", {}),
        "altman_z": z, "altman_note": z_note,
        "rev_cagr": rev_cagr, "ni_cagr": ni_cagr,
        "cash_conversion": cash_conv, "margin_trend_pp": margin_trend,
    }
    return _clamp(50 + adj), details


# --------------------------------------------------------------------------
# Institutional signals: NSE delivery %, analyst consensus
# --------------------------------------------------------------------------

BUILDUP_ADJ = {
    "long buildup": 6, "short covering": 3,
    "long unwinding": -3, "short buildup": -6,
}


def institutional_score(delivery: float | None, info: dict,
                        price: float | None,
                        fo: dict | None = None,
                        deals: list[dict] | None = None) -> tuple[float, dict]:
    adj = 0.0
    if delivery is not None:
        if delivery >= 65:
            adj += 10
        elif delivery >= 50:
            adj += 5
        elif delivery < 30:
            adj -= 5

    target = info.get("targetMeanPrice")
    upside = None
    if target and price:
        upside = (target / price - 1) * 100
        if upside > 15:
            adj += 8
        elif upside > 5:
            adj += 4
        elif upside < 0:
            adj -= 6

    reco = (info.get("recommendationKey") or "").lower()
    adj += {"strong_buy": 6, "buy": 3, "underperform": -4, "sell": -6}.get(reco, 0)

    # Derivatives positioning (only for F&O-listed stocks)
    buildup = pcr = oi_chg = None
    if fo:
        buildup = fo.get("buildup")
        pcr = fo.get("pcr")
        oi_chg = fo.get("fut_oi_chg_pct")
        adj += BUILDUP_ADJ.get(buildup, 0)
        if pcr is not None:
            if pcr >= 1.2:
                adj += 3      # heavy put writing = support below
            elif pcr <= 0.6:
                adj -= 3

    # Bulk/block deals: net big-money direction on the latest trading day
    bulk_net = None
    if deals:
        bought = sum(d["qty"] for d in deals if d["side"] == "BUY")
        sold = sum(d["qty"] for d in deals if d["side"] == "SELL")
        bulk_net = bought - sold
        if bulk_net > 0:
            adj += 4
        elif bulk_net < 0:
            adj -= 4

    details = {
        "delivery_pct": delivery, "target_upside_pct": upside,
        "recommendation": reco or None,
        "fo_buildup": buildup, "fo_oi_chg_pct": oi_chg, "fo_pcr": pcr,
        "bulk_net_qty": bulk_net, "bulk_deals": (deals or [])[:3],
    }
    return _clamp(50 + adj), details
