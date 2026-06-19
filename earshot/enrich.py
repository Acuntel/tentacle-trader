"""On-demand enrichment — the brain proactively collecting data it's missing.

When EARSHOT sees an UNDER-CONFIRMED setup (a promising ticker with only ONE signal family), it
fires targeted probes at that specific name (StockTwits sentiment + a fresh Google-News query) to
seek an independent second confirmation — then re-evaluates. That's "the brain realizes it needs to
learn more about something and goes to get it." Bounded per cycle + per-ticker cooldown so we never
hammer a source.
"""
from __future__ import annotations

import time
import urllib.parse
import requests
import feedparser

from .models import RawSignal, now_ts

UA = "Mozilla/5.0 (EARSHOT enrich)"


def _stocktwits(ticker: str, weight: float) -> list[RawSignal]:
    """Real-time per-ticker sentiment/buzz — adds a SOCIAL-family confirmation (long-biased)."""
    try:
        r = requests.get(f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json",
                         headers={"User-Agent": UA}, timeout=10)
        msgs = r.json().get("messages", [])
        if len(msgs) < 5:
            return []
        def s(m):
            return ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        bull = sum(1 for m in msgs if s(m) == "Bullish")
        bear = sum(1 for m in msgs if s(m) == "Bearish")
        if bull <= bear:
            return []                                   # only confirm LONGs
        ratio = bull / max(1, bull + bear)
        return [RawSignal(source="stocktwits", weight=weight, ts=now_ts(), tickers=[ticker],
                          headline=f"{ticker} StockTwits {bull}B/{bear}Be ({ratio:.0%} bull, {len(msgs)} msgs)",
                          url="https://stocktwits.com", strength=min(2.0, 0.5 + ratio))]
    except Exception:
        return []


def _gnews_ticker(ticker: str, weight: float) -> list[RawSignal]:
    """A fresh, targeted news query for this exact name — corroborates the NEWS family."""
    try:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f'"{ticker}" stock')
               + "&hl=en-US&gl=US&ceid=US:en")
        fp = feedparser.parse(requests.get(url, headers={"User-Agent": UA}, timeout=10).content)
        return [RawSignal(source="google_news", weight=weight, ts=now_ts(), tickers=[ticker],
                          headline=getattr(e, "title", "")[:160], url=getattr(e, "link", ""),
                          strength=0.6) for e in fp.entries[:5]]
    except Exception:
        return []


class EnrichmentEngine:
    def __init__(self, cfg: dict):
        e = cfg.get("enrichment", {})
        self.max_per_cycle = int(e.get("max_per_cycle", 2))
        self.cooldown = float(e.get("cooldown_sec", 1800))
        self.weights = None                              # shared weights dict, set by main
        self._probed: dict = {}

    def enrich(self, cands, buffer) -> int:
        """Probe under-confirmed promising candidates; append any new signals to the buffer."""
        if self.weights is None:
            return 0
        now = time.time()
        picked, added = 0, 0
        for c in cands:
            if picked >= self.max_per_cycle:
                break
            if getattr(c, "_families", 1) >= 2:          # already confirmed -> skip
                continue
            if now - self._probed.get(c.ticker, 0) < self.cooldown:
                continue
            self._probed[c.ticker] = now
            picked += 1
            for sig in (_stocktwits(c.ticker, self.weights.get("stocktwits", 0.35))
                        + _gnews_ticker(c.ticker, self.weights.get("google_news", 0.45))):
                buffer.append(sig)
                added += 1
        return added
