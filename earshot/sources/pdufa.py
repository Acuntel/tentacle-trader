"""PDUFA run-up tentacle — a SCHEDULED catalyst, the perfect shape for a ~30-min-latency bot.

The documented pattern (research sweep 2026-08-05): biotechs accumulate in the 4-8 weeks BEFORE
an FDA decision date as traders position, and the disciplined play is to harvest the anticipation
and ALWAYS exit >=2 weeks before the binary event — never hold the coin flip. There is no latency
race here at all: the date is public weeks ahead.

The calendar comes free from the FDA prediction markets themselves (fdaodds.py): each Kalshi /
Polymarket FDA event carries a close date = the decision window. This source emits a pdufa_runup
signal while a name sits INSIDE the entry window (21-56 days before the decision); the decision
timestamp rides in raw["decision_ts"] so the brain can stamp the position with a hard
exit-by date (exits.py 'pre_event' — fires regardless of P&L).

Emits once per ticker per day (the calendar is slow); the regulatory family's 4h signal-memory
TTL gives each emission a real shot at cross-family confirmation.
"""
from __future__ import annotations

import time

from ..models import RawSignal, now_ts, iso
from .base import Source
from .. import fdaodds

ENTRY_MAX_DAYS = 56      # research window: accumulate 4-8 weeks out
ENTRY_MIN_DAYS = 21      # inside 3 weeks the run-up is mostly done AND the exit rule looms — don't enter


class PdufaRunupSource(Source):
    key = "pdufa"
    min_interval = 10800.0        # 3h — decision calendars move daily, not by the minute

    def __init__(self, weights: dict):
        super().__init__(weights)
        self._emitted: dict[str, str] = {}      # ticker -> last emit date (once/day)

    def fetch(self) -> list[RawSignal]:
        out: list[RawSignal] = []
        eng = fdaodds.engine()
        today = iso(time.time())[:10]
        now = time.time()
        for tk, ev in eng.calendar().items():
            days_out = (ev["close_ts"] - now) / 86400
            if not (ENTRY_MIN_DAYS <= days_out <= ENTRY_MAX_DAYS):
                continue
            if self._emitted.get(tk) == today:
                continue
            self._emitted[tk] = today
            o = eng.book.get(tk)
            odds_txt = f", approval odds {o['prob']:.0%}" if o else ""
            out.append(RawSignal(
                source=self.key, weight=self.weight(), ts=now_ts(), tickers=[tk],
                headline=(f"FDA decision window ~{days_out:.0f}d out for {tk} "
                          f"({ev['title'][:70]}{odds_txt}) — run-up window"),
                url="https://kalshi.com", catalyst_hint="pdufa_runup", strength=1.2,
                raw={"decision_ts": ev["close_ts"]},
            ))
        return out
