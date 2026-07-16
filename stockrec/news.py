"""News aggregation and recency-weighted sentiment.

Sources:
  - Google News RSS (free, no key, broad Indian coverage, has timestamps)
  - Yahoo Finance news (via yfinance, already fetched in data.py)

Sentiment is VADER, weighted by headline age: today's news counts far more
than a three-week-old article.
"""

from __future__ import annotations

import math
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from . import data

_analyzer = SentimentIntensityAnalyzer()

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
RSS_TTL = 4 * 3600
HALF_LIFE_DAYS = 7.0        # a week-old headline carries half the weight


def _company_query(name: str, symbol: str) -> str:
    for suffix in (" Limited", " Ltd.", " Ltd"):
        name = name.removesuffix(suffix)
    return f'"{name or symbol}" stock'


def google_news(name: str, symbol: str, limit: int = 20) -> list[dict]:
    """Recent headlines from Google News RSS: [{title, age_days}, ...]."""
    cached = data._cache_get("gnews", symbol, RSS_TTL)
    if cached is not None:
        return cached

    query = urllib.parse.quote(_company_query(name, symbol))
    url = (f"https://news.google.com/rss/search?q={query}"
           f"&hl=en-IN&gl=IN&ceid=IN:en")
    items: list[dict] = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            root = ET.fromstring(resp.read())
        now = datetime.now(timezone.utc)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if " - " in title:      # strip trailing publisher name
                title = title.rsplit(" - ", 1)[0].strip()
            if not title:
                continue
            age_days = 30.0
            pub = item.findtext("pubDate")
            if pub:
                try:
                    age_days = max(
                        0.0, (now - parsedate_to_datetime(pub)).total_seconds() / 86400
                    )
                except (ValueError, TypeError):
                    pass
            items.append({"title": title, "age_days": age_days})
            if len(items) >= limit:
                break
    except Exception:
        pass

    data._cache_put("gnews", symbol, items)
    return items


def sentiment(name: str, symbol: str, yahoo_titles: list[str]) -> tuple[float, dict]:
    """Recency-weighted sentiment 0-100 across Google News + Yahoo headlines."""
    scored: list[tuple[float, float]] = []  # (compound, weight)

    for item in google_news(name, symbol):
        w = math.exp(-item["age_days"] * math.log(2) / HALF_LIFE_DAYS)
        c = _analyzer.polarity_scores(item["title"])["compound"]
        scored.append((c, max(w, 0.02)))

    # Yahoo gives no timestamp here; treat as moderately fresh.
    for title in yahoo_titles:
        scored.append((_analyzer.polarity_scores(title)["compound"], 0.5))

    if not scored:
        return 50.0, {"headlines": 0, "avg_compound": 0.0, "titles": []}

    total_w = sum(w for _, w in scored)
    avg = sum(c * w for c, w in scored) / total_w
    # With very few headlines, shrink toward neutral - low confidence.
    confidence = min(1.0, len(scored) / 6)
    score = 50 + avg * 50 * confidence

    titles = [i["title"] for i in google_news(name, symbol)[:5]] or yahoo_titles[:5]
    return max(0.0, min(100.0, score)), {
        "headlines": len(scored),
        "avg_compound": avg,
        "titles": titles,
    }
