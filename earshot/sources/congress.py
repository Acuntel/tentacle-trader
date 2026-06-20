"""Congress-trading tentacle — politician disclosures as a (legal) smart-money read.

⚠️ HONEST: the FREE no-key mirrors died in 2026 (house/senate-stock-watcher S3 -> 403, CapitolTrades
-> Cloudflare-walled). The only remaining LIVE ticker-level feed is keyed; this uses FMP's free tier
and stays DARK until FMP_API_KEY is set (like FRED/Finnhub/Cloudflare). And it's a WEAK, SLOW signal:
disclosures lag the trade by up to 45 days and run sub-50% hit-rate, so it is confirmation-only, never
a trigger — low weight, long-side (purchases) only. Family 'flow'. Baseline-delta on new disclosures.
"""
from __future__ import annotations

import os
import requests

from ..models import RawSignal, now_ts
from .base import Source
from .altdata import _load, _save

TIMEOUT = 15
UA = {"User-Agent": "Mozilla/5.0 (TENTACLE research)"}
FEEDS = ("senate-trading-rss-feed", "house-disclosure-rss-feed")   # FMP v4; house name may vary by plan


class CongressTradesSource(Source):
    key = "congress"
    min_interval = 21600.0          # 6h — disclosures are slow and ~45-day lagged
    PATH = "state/congress_seen.json"

    def __init__(self, weights: dict):
        super().__init__(weights)
        self.token = os.getenv("FMP_API_KEY")
        self.enabled = bool(self.token)
        if not self.enabled:
            print("  [tentacle:congress] inactive (no FMP_API_KEY — free key lights it up; weak/stale signal)")

    def fetch(self) -> list[RawSignal]:
        if not self.token:
            return []
        seen = set(_load(self.PATH, []))
        out: list[RawSignal] = []
        for feed in FEEDS:
            try:
                r = requests.get(f"https://financialmodelingprep.com/api/v4/{feed}",
                                 params={"page": 0, "apikey": self.token}, headers=UA, timeout=TIMEOUT)
                rows = r.json()
                if not isinstance(rows, list):
                    continue
            except Exception:
                continue
            for t in rows[:60]:
                sym = (t.get("symbol") or t.get("ticker") or "").upper().strip()
                ttype = (t.get("type") or "").lower()
                if not sym or not sym.isalpha() or "purchase" not in ttype:    # long-side only
                    continue
                person = (t.get("lastName") or t.get("representative") or t.get("senator") or "member")
                uid = f"{sym}|{t.get('transactionDate','')}|{person}"
                if uid in seen:
                    continue
                seen.add(uid)
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=[sym],
                    headline=f"{sym} — Congress purchase disclosed ({person}, {t.get('amount','')})",
                    url=t.get("link", "https://disclosures-clerk.house.gov"),
                    catalyst_hint=None, strength=0.6))
        _save(self.PATH, sorted(seen)[-4000:])           # cap the seen-set; persist across redeploys
        return out
