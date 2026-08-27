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

# W1.5: running per-feature stats for the auto-included RAW numeric tier (t_/o_/f_). tanh() straight
# on raw magnitudes (RSI ~60, market caps in dollars, strikes) froze the whole tier at a constant
# ±1.0 — z-score FIRST, then squash. featurize() is a staticmethod (analyst.py calls it on the
# class), so the registry lives at module level; LearnedGate seeds it from knowledge.json
# ("feature_stats") on boot and persists it back on update().
_RAW_STATS: dict = {}


def head(know: dict, key: str, **kw) -> "LearnedGate":
    """W2.1/W2.3: shared per-horizon / directional model heads. One PROCESS-WIDE instance per
    (know, key) so the Learner (which trains a head) and the Brain (which predicts from it) see the
    SAME in-memory weights — two separate LearnedGate(know, key=...) instances would silently read
    stale weights. Keys in use: "model" (the legacy 24h head), "model_h120" (the ~5-day hold head),
    "model_dir" (did the CHOSEN side beat the market — trains the direction call)."""
    k = (id(know), key)
    g = _HEADS.get(k)
    if g is None:
        g = _HEADS[k] = LearnedGate(know, key=key, **kw)
    return g


_HEADS: dict[tuple[int, str], "LearnedGate"] = {}


def _znorm(key: str, fv: float) -> float:
    """Welford running z-score -> tanh squash on a ~2-sigma scale. Bounded (-1,1), NaN-safe,
    neutral (0.0) until a feature has enough spread to carry information."""
    st = _RAW_STATS.setdefault(key, {"n": 0, "mean": 0.0, "m2": 0.0})
    st["n"] += 1
    d = fv - st["mean"]
    st["mean"] += d / st["n"]
    st["m2"] += d * (fv - st["mean"])
    if st["n"] < 2:
        return 0.0                                     # no scale info yet
    var = st["m2"] / (st["n"] - 1)
    if not (var > 1e-12):                              # constant so far (guards NaN too) -> no signal
        return 0.0
    z = (fv - st["mean"]) / math.sqrt(var)
    return math.tanh(z / 2.0) if z == z else 0.0


class LearnedGate:
    def __init__(self, know: dict, lr: float = 0.05, key: str = "model",
                 warm: int = 300, lr_floor_ratio: float = 0.4):
        self.know = know
        self.key = key                                 # persist under this knowledge.json key
        m = know.get(key, {})
        self.w = {f: float(m.get("w", {}).get(f, 0.0)) for f in FEATURES}
        self.b = float(m.get("b", 0.0))
        self.n = int(m.get("n", 0))
        self.brier = float(m.get("brier", 0.22))      # running calibration quality (lower = better)
        # W2.1: per-HEAD label base rate (EMA). The 1h/24h/120h horizons win at ~0.15/0.45/0.55 —
        # one global field rate misprices every head's no-skill floor, so each head tracks its own.
        self.base = float(m.get("base", 0.5))
        self.lr = lr
        # WS3 adaptive learning rate: large while the model is IGNORANT (few samples), annealing to a
        # FLOOR as n grows so a flood of WS1 samples can't thrash it — but never to zero, so it keeps
        # tracking a non-stationary market. lr_eff = lr * max(floor_ratio, warm/(warm+n)).
        self.warm = max(1, int(warm))
        self.lr_floor_ratio = float(lr_floor_ratio)
        # W1.5: seed the raw-feature normalizer from persisted stats so z-scores survive restarts
        for fk, fd in (know.get("feature_stats") or {}).items():
            if isinstance(fd, dict) and fd.get("norm_n", 0) > _RAW_STATS.get(fk, {}).get("n", 0):
                mu, m2 = float(fd.get("norm_mean", 0.0)), float(fd.get("norm_m2", 0.0))
                if math.isfinite(mu) and math.isfinite(m2):
                    _RAW_STATS[fk] = {"n": int(fd["norm_n"]), "mean": mu, "m2": m2}

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
        conv = min(max(float(verdict.get("conviction", 50)), 0), 100) / 100.0
        is_filing = 1.0 if "filing" in p else 0.0
        is_options = 1.0 if "options" in p else 0.0
        is_price = 1.0 if "price" in p else 0.0
        feat = {
            "independence": indep, "news_events": news, "opt_bias": opt_b, "tech_bias": tech_b,
            # AUDIT FIX: 8 self-changes ratcheted this scale 2->4->...->64; tanh(sqz*64) saturates at
            # sqz=0.05 (near-binary feature, information destroyed). sqz is already 0..1 — mild scale.
            "squeeze": math.tanh(sqz * 1.0), "quality": qual, "analyst_conv": conv,
            "is_filing": is_filing, "is_options": is_options, "is_price": is_price, "is_regulatory": -1.0 if "regulatory" in p else 0.0,
            "f_market_cap": _znorm("f_market_cap", float(f.get("market_cap", 0) or 0)), "o_max_pain": _znorm("o_max_pain", float(o.get("max_pain", 0) or 0)),
            "indep_x_conv": indep * conv, "opt_x_tech": opt_b * tech_b, "sqz_x_filing": sqz * is_filing, "sqz_x_regulatory": sqz * (1.0 if "regulatory" in p else 0.0),
            "risk_on": 1.0 if "risk_on" in macro else 0.0,
            "rates_down": 1.0 if "rates_down" in macro else 0.0,
            "downtrend": 1.0 if "downtrend" in macro else 0.0,
        }
        # WIDEN — log EVERYTHING the bot already measures so the model + the Forge can use it all, not a
        # subset. Every pillar type, source breadth, and every numeric tech/options/fundamentals signal.
        # Auto-included values are z-scored per feature (running stats, persisted) THEN tanh-squashed to
        # (-1,1). W1.5 AUDIT FIX: tanh straight on raw magnitudes (RSI ~60, caps in $, strikes) pinned
        # 7 features at a constant ~1.0 — a whole dead tier. New keys still cost nothing: weights start
        # at 0 (backward-compatible).
        for pil in ("regulatory", "crowd", "demand", "onchain", "flow", "altdata"):
            feat["is_" + pil] = 1.0 if pil in p else 0.0
        feat["n_src"] = min(float(dossier.get("n_sources", 0) or 0), 15) / 15.0
        feat["filing_events"] = min(float(dossier.get("filing_events", 0) or 0), 5) / 5.0
        for prefix, d in (("t_", t), ("o_", o), ("f_", f)):
            for k, v in (d.items() if isinstance(d, dict) else ()):
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(fv) and abs(fv) > 1e-9:        # finite, non-zero (NaN-safe)
                    feat[prefix + str(k)] = round(_znorm(prefix + str(k), fv), 4)
        # Forge-found features: t_bias, o_skew, f_squeeze are the strongest predictors.
        # Expose them explicitly so the online learner gets stable, named weights.
        feat["t_bias"] = feat.get("t_bias", 0.0)
        feat["o_skew"] = feat.get("o_skew", 0.0)
        feat["f_squeeze"] = feat.get("f_squeeze", 0.0)
        # Forge-found features: is_crowd and is_price are strong predictors; expose them explicitly
        # so the online learner gets stable, named weights (avoids dynamic key churn in knowledge.json).
        feat["is_crowd"] = feat.get("is_crowd", 0.0)
        feat["is_price"] = feat.get("is_price", 0.0)
        return feat

    def predict(self, feats: dict) -> float:
        # Only use features that have weights (avoids bloating the dot-product with
        # never-seen keys and keeps the model compact in knowledge.json).
        z = self.b + sum(self.w.get(k, 0.0) * v for k, v in feats.items() if k in self.w)
        return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, z))))

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
        self.base = 0.99 * self.base + 0.01 * (1.0 if win else 0.0)                    # per-head base rate
        self.know[self.key] = {"w": self.w, "b": round(self.b, 4), "n": self.n,
                               "brier": round(self.brier, 4), "base": round(self.base, 4)}

    def ready(self, min_n: int = 30) -> bool:
        return self.n >= min_n

    def trust(self) -> float:
        """How much to trust the model's edge for sizing — 0 at (or worse than) the NO-SKILL Brier,
        rising toward 1 as calibration genuinely beats it. AUDIT FIX: the no-skill floor depends on the
        label base rate — always predicting the field rate p scores Brier p(1-p) (~0.224 at p=0.34),
        NOT 0.25. The old fixed [0.18, 0.30] window granted a zero-skill predictor trust ~0.4-0.7,
        which then drove Kelly-style sizing. Skill is measured RELATIVE to the field's floor."""
        if not self.ready():
            return 0.0
        # W2.1: judge each head against ITS OWN label distribution — the per-head base rate once the
        # head has real samples (the global field rate blends horizons with 0.15..0.55 base rates,
        # mispricing every head's no-skill floor).
        if self.n >= 100:
            fld = min(0.95, max(0.05, self.base))
        else:
            try:
                from .patterns import field_rate
                fld = field_rate(self.know)
            except Exception:
                fld = 0.5
        b0 = max(0.05, fld * (1 - fld))                # the no-skill Brier for this label distribution
        return max(0.0, min(1.0, (b0 - self.brier) / (b0 * 0.35)))
