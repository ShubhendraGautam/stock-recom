"""Empirical 20-day prediction from historical analogs.

Approach (standard quant methodology, no curve-fitting black box):

1. Walk-forward dataset: for every stock, at weekly steps over ~5 years,
   record the factor state (momentum, RSI, trend, 52w position, volume,
   market regime) and the ACTUAL 20-day forward return that followed.
2. Analog forecast: a stock's state today is matched to its k nearest
   historical states (z-scored k-NN across the whole universe); the
   distribution of their forward returns gives an empirical win rate,
   median outcome and tail risk for the next ~20 trading days.
3. Factor validation: per-date cross-sectional rank correlation (information
   coefficient) between each factor and forward returns says which signals
   actually predicted anything in this universe - printed with the report
   so the scoring stays evidence-based.

No probability here is "pinpoint": the honest output is a distribution,
and the trade plan (ATR stop/target) manages the cases where it's wrong.
"""

from __future__ import annotations

import pickle
import time

import numpy as np
import pandas as pd

from .data import CACHE_DIR

FWD_DAYS = 21          # ~one trading month
SAMPLE_STEP = 5        # weekly sampling of history
MIN_HISTORY = 280      # rows needed before a date can produce features
K_NEIGHBORS = 250
DS_TTL = 24 * 3600

FEATURES = ["mom_1m", "mom_3m", "rsi", "dist_50", "dist_200",
            "pos_52w", "vol_ratio", "regime"]
# regime counts double in the distance metric: analogs should come from a
# similar market environment.
FEATURE_WEIGHTS = np.array([1, 1, 1, 1, 1, 1, 1, 2.0])

_DS_PATH = CACHE_DIR / "predict_ds.pkl"


def _feature_frame(close: pd.Series, volume: pd.Series,
                   regime: pd.Series) -> pd.DataFrame:
    f = pd.DataFrame(index=close.index)
    f["mom_1m"] = close.pct_change(21)
    f["mom_3m"] = close.pct_change(63)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14).mean()
    rs = gain / loss.replace(0, np.nan)
    f["rsi"] = (100 - 100 / (1 + rs)) / 100
    f["dist_50"] = close / close.rolling(50).mean() - 1
    f["dist_200"] = close / close.rolling(200).mean() - 1
    hi, lo = close.rolling(252).max(), close.rolling(252).min()
    rng = (hi - lo).replace(0, np.nan)
    f["pos_52w"] = (close - lo) / rng
    f["vol_ratio"] = volume.rolling(20).mean() / volume.rolling(90).mean()
    f["regime"] = regime.reindex(close.index).ffill()
    return f


def build_dataset(ohlcv_by_symbol: dict[str, pd.DataFrame],
                  nifty_closes: pd.Series | None) -> dict | None:
    """Build + cache the pooled walk-forward dataset. Returns the dataset."""
    if nifty_closes is None or len(nifty_closes) < 260:
        return None
    regime = (nifty_closes > nifty_closes.rolling(200).mean()).astype(float)

    frames = []
    for symbol, df in ohlcv_by_symbol.items():
        close, volume = df["Close"], df["Volume"]
        if len(close) < MIN_HISTORY + FWD_DAYS:
            continue
        f = _feature_frame(close, volume, regime)
        f["fwd20"] = close.shift(-FWD_DAYS) / close - 1
        f["symbol"] = symbol
        f = f.iloc[MIN_HISTORY::SAMPLE_STEP].dropna()
        if len(f):
            frames.append(f)
    if not frames:
        return None

    pooled = pd.concat(frames)
    X = pooled[FEATURES].to_numpy(dtype=float)
    mean, std = X.mean(axis=0), X.std(axis=0)
    std[std == 0] = 1.0

    ds = {
        "X": (X - mean) / std,
        "fwd": pooled["fwd20"].to_numpy(dtype=float),
        "mean": mean, "std": std,
        "ics": _factor_ics(pooled),
        "n": len(pooled),
        "n_symbols": pooled["symbol"].nunique(),
        "built": time.time(),
    }
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        _DS_PATH.write_bytes(pickle.dumps(ds))
    except OSError:
        pass
    return ds


REBUILD_SECONDS = 30 * 86400   # full 5y rebuild only monthly


def load_dataset(max_age: float = DS_TTL) -> dict | None:
    """Load the persisted dataset if younger than max_age seconds."""
    try:
        if _DS_PATH.exists() and time.time() - _DS_PATH.stat().st_mtime < max_age:
            return pickle.loads(_DS_PATH.read_bytes())
    except Exception:
        pass
    return None


def _factor_ics(pooled: pd.DataFrame) -> dict[str, float]:
    """Mean per-date cross-sectional rank IC of each factor vs fwd return."""
    ics: dict[str, float] = {}
    grouped = pooled.groupby(pooled.index)
    for feat in FEATURES:
        if feat == "regime":       # constant across the cross-section
            continue
        per_date = []
        for _, g in grouped:
            if len(g) >= 20:
                ic = g[feat].rank().corr(g["fwd20"].rank())
                if pd.notna(ic):
                    per_date.append(ic)
        if per_date:
            ics[feat] = float(np.mean(per_date))
    return ics


def features_today(ohlcv: pd.DataFrame,
                   nifty_closes: pd.Series | None) -> np.ndarray | None:
    """Current feature vector for one stock, or None if not computable."""
    if nifty_closes is None or len(ohlcv) < MIN_HISTORY:
        return None
    regime = (nifty_closes > nifty_closes.rolling(200).mean()).astype(float)
    f = _feature_frame(ohlcv["Close"], ohlcv["Volume"], regime)
    last = f.iloc[-1]
    if last.isna().any():
        return None
    return last[FEATURES].to_numpy(dtype=float)


def analog_forecast(ds: dict, feats: np.ndarray,
                    k: int = K_NEIGHBORS) -> dict | None:
    """Empirical 20d forward-return distribution of the k nearest analogs."""
    if ds is None or feats is None or ds["n"] < 500:
        return None
    v = (feats - ds["mean"]) / ds["std"]
    d2 = ((ds["X"] - v) ** 2 * FEATURE_WEIGHTS).sum(axis=1)
    k = min(k, len(d2))
    idx = np.argpartition(d2, k - 1)[:k]
    fwd = ds["fwd"][idx]
    return {
        "n": int(len(fwd)),
        "win_rate": float((fwd > 0).mean()),
        "mean_pct": float(fwd.mean()) * 100,
        "median_pct": float(np.median(fwd)) * 100,
        "p5_pct": float(np.percentile(fwd, 5)) * 100,
        "p95_pct": float(np.percentile(fwd, 95)) * 100,
    }


def prediction_score(forecast: dict | None) -> float | None:
    """Analog forecast -> 0-100 score (win rate + median, shrunk by n)."""
    if forecast is None:
        return None
    raw = ((forecast["win_rate"] - 0.5) * 80
           + max(-12.0, min(12.0, forecast["median_pct"] * 3)))
    confidence = min(1.0, forecast["n"] / 150)
    return max(0.0, min(100.0, 50 + raw * confidence))


def trade_plan(price: float, atr_pct: float | None,
               forecast: dict | None) -> dict | None:
    """ATR-anchored stop/target for a ~20-day swing position."""
    if not price or not atr_pct:
        return None
    stop = price * (1 - 2.0 * atr_pct / 100)
    up = 2.5 * atr_pct / 100
    if forecast and forecast["median_pct"] > 0:
        up = max(up, forecast["median_pct"] / 100)
    target = price * (1 + up)
    rr = (target - price) / max(price - stop, 1e-9)
    return {
        "stop": stop, "stop_pct": (stop / price - 1) * 100,
        "target": target, "target_pct": (target / price - 1) * 100,
        "rr": rr,
    }


IC_LABELS = {
    "mom_1m": "1m momentum", "mom_3m": "3m momentum", "rsi": "RSI",
    "dist_50": "50DMA dist", "dist_200": "200DMA dist",
    "pos_52w": "52w position", "vol_ratio": "volume ratio",
}


def ic_summary(ds: dict | None, top: int = 4) -> str | None:
    """One-line walk-forward validation summary for the report footer."""
    if not ds or not ds.get("ics"):
        return None
    ranked = sorted(ds["ics"].items(), key=lambda kv: abs(kv[1]), reverse=True)
    parts = [f"{IC_LABELS.get(f, f)} {ic:+.03f}" for f, ic in ranked[:top]]
    return (f"signal check (walk-forward, {ds['n']:,} samples, "
            f"{ds['n_symbols']} stocks): IC " + " | ".join(parts))
