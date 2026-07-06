"""#1 — Close the sim->live TRANSFER GAP.

The decade of history used to only train a parallel momentum-pattern namespace that barely reached the
live decision. This trains a SEPARATE P(win) model (`hist_model`) on a decade of catalyst-like events
represented in the SAME feature space the live brain decides on, labeled by forward ALPHA (return minus
SPY). The brain then BLENDS this historical prior into the live P(win) — so the whole decade informs
live decisions, while the live model stays pure (trained only on real live outcomes) and dominates.

Honest limitation: history has no real multi-source dossiers, so the filing/news/options pillars are
0 and the price/regime features carry the signal — this model learns "which price+regime setups precede
real alpha" across a decade. It's a PRIOR, blended at a low weight; live outcomes still own the call.
"""
from __future__ import annotations

import random

from . import history, marketadj
from .learnedgate import LearnedGate


class HistoricalTrainer:
    def __init__(self, cfg: dict, know: dict):
        h = cfg.get("histtrain", {})
        s = cfg.get("sim", {})
        self.on = bool(h.get("enabled", True))
        self.per_pass = int(h.get("events_per_pass", 120))
        self.move_pct = float(h.get("event_move_pct", 3.0))
        self.hold = int(s.get("hold_days", 5))
        self.stop = float(s.get("stop_pct", 8.0))
        self.target = float(s.get("target_pct", 12.0))
        self.gate = LearnedGate(know, lr=float(h.get("learn_rate", 0.02)), key="hist_model")
        self.trained = int(know.get("hist_model", {}).get("n", 0))

    @staticmethod
    def features(closes: list[float], i: int) -> dict:
        """A LIVE-feature vector built from price/technical history at a historical catalyst bar."""
        sma50 = sum(closes[i - 50:i]) / 50
        up = closes[i] > sma50
        mom = closes[i] / closes[i - 1] - 1
        gains = sum(max(0.0, closes[j] - closes[j - 1]) for j in range(i - 14, i)) / 14
        losses = sum(max(0.0, closes[j - 1] - closes[j]) for j in range(i - 14, i)) / 14
        rsi = (100 - 100 / (1 + gains / losses)) / 100 if losses > 0 else 0.7
        tech_b = max(-0.5, min(0.5, mom * 5 + (0.1 if up else -0.1)))
        conv = min(1.0, 0.4 + abs(mom) * 6)
        return {
            "independence": 0.4, "news_events": 0.2, "opt_bias": 0.0, "tech_bias": tech_b,
            "squeeze": 0.0, "quality": 0.5, "analyst_conv": conv,
            "is_filing": 0.0, "is_options": 0.0, "is_price": 1.0,
            "indep_x_conv": 0.4 * conv, "opt_x_tech": 0.0, "sqz_x_filing": 0.0,
            "risk_on": 1.0 if up else 0.0, "rates_down": 0.0, "downtrend": 0.0 if up else 1.0,
        }

    def step(self) -> dict:
        if not self.on:
            return {"trained": 0}
        tickers = history.cached_tickers()
        if len(tickers) < 5:
            return {"trained": 0}
        n, tries = 0, 0
        while n < self.per_pass and tries < self.per_pass * 8:
            tries += 1
            bars = history.load(random.choice(tickers))
            if len(bars) < self.hold + 60:
                continue
            dates = [d for d, _ in bars]
            closes = [c for _, c in bars]
            i = random.randint(55, len(closes) - self.hold - 1)
            if closes[i - 1] <= 0 or abs(closes[i] / closes[i - 1] - 1) * 100 < self.move_pct:
                continue
            entry, ret, j = closes[i], 0.0, i + self.hold
            for k in range(i + 1, i + 1 + self.hold):
                chg = (closes[k] / entry - 1) * 100
                if chg <= -self.stop:
                    ret, j = -self.stop, k; break
                if chg >= self.target:
                    ret, j = self.target, k; break
                ret, j = chg, k
            mkt = marketadj.market_return(dates[i], dates[min(j, len(dates) - 1)])
            if mkt is None:
                continue          # AUDIT FIX: no benchmark -> SKIP; the raw-return fallback silently
                                  # trained the persistent decade prior on beta, not alpha
            alpha = ret - mkt
            self.gate.update(self.features(closes, i), alpha > 0)   # LABEL BY ALPHA, live feature space
            n += 1
        self.trained += n
        return {"trained": n, "total": self.trained, "brier": round(self.gate.brier, 3)}
