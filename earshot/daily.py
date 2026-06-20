"""Daily circuit breaker.

If the day's losses hit the limit (default -10% of capital), TENTACLE halts for the rest of the
trading day, goes deep into the dungeon, and comes back the NEXT day more cautious — and it
stays more cautious until it earns trust back. Caution decays on clean days, ratchets up on
breached ones, and persists across restarts/redeploys (it's on the volume).
"""
from __future__ import annotations

import json
import os
import time

DAILY_PATH = "state/daily.json"


def _load(p, d):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return d


def _save(p, o):
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w") as f:
        json.dump(o, f, indent=2)


class DailyGuard:
    def __init__(self, cfg: dict):
        r = cfg["risk"]
        self.loss_pct = float(r.get("daily_loss_pct", 0.10))
        self.step = float(r.get("caution_step", 5))
        self.cmax = int(r.get("caution_max", 6))
        self.s = _load(DAILY_PATH, {"date": None, "caution": 0})

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def roll(self, equity_now: float):
        """At each new trading day: reset the day, and adjust caution based on yesterday."""
        d = self._today()
        if self.s.get("date") != d:
            breached = self.s.get("halted", False)
            caution = self.s.get("caution", 0)
            caution = min(self.cmax, caution + 1) if breached else max(0, caution - 1)
            self.s = {"date": d, "start_equity": equity_now, "low": equity_now,
                      "halted": False, "caution": caution, "resumed_after_breach": breached}
            _save(DAILY_PATH, self.s)

    def update(self, equity_now: float, capital: float) -> bool:
        """Returns True the moment a breach happens (so the caller can run deep reflection once)."""
        self.roll(equity_now)
        self.s["low"] = min(self.s.get("low", equity_now), equity_now)
        day_pnl = equity_now - self.s["start_equity"]
        newly = False
        if not self.s.get("halted") and capital > 0 and day_pnl <= -self.loss_pct * capital:
            self.s["halted"] = True
            self.s["breach_pnl"] = round(day_pnl, 2)
            newly = True
        _save(DAILY_PATH, self.s)
        return newly

    def halted(self) -> bool:
        return self.s.get("date") == self._today() and self.s.get("halted", False)

    def caution_bump(self) -> float:
        """Extra points added to the conviction bar — higher after recent bad days."""
        return self.s.get("caution", 0) * self.step

    def day_pnl(self, equity_now: float) -> float:
        return equity_now - self.s.get("start_equity", equity_now)
