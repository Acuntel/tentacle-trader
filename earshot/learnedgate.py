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
    def __init__(self, know: dict, lr: float = 0.05, key: str = "model",
                 warm: int = 300, lr_floor_ratio: float = 0.4):
        self.know = know
        self.key = key                                 # persist under this knowledge.json key
        m = know.get(key, {})
        self.w = {f: float(m.get("w", {}).get(f, 0.0)) for f in FEATURES}
        self.b = float(m.get("b", 0.0))
        self.n = int(m.get("n", 0))
        self.brier = float(m.get("brier", 0.25))      # running calibration quality (lower = better)
        self.lr = lr
        # WS3 adaptive learning rate: large while the model is IGNORANT (few samples), annealing to a
        # FLOOR as n grows so a flood of WS1 samples can't thrash it — but never to zero, so it keeps
        # tracking a non-stationary market. lr_eff = lr * max(floor_ratio, warm/(warm+n)).
        self.warm = max(1, int(warm))
        self.lr_floor_ratio = float(lr_floor_ratio)

    def _lr_eff(self) -> float:
        return self.lr * max(self.lr_floor_ratio, self.warm / (self.warm + self.n))

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
        feat = {
            "independence": indep, "news_events": news, "opt_bias": opt_b, "tech_bias": tech_b,
            "squeeze": math.tanh(sqz * 32.0), "quality": qual, "analyst_conv": conv,
            "is_filing": is_filing, "is_options": is_options, "is_price": is_price,
            "indep_x_conv": indep * conv, "opt_x_tech": opt_b * tech_b, "sqz_x_filing": sqz * is_filing,
            "risk_on": 1.0 if "risk_on" in macro else 0.0,
            "rates_down": 1.0 if "rates_down" in macro else 0.0,
            "downtrend": 1.0 if "downtrend" in macro else 0.0,
        }
        # WIDEN — log EVERYTHING the bot already measures so the model + the Forge can use it all, not a
        # subset. Every pillar type, source breadth, and every numeric tech/options/fundamentals signal.
        # Auto-included values are tanh-squashed to (-1,1) so raw magnitudes (prices, floats) can't
        # destabilize the online learner. New keys cost nothing: weights start at 0 (backward-compatible).
        for pil in ("regulatory", "crowd", "demand", "onchain", "flow", "altdata"):
            feat["is_" + pil] = 1.0 if pil in p else 0.0
        feat["n_src"] = min(float(dossier.get("n_sources", 0) or 0), 10) / 10.0
        for prefix, d in (("t_", t), ("o_", o), ("f_", f)):
            for k, v in (d.items() if isinstance(d, dict) else ()):
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if fv == fv and abs(fv) > 1e-9:                 # finite, non-zero (NaN-safe)
                    feat[prefix + str(k)] = round(math.tanh(fv), 4)
        return feat

    def predict(self, feats: dict) -> float:
        z = self.b + sum(self.w.get(k, 0.0) * v for k, v in feats.items())
        return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, z))))

    # NOTE (2026-07-03): a former `gate_score()` helper was DELETED here. It was created by a
    # self-change, never wired to a caller, and then became flypaper — the improve-code lane
    # "improved" it 7+ times because dead code always survives the gate (it can't break or regress
    # anything). Forge findings reach the live path through forge.champion_pwin() in brain.py.
    # If you (the self-coder) are reading this: only patch code with a real call site.

    def update(self, feats: dict, win: bool):
        pred = self.predict(feats)
        err = (1.0 if win else 0.0) - pred
        lr = self._lr_eff()                            # WS3: fast when ignorant, settles under the flood
        # L2 regularization on update: decay weights toward zero to prevent rare-feature blowup
        # from destabilizing the online learner under sample floods.
        reg = 1e-4 * lr
        for k, v in feats.items():
            self.w[k] = self.w.get(k, 0.0) * (1.0 - reg) + lr * err * v
        self.b += lr * err
        self.n += 1
        self.brier = 0.99 * self.brier + 0.01 * (pred - (1.0 if win else 0.0)) ** 2   # EMA Brier
        self.know[self.key] = {"w": self.w, "b": round(self.b, 4), "n": self.n,
                               "brier": round(self.brier, 4)}

    def ready(self, min_n: int = 30) -> bool:
        return self.n >= min_n

    def trust(self) -> float:
        """How much to trust the model's edge for sizing — 1.0 when well-calibrated (Brier≈0.18),
        fading to 0 as calibration degrades (Brier≈0.30). Gates Kelly-style sizing on real reliability."""
        if not self.ready():
            return 0.0
        return max(0.0, min(1.0, (0.30 - self.brier) / 0.12))
