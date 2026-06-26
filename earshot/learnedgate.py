"""Learned P(win) — replaces 'count 2 sources' with a model trained on REAL outcomes.

A tiny online logistic regression over dossier features (independence, options/price bias, squeeze,
quality, analyst conviction, evidence types). It updates on every RESOLVED trade/observation and is
persisted in knowledge.json, so it sharpens daily. Advisory until it has enough samples to be trusted,
then the gate requires P(win) to clear a bar — confirmation by a model that has learned what wins,
not by arithmetic on headline counts.
"""
from __future__ import annotations

import math

FEATURES = ["independence", "news_events", "opt_bias", "tech_bias", "squeeze", "quality",
            "analyst_conv", "is_filing", "is_options", "is_price",
            # interactions (non-additive structure a flat linear model can't see)
            "indep_x_conv", "opt_x_tech", "sqz_x_filing",
            # regime conditioning (the same setup behaves differently across regimes)
            "risk_on", "rates_down", "downtrend"]


class LearnedGate:
    def __init__(self, know: dict, lr: float = 0.05):
        self.know = know
        m = know.get("model", {})
        self.w = {f: float(m.get("w", {}).get(f, 0.0)) for f in FEATURES}
        self.b = float(m.get("b", 0.0))
        self.n = int(m.get("n", 0))
        self.brier = float(m.get("brier", 0.25))      # running calibration quality (lower = better)
        self.lr = lr

    @staticmethod
    def featurize(dossier: dict, verdict: dict) -> dict:
        t = dossier.get("tech") or {}
        o = dossier.get("options") or {}
        f = dossier.get("fundamentals") or {}
        p = dossier.get("pillars", [])
        macro = (dossier.get("macro") or "").lower()
        indep = dossier.get("independence", 0) / 5.0
        news = min(dossier.get("news_events", 0), 5) / 5.0
        opt_b = float(o.get("bias", 0) or 0)
        tech_b = float(t.get("bias", 0) or 0)
        sqz = float(f.get("squeeze", 0) or 0)
        qual = float(f.get("quality", 0.5) or 0.5)
        conv = float(verdict.get("conviction", 50)) / 100.0
        is_filing = 1.0 if "filing" in p else 0.0
        is_options = 1.0 if "options" in p else 0.0
        is_price = 1.0 if "price" in p else 0.0
        return {
            "independence": indep, "news_events": news, "opt_bias": opt_b, "tech_bias": tech_b,
            "squeeze": sqz, "quality": qual, "analyst_conv": conv,
            "is_filing": is_filing, "is_options": is_options, "is_price": is_price,
            "indep_x_conv": indep * conv, "opt_x_tech": opt_b * tech_b, "sqz_x_filing": sqz * is_filing,
            "risk_on": 1.0 if "risk_on" in macro else 0.0,
            "rates_down": 1.0 if "rates_down" in macro else 0.0,
            "downtrend": 1.0 if "downtrend" in macro else 0.0,
        }

    def predict(self, feats: dict) -> float:
        z = self.b + sum(self.w.get(k, 0.0) * v for k, v in feats.items())
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def update(self, feats: dict, win: bool):
        pred = self.predict(feats)
        err = (1.0 if win else 0.0) - pred
        for k, v in feats.items():
            self.w[k] = self.w.get(k, 0.0) + self.lr * err * v
        self.b += self.lr * err
        self.n += 1
        self.brier = 0.99 * self.brier + 0.01 * (pred - (1.0 if win else 0.0)) ** 2   # EMA Brier
        self.know["model"] = {"w": self.w, "b": round(self.b, 4), "n": self.n,
                              "brier": round(self.brier, 4)}

    def ready(self, min_n: int = 30) -> bool:
        return self.n >= min_n

    def trust(self) -> float:
        """How much to trust the model's edge for sizing — 1.0 when well-calibrated (Brier≈0.18),
        fading to 0 as calibration degrades (Brier≈0.30). Gates Kelly-style sizing on real reliability."""
        if not self.ready():
            return 0.0
        return max(0.0, min(1.0, (0.30 - self.brier) / 0.12))
