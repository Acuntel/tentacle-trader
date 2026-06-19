"""MarketNewsSource — OUR OWN newswire (the free replacement for Alpaca's Benzinga feed).

Alpaca's news tentacle = Benzinga's wire pushed over WebSocket (structured headline + symbols).
We don't need Alpaca: Benzinga itself republishes the primary wires (GlobeNewswire, PR Newswire,
Business Wire) plus fast market media (CNBC/MarketWatch/Yahoo/Seeking Alpha) — all FREE via RSS.
This source aggregates them, extracts tickers (incl. precise '(NASDAQ: XYZ)' press-release parsing),
dedupes, and emits the same structured news-family signals. See ALPACA_ANALYSIS.md.

Tradeoff vs Alpaca: poll-based (~60s) + broader/noisier instead of push/Benzinga-curated — fine,
because our edge is multi-source DRIFT (minutes–hours), not the millisecond gap.
"""
from __future__ import annotations

import requests
import feedparser

from ..models import RawSignal, now_ts
from .base import Source, extract_tickers

TIMEOUT = 10
UA = "Mozilla/5.0 (EARSHOT newswire aggregator)"

# (url, source_key, strength) — press releases are primary (high weight 'newswire'),
# market media is secondary ('google_news', lower weight). Both are the 'news' family.
FEEDS = [
    ("https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire", "newswire", 1.0),
    ("https://www.prnewswire.com/rss/news-releases-list.rss", "newswire", 1.0),
    ("https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss", "newswire", 1.0),
    ("https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWA==", "newswire", 1.0),
    ("https://www.cnbc.com/id/10000664/device/rss/rss.html", "google_news", 0.9),   # markets
    ("https://www.cnbc.com/id/15839135/device/rss/rss.html", "google_news", 0.9),   # earnings
    ("http://feeds.marketwatch.com/marketwatch/realtimeheadlines/", "google_news", 0.9),
    ("https://finance.yahoo.com/news/rssindex", "google_news", 0.8),
    ("https://seekingalpha.com/market_currents.xml", "google_news", 0.9),
]


class MarketNewsSource(Source):
    """One tentacle, many free wires = our self-owned Benzinga substitute."""
    key = "newswire"
    min_interval = 30.0          # press-release wires update every few min; 30s catches them fast

    def __init__(self, weights: dict, extra_feeds: list | None = None):
        super().__init__(weights)
        self.feeds = list(FEEDS)
        for url in (extra_feeds or []):
            self.feeds.append((url, "newswire", 1.0))

    def fetch(self) -> list[RawSignal]:
        out: list[RawSignal] = []
        seen = set()
        for url, skey, strength in self.feeds:
            try:
                r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
                feed = feedparser.parse(r.content)
            except Exception as e:
                print(f"    [newswire feed down] {url.split('/')[2]}: {e}")
                continue
            for e in feed.entries[:25]:
                title = getattr(e, "title", "")
                tickers = extract_tickers(f"{title} {getattr(e, 'summary', '')}")
                if not tickers:
                    continue
                key = (tuple(tickers), title[:80])
                if key in seen:
                    continue
                seen.add(key)
                out.append(RawSignal(
                    source=skey, weight=self.weight(skey), ts=now_ts(),
                    tickers=tickers, headline=title, url=getattr(e, "link", ""),
                    strength=strength,
                ))
        return out
