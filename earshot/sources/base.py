from __future__ import annotations

import json
import os
import re
import time
from ..models import RawSignal

TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")
# High-precision: press releases say "(NASDAQ: XYZ)" / "(NYSE: ABC)" right in the text.
EXCHANGE_RE = re.compile(
    r"\((?:NYSE American|NYSE|NASDAQ|Nasdaq|AMEX|OTCQB|OTCMKTS|OTC|CBOE)[:\s]+([A-Z]{1,5})\)")

# Name->ticker map for headline extraction (the Source Scout grows this over time).
NAME_TICKER = {
    "nvidia": "NVDA", "microsoft": "MSFT", "apple": "AAPL", "tesla": "TSLA",
    "amazon": "AMZN", "alphabet": "GOOGL", "google": "GOOGL", "meta platforms": "META",
    "facebook": "META", "broadcom": "AVGO", "palantir": "PLTR", "super micro": "SMCI",
    "micron": "MU", "qualcomm": "QCOM", "oracle": "ORCL", "netflix": "NFLX",
    "salesforce": "CRM", "adobe": "ADBE", "servicenow": "NOW", "snowflake": "SNOW",
    "advanced micro devices": "AMD", "arm holdings": "ARM", "intel": "INTC",
    "ionq": "IONQ", "rigetti": "RGTI", "d-wave": "QBTS", "quantum computing": "QUBT",
    "lockheed": "LMT", "raytheon": "RTX", "boeing": "BA", "northrop": "NOC",
    "general dynamics": "GD", "l3harris": "LHX", "leidos": "LDOS",
    "pfizer": "PFE", "moderna": "MRNA", "merck": "MRK", "eli lilly": "LLY",
    "amgen": "AMGN", "gilead": "GILD", "biogen": "BIIB", "vertex pharma": "VRTX",
    "regeneron": "REGN", "abbvie": "ABBV", "novavax": "NVAX", "sarepta": "SRPT",
    "jpmorgan": "JPM", "goldman sachs": "GS", "morgan stanley": "MS", "bank of america": "BAC",
    "wells fargo": "WFC", "citigroup": "C", "visa inc": "V", "mastercard": "MA",
    "berkshire hathaway": "BRK-B", "blackrock": "BLK", "coinbase": "COIN",
    "exxon": "XOM", "chevron": "CVX", "occidental": "OXY", "conocophillips": "COP",
    "walmart": "WMT", "costco": "COST", "home depot": "HD", "mcdonald": "MCD",
    "coca-cola": "KO", "pepsico": "PEP", "procter & gamble": "PG", "nike": "NKE",
    "disney": "DIS", "netflix inc": "NFLX", "uber": "UBER", "airbnb": "ABNB",
    "ford motor": "F", "general motors": "GM", "rivian": "RIVN", "lucid": "LCID",
    "taiwan semiconductor": "TSM", "asml": "ASML", "marvell": "MRVL", "micron technology": "MU",
}


def extract_tickers(text: str) -> list[str]:
    text = text or ""
    found = set(TICKER_RE.findall(text))
    found.update(EXCHANGE_RE.findall(text))           # precise press-release tickers
    low = text.lower()
    for name, tk in NAME_TICKER.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low):   # word-boundary -> few false positives
            found.add(tk)
    return sorted(found)


class SeenSet:
    """Persisted emit-once memory for a tentacle. A discrete event (a filing, an award, an insider
    row, an earnings report) should enter the brain ONCE, at first observation — not on every poll
    with a fresh timestamp. Re-emission defeats the buffer dedup + the slow-family TTL, makes stale
    events look eternally new, and turns one 8-K into an all-day drumbeat of fake confirmation
    (AUDIT: sec_fulltext, openinsider, finnhub_earnings and usaspending all had this). Capped FIFO,
    persisted on the volume so restarts don't re-emit history."""

    def __init__(self, name: str, cap: int = 4000):
        self.path = os.path.join("state", f"seen_{name}.json")
        self.cap = cap
        try:
            with open(self.path) as f:
                self._items = [str(x) for x in json.load(f)]
        except Exception:
            self._items = []
        self._set = set(self._items)

    def seen(self, key) -> bool:
        return str(key) in self._set

    def add(self, key):
        k = str(key)
        if k in self._set:
            return
        self._set.add(k)
        self._items.append(k)
        if len(self._items) > self.cap:
            drop = self._items[: len(self._items) - self.cap]
            self._items = self._items[len(drop):]
            self._set.difference_update(drop)
        try:
            os.makedirs("state", exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self._items, f)
        except Exception:
            pass


class Source:
    """Base tentacle. Subclasses set `key`, `min_interval` (seconds between polls), and `fetch()`."""
    key: str = "base"
    min_interval: float = 60.0      # how fast THIS tentacle may safely be polled (rate-limit aware)

    def __init__(self, weights: dict):
        self.weights = weights
        self.enabled = True
        self.errors = 0
        self._last = 0.0

    def weight(self, key: str | None = None) -> float:
        return float(self.weights.get(key or self.key, 0.5))

    def due(self) -> bool:
        return (time.time() - self._last) >= self.min_interval

    def fetch(self) -> list[RawSignal]:  # pragma: no cover - interface
        raise NotImplementedError

    def safe_fetch(self) -> list[RawSignal]:
        self._last = time.time()
        try:
            return self.fetch() or []
        except Exception as e:  # one bad tentacle never sinks the octopus
            self.errors += 1
            print(f"  [tentacle:{self.key}] error: {e}")
            return []
