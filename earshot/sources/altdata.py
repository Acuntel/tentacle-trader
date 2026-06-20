"""Scrape-pack tentacles — orthogonal, company-specific data most retail never tracks.

Built in the house scraper style (spoofed UA, polite rotation, defensive). Two new arms:
  - SecFullTextSource: query EDGAR full-text search for high-signal PHRASES the moment they're filed
    (definitive merger agreement, strategic alternatives, going concern, exec resignations, BTD).
  - GreenhouseHiringSource: hiring-velocity from public Greenhouse job boards — an acceleration in
    open roles front-runs growth (this is the alt-data funds pay Thinknum/LinkUp for).
Both degrade to [] on any failure.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
import requests

from ..models import RawSignal, now_ts
from .base import Source

TIMEOUT = 12
TICKER_IN_PARENS = re.compile(r"\(([A-Z]{1,5})\)")


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f)
    except Exception:
        pass


class SecFullTextSource(Source):
    """EDGAR full-text search (free, no key) — catch a material phrase the instant it's filed."""
    key = "sec_fulltext"
    min_interval = 900.0      # 30 min between sweeps; we rotate 2 queries per fetch (polite to SEC)
    URL = "https://efts.sec.gov/LATEST/search-index"
    QUERIES = [
        ('"definitive merger agreement"', "mna_target", 1.0),
        ('"agreement and plan of merger"', "mna_target", 1.0),
        ('"strategic alternatives"', "mna_target", 0.8),
        ('"breakthrough therapy designation"', "fda_approval", 1.0),
        ('"receipt of FDA approval"', "fda_approval", 1.0),
        ('"awarded a contract"', "gov_contract", 0.7),
        ('"going concern"', None, 0.5),                  # risk flag (we're long-only -> a filter signal)
        ('"chief executive officer" "resignation"', None, 0.5),
    ]

    def __init__(self, weights: dict, ua: str):
        super().__init__(weights)
        self.ua = ua
        self.i = 0

    def fetch(self) -> list[RawSignal]:
        out: list[RawSignal] = []
        cutoff = (date.today() - timedelta(days=5)).isoformat()   # only FRESH filings are catalysts
        batch = [self.QUERIES[(self.i + k) % len(self.QUERIES)] for k in range(2)]
        self.i = (self.i + 2) % len(self.QUERIES)
        for q, hint, strength in batch:
            try:
                r = requests.get(self.URL, params={"q": q, "forms": "8-K", "dateRange": "custom",
                                                   "startdt": cutoff, "enddt": date.today().isoformat()},
                                 headers={"User-Agent": self.ua}, timeout=TIMEOUT)
                hits = (r.json().get("hits", {}) or {}).get("hits", [])
            except Exception:
                continue
            for h in hits[:20]:
                src = h.get("_source", {})
                if src.get("file_date", "") < cutoff:         # belt-and-suspenders recency guard
                    continue
                names = " ".join(src.get("display_names", []) or [])
                tickers = [t for t in TICKER_IN_PARENS.findall(names) if t != "CIK"]
                if not tickers:
                    continue
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=tickers[:1],
                    headline=f"SEC 8-K full-text match {q}: {names[:90]}",
                    url="https://efts.sec.gov/LATEST/search-index?q=" + q.replace(' ', '+'),
                    catalyst_hint=hint, strength=strength))
        return out


# Public-company Greenhouse board tokens (board token -> ticker). Wrong tokens just 404 -> skipped.
GREENHOUSE = {
    "coinbase": "COIN", "robinhood": "HOOD", "datadog": "DDOG", "cloudflare": "NET",
    "gitlab": "GTLB", "affirm": "AFRM", "roblox": "RBLX", "snowflakecomputing": "SNOW",
    "doordash": "DASH", "airbnb": "ABNB", "pinterest": "PINS", "asana": "ASAN",
    "samsara": "IOT", "confluent": "CFLT", "hashicorp": "HCP", "unity": "U",
    "sofi": "SOFI", "toast": "TOST", "lyft": "LYFT", "instacart": "CART",
}


class GreenhouseHiringSource(Source):
    """Hiring velocity — a jump in open roles front-runs revenue. Stateful surge detection (6h)."""
    key = "hiring"
    min_interval = 21600.0
    PATH = "state/hiring_counts.json"
    API = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"

    def __init__(self, weights: dict):
        super().__init__(weights)
        self.batch = 8
        self.i = 0
        self.tokens = list(GREENHOUSE.keys())

    def fetch(self) -> list[RawSignal]:
        prev = _load(self.PATH, {})
        out: list[RawSignal] = []
        picks = [self.tokens[(self.i + k) % len(self.tokens)] for k in range(self.batch)]
        self.i = (self.i + self.batch) % len(self.tokens)
        for tok in picks:
            try:
                r = requests.get(self.API.format(tok), headers={"User-Agent": "Mozilla/5.0"},
                                 timeout=TIMEOUT)
                n = len(r.json().get("jobs", []))
            except Exception:
                continue
            if not n:
                continue
            old = prev.get(tok)
            if old and n > old * 1.15 and n - old >= 5:
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=[GREENHOUSE[tok]],
                    headline=f"{GREENHOUSE[tok]} hiring surge: {old}->{n} open roles (+{n-old})",
                    url=f"https://boards.greenhouse.io/{tok}", strength=min(1.5, 0.6 + (n - old) / 50)))
            prev[tok] = n
        _save(self.PATH, prev)
        return out
