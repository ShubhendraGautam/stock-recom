"""NSE India (nseindia.com) public site API client.

Provides two signals unavailable from Yahoo:
  - delivery percentage: share of traded volume actually taken to delivery
    (high = investors accumulating, low = intraday speculation)
  - FII/DII daily cash-market flows

The site requires a browser-like session (cookie warm-up) and dislikes
aggressive scraping, so requests are throttled and every failure degrades
to None rather than crashing a scan.
"""

from __future__ import annotations

import threading
import time

import requests

from . import data

_BASE = "https://www.nseindia.com"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
DELIVERY_TTL = 6 * 3600
_THROTTLE_S = 0.4

_lock = threading.Lock()
_session: requests.Session | None = None
_last_call = 0.0


def _get_json(path: str) -> dict | None:
    """Serialized, throttled GET against the NSE API. None on any failure."""
    global _session, _last_call
    with _lock:
        try:
            if _session is None:
                s = requests.Session()
                s.headers.update(_HEADERS)
                s.get(_BASE, timeout=10)          # cookie warm-up
                _session = s
            wait = _THROTTLE_S - (time.time() - _last_call)
            if wait > 0:
                time.sleep(wait)
            resp = _session.get(_BASE + path, timeout=10)
            _last_call = time.time()
            if resp.status_code != 200:
                _session = None                    # force re-warm next time
                return None
            return resp.json()
        except Exception:
            _session = None
            return None


def _delivery_map() -> dict[str, float]:
    """Delivery % for ALL NSE equities from the latest daily bhavcopy.

    The bhavcopy is an official public archive CSV (one file per trading
    day, all symbols) and is not bot-protected like the quote API. Walks
    back a few days to skip weekends/holidays. Cached for 6 hours.
    """
    with _deliv_lock:   # first scan: stop N workers downloading the same CSV
        return _delivery_map_locked()


_deliv_lock = threading.Lock()


def _delivery_map_locked() -> dict[str, float]:
    cached = data._cache_get("deliv", "all", DELIVERY_TTL)
    if cached is not None:
        return cached

    from datetime import date, timedelta

    result: dict[str, float] = {}
    d = date.today()
    for _ in range(8):
        url = ("https://archives.nseindia.com/products/content/"
               f"sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv")
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=25)
            if resp.status_code == 200 and not resp.content.startswith(b"<"):
                header_seen = False
                for line in resp.text.splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if not header_seen:
                        header_seen = True
                        continue
                    # SYMBOL, SERIES, ..., DELIV_PER (last column)
                    if len(parts) >= 15 and parts[1] == "EQ":
                        try:
                            result[parts[0]] = float(parts[-1])
                        except ValueError:
                            pass
                break
        except requests.RequestException:
            break
        d -= timedelta(days=1)

    if result:
        data._cache_put("deliv", "all", result)
    return result


def delivery_pct(symbol: str) -> float | None:
    """Latest delivery-to-traded-quantity percentage for an NSE symbol."""
    return _delivery_map().get(symbol.upper())


FO_TTL = 6 * 3600
DEALS_TTL = 6 * 3600

_fo_lock = threading.Lock()


def fo_positioning() -> dict[str, dict]:
    """Derivatives positioning per F&O stock from the official F&O bhavcopy.

    {symbol: {fut_oi, fut_oi_chg_pct, buildup, pcr}}
      buildup: how futures traders are positioned (price vs open interest):
        long buildup    - price up, OI up      (bullish conviction)
        short buildup   - price down, OI up    (bearish conviction)
        short covering  - price up, OI down    (mildly bullish)
        long unwinding  - price down, OI down  (mildly bearish)
      pcr: put/call open-interest ratio across the stock's options.
    """
    with _fo_lock:
        cached = data._cache_get("fo", "all", FO_TTL)
        if cached is not None:
            return cached

        import io
        from datetime import date, timedelta

        import pandas as pd

        raw = None
        d = date.today()
        for _ in range(8):
            url = ("https://archives.nseindia.com/content/fo/"
                   f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip")
            try:
                resp = requests.get(url, headers=_HEADERS, timeout=40)
                if resp.status_code == 200:
                    raw = resp.content
                    break
            except requests.RequestException:
                break
            d -= timedelta(days=1)
        if raw is None:
            return {}

        try:
            df = pd.read_csv(
                io.BytesIO(raw), compression="zip",
                usecols=["FinInstrmTp", "TckrSymb", "XpryDt", "OptnTp",
                         "ClsPric", "PrvsClsgPric", "OpnIntrst",
                         "ChngInOpnIntrst", "TtlTradgVol"],
            )
        except Exception:
            return {}

        result: dict[str, dict] = {}

        futs = df[df["FinInstrmTp"] == "STF"]
        for symbol, grp in futs.groupby("TckrSymb"):
            oi = float(grp["OpnIntrst"].sum())
            oi_chg = float(grp["ChngInOpnIntrst"].sum())
            prev_oi = max(oi - oi_chg, 1.0)
            # price direction from the most-traded (front) contract
            front = grp.sort_values("TtlTradgVol", ascending=False).iloc[0]
            price_up = float(front["ClsPric"]) >= float(front["PrvsClsgPric"])
            oi_up = oi_chg > 0
            buildup = ("long buildup" if price_up and oi_up else
                       "short buildup" if not price_up and oi_up else
                       "short covering" if price_up else "long unwinding")
            result[str(symbol)] = {
                "fut_oi": oi,
                "fut_oi_chg_pct": oi_chg / prev_oi * 100,
                "buildup": buildup,
                "pcr": None,
            }

        opts = df[df["FinInstrmTp"] == "STO"]
        for symbol, grp in opts.groupby("TckrSymb"):
            ce = float(grp.loc[grp["OptnTp"] == "CE", "OpnIntrst"].sum())
            pe = float(grp.loc[grp["OptnTp"] == "PE", "OpnIntrst"].sum())
            if ce > 0:
                entry = result.setdefault(
                    str(symbol),
                    {"fut_oi": None, "fut_oi_chg_pct": None,
                     "buildup": None, "pcr": None},
                )
                entry["pcr"] = pe / ce

        if result:
            data._cache_put("fo", "all", result)
        return result


def bulk_deals() -> dict[str, list[dict]]:
    """Latest-day bulk + block deals per symbol (official NSE archive CSVs)."""
    cached = data._cache_get("deals", "all", DEALS_TTL)
    if cached is not None:
        return cached

    result: dict[str, list[dict]] = {}
    for name, kind in (("bulk.csv", "bulk"), ("block.csv", "block")):
        try:
            resp = requests.get(
                f"https://archives.nseindia.com/content/equities/{name}",
                headers=_HEADERS, timeout=20,
            )
            if resp.status_code != 200:
                continue
            lines = resp.text.splitlines()
            for line in lines[1:]:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 7 or parts[0] == "NO RECORDS":
                    continue
                try:
                    qty = float(parts[5])
                    price = float(parts[6])
                except ValueError:
                    continue
                result.setdefault(parts[1], []).append({
                    "kind": kind, "date": parts[0], "client": parts[3],
                    "side": parts[4].upper(), "qty": qty, "price": price,
                })
        except requests.RequestException:
            continue

    data._cache_put("deals", "all", result)
    return result


def fo_ban_list() -> set[str]:
    """Symbols currently in the F&O ban period (official archive CSV)."""
    cached = data._cache_get("foban", "all", 6 * 3600)
    if cached is not None:
        return set(cached)
    symbols: set[str] = set()
    try:
        resp = requests.get(
            "https://nsearchives.nseindia.com/content/fo/fo_secban.csv",
            headers=_HEADERS, timeout=15)
        if resp.status_code == 200:
            for line in resp.text.splitlines()[1:]:
                parts = line.split(",")
                if len(parts) >= 2 and parts[-1].strip():
                    symbols.add(parts[-1].strip().upper())
    except requests.RequestException:
        pass
    data._cache_put("foban", "all", sorted(symbols))
    return symbols


def fii_dii() -> list[dict] | None:
    """Latest FII/DII cash-market activity (buy/sell/net, in ₹ crore)."""
    cached = data._cache_get("fiidii", "latest", 3600)
    if cached is not None:
        return cached
    payload = _get_json("/api/fiidiiTradeReact")
    if not isinstance(payload, list) or not payload:
        return None
    data._cache_put("fiidii", "latest", payload)
    return payload
