"""24/7 simulation engine — the bot never stops learning, even when the market is shut.

Every cycle it replays random slices of the cached decade as paper episodes: find a real historical
up-catalyst (a big one-day move), enter long at that close, hold with the same stop/target discipline
the live bot uses, and score what actually happened next. Each episode teaches the PATTERN MEMORY
("do >5% up-moves in an uptrend drift further?") and grows a separate SIM track record so you can
watch the son learn and grow on history while the real market sleeps.

This is research, not live trading: sim outcomes train patterns + a sim equity curve the night coach
reads — they do NOT touch the real price-based mastery ledger or the live firing gate. Real money
stays gated on forward, real-time shadow results.
"""
from __future__ import annotations

import json
import os
import random
import time

from . import history

SIM_PATH = "state/sim.json"


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


class SimEngine:
    def __init__(self, cfg: dict, learner):
        s = cfg.get("sim", {})
        self.on = bool(s.get("enabled", True))
        self.per_cycle = int(s.get("episodes_per_cycle", 25))   # how many history replays each tick
        self.move_pct = float(s.get("event_move_pct", 3.0))     # what counts as a catalyst-like move
        self.hold = int(s.get("hold_days", 5))
        self.stop = float(s.get("stop_pct", 8.0))
        self.target = float(s.get("target_pct", 12.0))
        self.risk = float(s.get("sim_risk_frac", 0.10))         # notional risked per episode (for the curve)
        self.learner = learner
        start = float(cfg["survival"]["starting_capital"])
        self.state = _load(SIM_PATH, {
            "episodes": 0, "wins": 0, "sum_ret": 0.0,
            "sim_equity": start, "peak": start, "start": start, "last_ts": 0,
        })

    # ---- one historical episode: find an up-catalyst, hold it, score the drift ----
    def _episode(self, tickers: list[str]) -> dict | None:
        t = random.choice(tickers)
        bars = history.load(t)
        if len(bars) < self.hold + 60:
            return None
        closes = [c for _, c in bars]
        i = random.randint(55, len(closes) - self.hold - 1)
        if closes[i - 1] <= 0:
            return None
        r1 = (closes[i] / closes[i - 1] - 1) * 100
        if r1 < self.move_pct:                                   # only act on a real up-catalyst
            return None
        sma50 = sum(closes[i - 50:i]) / 50
        trend = "uptrend" if closes[i] > sma50 else "downtrend"
        mag = "huge" if r1 >= 8 else "big" if r1 >= 5 else "mid"
        sig = f"sim_mom|up|{mag}|{trend}"
        entry = closes[i]
        ret = 0.0
        for j in range(i + 1, i + 1 + self.hold):               # walk the daily closes, apply discipline
            chg = (closes[j] / entry - 1) * 100
            if chg <= -self.stop:
                ret = -self.stop; break
            if chg >= self.target:
                ret = self.target; break
            ret = chg
        return {"sig": sig, "ticker": t, "ret": round(ret, 3), "win": ret > 0, "move": round(r1, 2)}

    def step(self) -> dict:
        """Run a bounded batch of episodes, teach the pattern memory, grow the sim curve. 24/7-safe."""
        if not self.on:
            return {"ran": 0}
        tickers = history.cached_tickers()
        if len(tickers) < 5:
            return {"ran": 0, "note": "history still loading"}
        ran = 0
        for _ in range(self.per_cycle * 3):                     # extra tries since most days aren't events
            if ran >= self.per_cycle:
                break
            ep = self._episode(tickers)
            if not ep:
                continue
            ran += 1
            self.learner.patterns.record(ep["sig"], ep["win"], ep["ret"])   # learn the historical pattern
            self.state["episodes"] += 1
            self.state["wins"] += 1 if ep["win"] else 0
            self.state["sum_ret"] += ep["ret"]
            self.state["sim_equity"] *= (1 + self.risk * ep["ret"] / 100)
            self.state["peak"] = max(self.state["peak"], self.state["sim_equity"])
        if ran:
            self.state["last_ts"] = time.time()
            _save(SIM_PATH, self.state)
            self.learner._persist_know()                        # patterns ride along into knowledge.json
        return {"ran": ran}

    def report(self) -> dict:
        n = self.state["episodes"]
        wr = round(self.state["wins"] / n, 3) if n else 0.0
        avg = round(self.state["sum_ret"] / n, 3) if n else 0.0
        return {"episodes": n, "win_rate": wr, "avg_ret": avg,
                "sim_equity": round(self.state["sim_equity"], 2),
                "grown_pct": round((self.state["sim_equity"] / self.state["start"] - 1) * 100, 1)}

    def findings(self, top: int = 6) -> dict:
        """Best/worst historical momentum setups the sims have learned — fed to the night coach so
        a decade of replays turns into concrete proposed improvements."""
        rep = self.learner.patterns.report()
        sims = [r for r in rep.get("best", []) + rep.get("worst", []) if r["sig"].startswith("sim_")]
        sims.sort(key=lambda r: r["win"], reverse=True)
        return {"best": sims[:top], "worst": sims[-top:][::-1]}
