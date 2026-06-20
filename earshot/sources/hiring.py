"""Multi-ATS hiring velocity — extends the Greenhouse tentacle to Lever / Ashby / SmartRecruiters.

A jump in open roles (especially revenue-driving ones) front-runs growth — the alt-data funds pay
Thinknum/LinkUp for. Each public company exposes its board through ONE applicant-tracking system; this
covers three more so we catch names Greenhouse misses. Baseline-delta on the open-role count, entity is
the company->ticker. Family 'alt'. ⚠️ Public-company coverage on Lever/Ashby is thinner than Greenhouse
(most public cos are on Greenhouse or Workday-no-API) — the REGISTRY is the part that grows over time.
"""
from __future__ import annotations

import requests

from ..models import RawSignal, now_ts
from .base import Source
from .altdata import _load, _save

TIMEOUT = 12
UA = {"User-Agent": "Mozilla/5.0 (TENTACLE hiring research)"}

# (ats, account, ticker). Wrong/private accounts just 404 -> skipped (degrade loudly per entity).
REGISTRY = [
    ("smartrecruiters", "Visa", "V"),
    ("smartrecruiters", "Square", "XYZ"),
    ("smartrecruiters", "Twitch", "AMZN"),
    ("ashby", "ramp", "_private"),
    ("lever", "netflix", "NFLX"),
    ("lever", "palantir", "PLTR"),
]


class MultiAtsHiringSource(Source):
    key = "hiring_ats"
    min_interval = 21600.0           # 6h; hiring moves slowly
    PATH = "state/hiring_ats.json"

    def __init__(self, weights, batch=6):
        super().__init__(weights)
        self.reg = [r for r in REGISTRY if not r[2].startswith("_")]
        self.i = 0
        self.batch = batch

    def _count(self, ats, acct):
        try:
            if ats == "lever":
                r = requests.get(f"https://api.lever.co/v0/postings/{acct}?mode=json",
                                 headers=UA, timeout=TIMEOUT)
                d = r.json()
                return len(d) if isinstance(d, list) else None
            if ats == "ashby":
                r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{acct}",
                                 headers=UA, timeout=TIMEOUT)
                return len((r.json() or {}).get("jobs", []))
            if ats == "smartrecruiters":
                r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{acct}/postings",
                                 params={"limit": 1}, headers=UA, timeout=TIMEOUT)
                return int((r.json() or {}).get("totalFound") or 0)
        except Exception:
            return None
        return None

    def fetch(self) -> list[RawSignal]:
        prev = _load(self.PATH, {})
        out = []
        picks = [self.reg[(self.i + k) % len(self.reg)] for k in range(min(self.batch, len(self.reg)))]
        self.i = (self.i + self.batch) % len(self.reg)
        for ats, acct, tk in picks:
            n = self._count(ats, acct)
            if not n:
                continue
            kk = f"{ats}:{acct}"
            old = prev.get(kk)
            if old and n > old * 1.15 and n - old >= 5:
                out.append(RawSignal(
                    source=self.key, weight=self.weight(), ts=now_ts(), tickers=[tk],
                    headline=f"{tk} hiring surge ({ats}): {old}->{n} open roles (+{n-old})",
                    url=f"https://{ats}", strength=min(1.5, 0.6 + (n - old) / 50)))
            prev[kk] = n
        _save(self.PATH, prev)
        return out
