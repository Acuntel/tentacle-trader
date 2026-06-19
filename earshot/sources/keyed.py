"""Free-tier tentacles that need an API key. They self-disable when the key is absent,
so the bot runs fine with zero keys and lights these up the moment you add one to .env."""
from __future__ import annotations

import datetime
import os
import requests

from ..models import RawSignal, now_ts
from .base import Source, extract_tickers

TIMEOUT = 12


class AlpacaNewsSource(Source):
    """Alpaca's free Benzinga newswire (REST poll here; a WebSocket upgrade is a drop-in later)."""
    key = "alpaca_news"

    def __init__(self, weights: dict):
        super().__init__(weights)
        self.kid = os.getenv("ALPACA_API_KEY")
        self.ksec = os.getenv("ALPACA_API_SECRET")
        self.enabled = bool(self.kid and self.ksec)
        if not self.enabled:
            print("  [tentacle:alpaca_news] inactive (no ALPACA_API_KEY)")

    def fetch(self) -> list[RawSignal]:
        r = requests.get(
            "https://data.alpaca.markets/v1beta1/news?limit=40",
            headers={"APCA-API-KEY-ID": self.kid, "APCA-API-SECRET-KEY": self.ksec},
            timeout=TIMEOUT,
        )
        out: list[RawSignal] = []
        for n in r.json().get("news", []):
            tickers = n.get("symbols") or extract_tickers(n.get("headline", ""))
            if not tickers:
                continue
            out.append(RawSignal(
                source=self.key, weight=self.weight(), ts=now_ts(),
                tickers=tickers, headline=n.get("headline", ""),
                url=n.get("url", ""), strength=1.0, raw={"summary": n.get("summary", "")},
            ))
        return out


class FinnhubSource(Source):
    """Finnhub free tier: market news + REAL earnings-surprise beats (the 'surprise' family).
    Earnings beats emit under source 'finnhub_earnings' so they count as a distinct signal family."""
    key = "finnhub"

    def __init__(self, weights: dict):
        super().__init__(weights)
        self.token = os.getenv("FINNHUB_API_KEY")
        self.enabled = bool(self.token)
        if not self.enabled:
            print("  [tentacle:finnhub] inactive (no FINNHUB_API_KEY)")

    def fetch(self) -> list[RawSignal]:
        out = self._news() + self._earnings()
        return out

    def _news(self) -> list[RawSignal]:
        r = requests.get(f"https://finnhub.io/api/v1/news?category=general&token={self.token}",
                         timeout=TIMEOUT)
        out: list[RawSignal] = []
        for n in r.json()[:40]:
            tickers = extract_tickers(f"{n.get('headline','')} {n.get('summary','')}")
            if tickers:
                out.append(RawSignal(source="finnhub", weight=self.weight("finnhub"), ts=now_ts(),
                                     tickers=tickers, headline=n.get("headline", ""),
                                     url=n.get("url", ""), strength=1.0))
        return out

    def _earnings(self) -> list[RawSignal]:
        # recently REPORTED earnings with a positive surprise -> the 'surprise' family
        today = datetime.date.today()
        frm = (today - datetime.timedelta(days=2)).isoformat()
        r = requests.get(f"https://finnhub.io/api/v1/calendar/earnings?from={frm}"
                         f"&to={today.isoformat()}&token={self.token}", timeout=TIMEOUT)
        out: list[RawSignal] = []
        for e in r.json().get("earningsCalendar", []):
            act, est, sym = e.get("epsActual"), e.get("epsEstimate"), e.get("symbol")
            if act is None or est in (None, 0) or not sym:
                continue
            surprise = (act - est) / abs(est)
            if surprise > 0.02:  # a beat
                out.append(RawSignal(
                    source="finnhub_earnings", weight=self.weight("finnhub_earnings"), ts=now_ts(),
                    tickers=[sym], headline=f"{sym} earnings beat: EPS {act} vs {est} est (+{surprise*100:.0f}%)",
                    url="https://finnhub.io", catalyst_hint="earnings_beat",
                    strength=min(2.0, 0.7 + surprise), raw=e,
                ))
        return out
