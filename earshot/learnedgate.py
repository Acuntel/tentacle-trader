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
            "analyst_conv", "is_filing", "is_options", "is_price"]


class LearnedGate:
    def __init__(self, know: dict, lr: float = 0.05):
        self.know = know
        m = know.get("model", {})
        self.w = {f: float(m.get("w", {}).get(f, 0.0)) for f in FEATURES}
        self.b = float(m.get("b", 0.0))
        self.n = int(m.get("n", 0))
        self.lr = lr

    @staticmethod
    def featurize(dossier: dict, verdict: dict) -> dict:
        t = dossier.get("tech") or {}
        o = dossier.get("options") or {}
        f = dossier.get("fundamentals") or {}
        p = dossier.get("pillars", [])
        return {
            "independence": dossier.get("independence", 0) / 5.0,
            "news_events": min(dossier.get("news_events", 0), 5) / 5.0,
            "opt_bias": float(o.get("bias", 0) or 0),
            "tech_bias": float(t.get("bias", 0) or 0),
            "squeeze": float(f.get("squeeze", 0) or 0),
            "quality": float(f.get("quality", 0.5) or 0.5),
            "analyst_conv": float(verdict.get("conviction", 50)) / 100.0,
            "is_filing": 1.0 if "filing" in p else 0.0,
            "is_options": 1.0 if "options" in p else 0.0,
            "is_price": 1.0 if "price" in p else 0.0,
        }

    def predict(self, feats: dict) -> float:
        z = self.b + sum(self.w.get(k, 0.0) * v for k, v in feats.items())
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))

    def update(self, feats: dict, win: bool):
        err = (1.0 if win else 0.0) - self.predict(feats)
        for k, v in feats.items():
            self.w[k] = self.w.get(k, 0.0) + self.lr * err * v
        self.b += self.lr * err
        self.n += 1
        self.know["model"] = {"w": self.w, "b": round(self.b, 4), "n": self.n}

    def ready(self, min_n: int = 30) -> bool:
        return self.n >= min_n
