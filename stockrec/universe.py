"""Stock universes for scanning (NSE symbols, Yahoo Finance format: SYMBOL.NS).

Edit these lists freely - they are plain symbol lists. Index constituents
change a few times a year, so refresh occasionally from nseindia.com.
"""

NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY",
    "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TMPV", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]

# NIFTY Next 50 (approximate constituents) - combined with NIFTY50 this
# gives a ~100 stock large-cap universe.
NIFTY_NEXT50 = [
    "ABB", "ADANIENSOL", "ADANIGREEN", "ADANIPOWER", "AMBUJACEM",
    "BAJAJHLDNG", "BANKBARODA", "BHEL", "BOSCHLTD", "BRITANNIA",
    "CANBK", "CGPOWER", "CHOLAFIN", "DABUR", "DIVISLAB",
    "DLF", "DMART", "GAIL", "GODREJCP", "HAVELLS",
    "HAL", "HYUNDAI", "ICICIGI", "ICICIPRULI", "INDHOTEL",
    "INDIGO", "IOC", "IRFC", "JINDALSTEL", "JSWENERGY",
    "LICI", "LODHA", "TMCV", "MOTHERSON", "NAUKRI",
    "NHPC", "PFC", "PIDILITIND", "PNB", "RECLTD",
    "SHREECEM", "SIEMENS", "SWIGGY", "TATAPOWER", "TORNTPHARM",
    "TVSMOTOR", "UNITDSPR", "VBL", "VEDL", "ZYDUSLIFE",
]

UNIVERSES = {
    "nifty50": NIFTY50,
    "nifty100": NIFTY50 + NIFTY_NEXT50,
}

# Fetched live from NSE's official index-constituent CSVs (cached 7 days) -
# mid/small-cap index churn is too high to hardcode.
_INDEX_CSVS = {
    "midcap150": "ind_niftymidcap150list.csv",
    "smallcap250": "ind_niftysmallcap250list.csv",
}

UNIVERSE_NAMES = sorted(UNIVERSES) + sorted(_INDEX_CSVS) + ["all"]


def _fetch_index_list(csv_name: str) -> list[str]:
    from . import data
    cached = data._cache_get("univ", csv_name, 7 * 86400)
    if cached is not None:
        return cached
    import requests
    symbols: list[str] = []
    try:
        resp = requests.get(
            f"https://archives.nseindia.com/content/indices/{csv_name}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        if resp.status_code == 200:
            lines = resp.text.splitlines()
            header = [h.strip().lower() for h in lines[0].split(",")]
            i = header.index("symbol")
            symbols = [ln.split(",")[i].strip() for ln in lines[1:]
                       if len(ln.split(",")) > i]
    except Exception:
        pass
    if symbols:
        data._cache_put("univ", csv_name, symbols)
    return symbols


def resolve(name: str) -> list[str]:
    """Universe name -> symbol list ('all' = nifty100 + mid + small caps)."""
    if name in UNIVERSES:
        return UNIVERSES[name]
    if name in _INDEX_CSVS:
        return _fetch_index_list(_INDEX_CSVS[name])
    if name == "all":
        out = list(UNIVERSES["nifty100"])
        for csv_name in _INDEX_CSVS.values():
            out += [s for s in _fetch_index_list(csv_name) if s not in out]
        return out
    return []


def to_yahoo(symbol: str) -> str:
    """NSE symbol -> Yahoo Finance ticker."""
    symbol = symbol.upper().strip()
    return symbol if symbol.endswith(".NS") else symbol + ".NS"


def from_yahoo(ticker: str) -> str:
    """Yahoo Finance ticker -> NSE symbol."""
    return ticker.removesuffix(".NS")
