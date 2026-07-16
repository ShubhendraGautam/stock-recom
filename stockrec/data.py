"""Market data fetching via yfinance, with a small on-disk cache.

Price history for the whole universe is fetched in one batched request
(fast). Fundamentals and news are fetched per-ticker, so they are only
requested for shortlisted stocks and cached for a few hours.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

# A delisted/renamed symbol otherwise dumps tracebacks into the CLI output;
# the scan already skips tickers that return no data.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
INFO_TTL = 4 * 3600      # fundamentals cache: 4 hours
NEWS_TTL = 4 * 3600      # news cache: 4 hours

# Fundamental fields we keep from Yahoo's info blob.
INFO_FIELDS = [
    "longName", "sector", "industry", "currentPrice",
    "trailingPE", "forwardPE", "priceToBook", "returnOnEquity",
    "debtToEquity", "profitMargins", "earningsGrowth", "revenueGrowth",
    "dividendYield", "marketCap", "fiftyTwoWeekLow", "fiftyTwoWeekHigh",
    "recommendationKey", "targetMeanPrice",
]


def _cache_path(kind: str, ticker: str) -> Path:
    safe = ticker.replace("&", "_").replace("-", "_").replace(".", "_")
    return CACHE_DIR / f"{kind}_{safe}.json"


def _cache_get(kind: str, ticker: str, ttl: float):
    p = _cache_path(kind, ticker)
    try:
        if p.exists() and time.time() - p.stat().st_mtime < ttl:
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _cache_put(kind: str, ticker: str, data) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        _cache_path(kind, ticker).write_text(
            json.dumps(data, default=str), encoding="utf-8"
        )
    except OSError:
        pass


def fetch_history(tickers: list[str], period: str = "1y") -> pd.DataFrame:
    """Batched daily OHLCV download. Returns yfinance multi-ticker frame."""
    return yf.download(
        tickers,
        period=period,
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False,
    )


def close_series(hist: pd.DataFrame, ticker: str) -> pd.Series | None:
    """Extract a clean Close series for one ticker from a batched frame."""
    try:
        if isinstance(hist.columns, pd.MultiIndex):
            s = hist[ticker]["Close"]
        else:  # single-ticker download
            s = hist["Close"]
    except KeyError:
        return None
    s = s.dropna()
    return s if len(s) >= 60 else None


def fetch_info(ticker: str) -> dict:
    """Fundamentals for one ticker (cached)."""
    cached = _cache_get("info", ticker, INFO_TTL)
    if cached is not None:
        return cached
    try:
        raw = yf.Ticker(ticker).info or {}
    except Exception:
        raw = {}
    info = {k: raw.get(k) for k in INFO_FIELDS}
    _cache_put("info", ticker, info)
    return info


def fetch_news_titles(ticker: str, limit: int = 12) -> list[str]:
    """Recent news headlines for one ticker (cached)."""
    cached = _cache_get("news", ticker, NEWS_TTL)
    if cached is not None:
        return cached
    titles: list[str] = []
    try:
        for item in yf.Ticker(ticker).news or []:
            content = item.get("content", item) or {}
            title = content.get("title") or item.get("title")
            if title:
                titles.append(str(title))
            if len(titles) >= limit:
                break
    except Exception:
        pass
    _cache_put("news", ticker, titles)
    return titles


STMT_TTL = 24 * 3600     # annual statements barely change: 24 hours

INDEX_TICKER = "^NSEI"   # NIFTY 50 index, used for beta/alpha/relative strength


def _df_to_jsonable(df: pd.DataFrame) -> dict:
    return {
        "index": [str(i) for i in df.index],
        "columns": [str(c)[:10] for c in df.columns],
        "data": [[None if pd.isna(v) else float(v) for v in row]
                 for row in df.to_numpy()],
    }


def _df_from_jsonable(obj: dict) -> pd.DataFrame:
    return pd.DataFrame(obj["data"], index=obj["index"], columns=obj["columns"])


def fetch_statements(ticker: str) -> dict[str, pd.DataFrame]:
    """Annual income statement / balance sheet / cash flow (cached 24h).

    Frames have line items as rows, fiscal years as columns (newest first).
    Any statement Yahoo doesn't provide comes back as an empty frame.
    """
    cached = _cache_get("stmt", ticker, STMT_TTL)
    if cached is not None:
        return {k: _df_from_jsonable(v) for k, v in cached.items()}

    t = yf.Ticker(ticker)
    out: dict[str, pd.DataFrame] = {}
    for key, attr in (("income", "income_stmt"),
                      ("balance", "balance_sheet"),
                      ("cashflow", "cashflow")):
        try:
            df = getattr(t, attr)
            out[key] = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
        except Exception:
            out[key] = pd.DataFrame()

    _cache_put("stmt", ticker, {k: _df_to_jsonable(v) for k, v in out.items()})
    return out


def ohlcv_frame(hist: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """Extract one ticker's OHLCV frame from a batched download."""
    try:
        df = hist[ticker] if isinstance(hist.columns, pd.MultiIndex) else hist
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    except KeyError:
        return None
    return df if len(df) >= 60 else None


def fetch_index_closes(period: str = "2y") -> pd.Series | None:
    """Daily closes for the NIFTY 50 index (for beta / relative strength)."""
    try:
        hist = fetch_history([INDEX_TICKER], period=period)
        return close_series(hist, INDEX_TICKER)
    except Exception:
        return None


def last_price(ticker: str) -> float | None:
    """Latest traded price for one ticker."""
    try:
        fi = yf.Ticker(ticker).fast_info
        price = fi.get("lastPrice") if hasattr(fi, "get") else fi["lastPrice"]
        return float(price) if price else None
    except Exception:
        return None
