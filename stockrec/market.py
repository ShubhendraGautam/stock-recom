"""Market-level context: regime (NIFTY trend + India VIX) and sector rotation.

One batched Yahoo download of the NIFTY, India VIX, and NSE sectoral
indices yields:
  - regime: is the market itself in an up/downtrend, and how fearful is it
  - rotation: which sectors lead/lag the NIFTY over 3 months, so each
    stock gets a sector tailwind/headwind in its technical score
"""

from __future__ import annotations

from . import data

CTX_TTL = 6 * 3600

SECTOR_INDICES = {
    "IT": "^CNXIT",
    "BANK": "^NSEBANK",
    "FIN SERVICES": "NIFTY_FIN_SERVICE.NS",
    "PHARMA": "^CNXPHARMA",
    "AUTO": "^CNXAUTO",
    "FMCG": "^CNXFMCG",
    "METAL": "^CNXMETAL",
    "ENERGY": "^CNXENERGY",
    "REALTY": "^CNXREALTY",
    "INFRA": "^CNXINFRA",
    "PSU BANK": "^CNXPSUBANK",
    "CONSUMPTION": "^CNXCONSUM",
    "MEDIA": "^CNXMEDIA",
}

# (keyword in Yahoo industry/sector, sector index label) - first match wins
_INDUSTRY_MAP = [
    ("bank", "BANK"),
    ("software", "IT"), ("information technology", "IT"),
    ("pharma", "PHARMA"), ("drug", "PHARMA"), ("biotech", "PHARMA"),
    ("healthcare", "PHARMA"), ("medical", "PHARMA"),
    ("auto", "AUTO"),
    ("steel", "METAL"), ("metal", "METAL"), ("aluminum", "METAL"),
    ("mining", "METAL"), ("copper", "METAL"),
    ("oil", "ENERGY"), ("gas", "ENERGY"), ("petroleum", "ENERGY"),
    ("power", "ENERGY"), ("electric", "ENERGY"), ("coal", "ENERGY"),
    ("solar", "ENERGY"), ("energy", "ENERGY"), ("utilities", "ENERGY"),
    ("real estate", "REALTY"), ("realty", "REALTY"),
    ("beverages", "FMCG"), ("packaged", "FMCG"), ("household", "FMCG"),
    ("tobacco", "FMCG"), ("food", "FMCG"), ("personal products", "FMCG"),
    ("consumer defensive", "FMCG"),
    ("construction", "INFRA"), ("engineering", "INFRA"),
    ("infrastructure", "INFRA"), ("industrial", "INFRA"),
    ("insurance", "FIN SERVICES"), ("capital markets", "FIN SERVICES"),
    ("credit", "FIN SERVICES"), ("financial", "FIN SERVICES"),
    ("asset management", "FIN SERVICES"),
    ("entertainment", "MEDIA"), ("media", "MEDIA"), ("broadcasting", "MEDIA"),
    ("luxury", "CONSUMPTION"), ("retail", "CONSUMPTION"),
    ("apparel", "CONSUMPTION"), ("restaurant", "CONSUMPTION"),
    ("lodging", "CONSUMPTION"), ("travel", "CONSUMPTION"),
    ("airline", "CONSUMPTION"), ("leisure", "CONSUMPTION"),
    ("consumer cyclical", "CONSUMPTION"),
]


def _ret(closes, days: int) -> float | None:
    if closes is None or len(closes) <= days:
        return None
    return float(closes.iloc[-1] / closes.iloc[-days] - 1) * 100


def context() -> dict:
    """{'nifty': .., 'nifty_vs_200dma_pct': .., 'trend': 'up'|'down',
        'vix': .., 'vix_pctile': .., 'rotation': {label: {...}}} (cached 6h).
    """
    cached = data._cache_get("market", "ctx", CTX_TTL)
    if cached is not None:
        return cached

    tickers = ["^NSEI", "^INDIAVIX"] + list(SECTOR_INDICES.values())
    ctx: dict = {"rotation": {}}
    try:
        hist = data.fetch_history(tickers, period="1y")
    except Exception:
        return ctx

    nifty = data.close_series(hist, "^NSEI")
    nifty_3m = None
    if nifty is not None and len(nifty) >= 200:
        price = float(nifty.iloc[-1])
        dma200 = float(nifty.rolling(200).mean().iloc[-1])
        ctx["nifty"] = price
        ctx["nifty_vs_200dma_pct"] = (price / dma200 - 1) * 100
        ctx["trend"] = "up" if price > dma200 else "down"
        nifty_3m = _ret(nifty, 63)

    vix = data.close_series(hist, "^INDIAVIX")
    if vix is not None:
        last = float(vix.iloc[-1])
        ctx["vix"] = last
        ctx["vix_pctile"] = float((vix <= last).mean()) * 100

    for label, ticker in SECTOR_INDICES.items():
        closes = data.close_series(hist, ticker)
        r1, r3 = _ret(closes, 21), _ret(closes, 63)
        if r3 is None:
            continue
        ctx["rotation"][label] = {
            "ret_1m_pct": r1,
            "ret_3m_pct": r3,
            "rs_3m_pct": r3 - nifty_3m if nifty_3m is not None else None,
        }

    data._cache_put("market", "ctx", ctx)
    return ctx


def sector_label(info: dict) -> str | None:
    """Best-effort map from Yahoo sector/industry to an NSE sector index."""
    text = f"{info.get('industry') or ''} {info.get('sector') or ''}".lower()
    for keyword, label in _INDUSTRY_MAP:
        if keyword in text:
            return label
    return None


def sector_rs(info: dict, ctx: dict) -> tuple[str | None, float | None]:
    """(sector index label, 3m relative strength vs NIFTY in % points)."""
    label = sector_label(info)
    if label is None:
        return None, None
    entry = (ctx.get("rotation") or {}).get(label)
    if entry is None or entry.get("rs_3m_pct") is None:
        return label, None
    return label, entry["rs_3m_pct"]
