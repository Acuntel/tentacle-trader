"""The shared BRAIN CORE — the one place everything that learns lives and is analyzed together.

Every learner (live trades, observations, the 24/7 sims, catalyst studies, calibration, lead-time,
pattern decay, the rule optimizer, the P(win) model, red-team, the SPY benchmark) reads and writes
ONE substrate: knowledge.json. Nothing learns in a silo. Once per heartbeat, `synthesize()` looks at
ALL of it at the same time and FUSES it — a source's live weight becomes its precision × how early it
fires × whether its patterns have decayed — so each signal sharpens the others. `snapshot()` hands the
whole brain to the coach's LLM so it can reason across everything at once. That is how it gets
continuously better, 24/7.
"""
from __future__ import annotations

import time

from .learning import WMIN, WMAX
from .patterns import field_rate

SECTIONS = {
    "sources": dict, "catalysts": dict, "weights": dict, "patterns": dict, "model": dict,
    "lessons": list, "coach_notes": dict,
    "leadtime": dict,         # src -> {first, seen}  (how often it fires FIRST on a confirmed setup)
    "calibration": dict,      # {bins: {conf_bucket: {n, wins}}, factor: x}
    "catalyst_rules": dict,   # learned per-catalyst {stop_pct, target_pct, hold_days}
    "studies": dict,          # catalyst-specific backtest summaries
    "benchmark": dict,        # us vs SPY buy-and-hold
    "redteam": dict,          # last adversarial self-test result
    "growth": dict,           # milestones
    "stream": list,           # rolling daily signal-stream summary (source co-occurrence + novelty)
    "regime_timeline": list,  # the macro regime over time (so learning conditions on regime shifts)
    "feature_stats": dict,    # persisted win/loss feature history (which features predict wins)
    "meta": dict,             # {synth_runs, last_synth}
}


class BrainCore:
    def __init__(self, know: dict, weights: dict, cfg: dict):
        self.know = know
        self.weights = weights                       # SHARED with the live tentacles
        self.cfg = cfg
        for name, typ in SECTIONS.items():
            know.setdefault(name, typ())
        know["meta"].setdefault("synth_runs", 0)
        self.min_samples = int(cfg.get("coach", {}).get("min_source_samples", 8))

    # ---- lead-time: which source got to the setup FIRST (being early IS the edge) ----
    def record_leadtime(self, candidate):
        sigs = getattr(candidate, "signals", []) or []
        if len({s.source for s in sigs}) < 2:
            return
        earliest = min(sigs, key=lambda s: getattr(s, "ts", 0)).source
        lt = self.know["leadtime"]
        for s in {x.source for x in sigs}:
            d = lt.setdefault(s, {"first": 0, "seen": 0})
            d["seen"] += 1
            if s == earliest:
                d["first"] += 1

    def lead_score(self, src: str) -> float:
        d = self.know["leadtime"].get(src)
        return d["first"] / d["seen"] if d and d["seen"] >= 5 else 0.5

    # ---- learned per-catalyst trading rules (#1) merged over the config defaults ----
    def rule(self, catalyst: str, default: dict) -> dict:
        r = dict(default)
        # catalyst-specific learning wins; the decade-swept generic rule is only a FALLBACK
        learned = self.know["catalyst_rules"].get(catalyst) or self.know["catalyst_rules"].get("__default__")
        if learned:
            r.update({k: learned[k] for k in ("stop_pct", "target_pct", "hold_days") if k in learned})
        return r

    # ---- calibration (#3): does a stated confidence match reality? ----
    def calib_factor(self, conf: float) -> float:
        # confidence-AWARE calibration (audit fix 2026-07-13): use the per-bucket factor so a
        # high-confidence bucket that historically loses is haircut harder than a winning mid bucket.
        # Buckets MUST match evolve.update_calibration: <70 -> "55", <85 -> "70", else "85".
        cal = self.know.get("calibration", {})
        per = cal.get("per_bin")
        if per:
            b = "55" if conf < 70 else "70" if conf < 85 else "85"
            if b in per:
                return float(per[b])
        return float(cal.get("factor", 1.0))          # back-compat: pre-fix knowledge.json (global scalar)

    # ---- THE simultaneous fusion — everything sharpens everything, in one pass ----
    def synthesize(self, patterns) -> dict:
        srcs = self.know["sources"]
        fld = field_rate(self.know)                           # the shared alpha-win base rate (~0.34, not 0.5)
        priors = self.cfg.get("source_weights", {})
        moved = []
        for src, st in srcs.items():
            if st.get("trades", 0) < self.min_samples:
                continue
            precision = st["wins"] / st["trades"]
            lead = self.lead_score(src)                       # earliness bonus
            base = self.weights.get(src, 0.5)
            # FIELD-RELATIVE + PRIOR-ANCHORED (see learning._credit_sources): neutral = winning at the
            # field rate; the target orbits the config prior so a wrongly-floored weight self-heals.
            slope = 1.5 if precision >= fld else 3.0          # loss-averse: below-field cuts harder
            target = priors.get(src, 0.5) * (1.0 + slope * (precision - fld)) * (0.95 + 0.1 * lead)
            new = round(max(WMIN, min(WMAX, 0.85 * base + 0.15 * target)), 3)
            if abs(new - base) >= 0.005:
                self.weights[src] = new
                moved.append(src)
        decayed = patterns.refresh_decay() if patterns is not None else []
        self.know["decay"] = {"decayed": decayed}
        self.know["weights"] = self.weights                   # ride along into knowledge.json
        self.know["meta"]["synth_runs"] += 1
        self.know["meta"]["last_synth"] = time.time()
        return {"weights_moved": len(moved), "decayed_patterns": len(decayed)}

    # ---- the RECURSIVE growth ledger: snapshot the whole son daily, track the RATE of improvement ----
    def record_day(self, day: str, metrics: dict) -> dict:
        hist = self.know["growth"].setdefault("history", [])
        row = {"day": day, **metrics}
        if hist and hist[-1].get("day") == day:
            hist[-1] = row                                   # keep one row per day, latest wins
        else:
            hist.append(row)
        self.know["growth"]["history"] = hist[-180:]         # ~6 months of daily snapshots
        return self.trajectory()

    def trajectory(self) -> dict:
        """Is the son growing — and growing FASTER? The recursive target: improve the rate of improvement.
        The coach reads this and is told to accelerate it; the benchmark + calibration keep it honest."""
        hist = self.know["growth"].get("history", [])
        if len(hist) < 2:
            return {"days": len(hist), "note": "building the growth curve"}
        cur, prev = hist[-1], hist[-2]
        wk = hist[-8] if len(hist) >= 8 else hist[0]

        def dd(k):
            return round(float(cur.get(k, 0) or 0) - float(prev.get(k, 0) or 0), 3)

        def d7(k):
            return round(float(cur.get(k, 0) or 0) - float(wk.get(k, 0) or 0), 3)

        return {"days": len(hist), "mastery_dod": dd("mastery"), "mastery_7d": d7("mastery"),
                "sim_son_dod": dd("sim_son"), "sim_son_7d": d7("sim_son"),
                "edge_dod": dd("edge"), "patterns_7d": d7("n_patterns"),
                "improving": (d7("mastery") + d7("edge") + d7("sim_son")) > 0}

    # ---- WS6: LEARNING VELOCITY — measure the RATE of real learning, not just activity ----
    def velocity(self, lgate) -> dict:
        """Is it actually learning, and how fast? Tracks the things that move only on REAL signal:
        samples/day, the Brier-score trend (calibration improving = genuine learning; flat after
        thousands of samples = no edge), and the off-book learning trades' directional hit-rate + alpha.
        If lots of data has accrued and Brier still hasn't improved, it says NO EDGE DETECTED — honestly."""
        vcfg = self.cfg.get("velocity", {})
        ex = self.know.get("explore", {})
        n = int(getattr(lgate, "n", 0))
        brier = round(float(getattr(lgate, "brier", 0.25)), 4)
        trust = round(float(lgate.trust()), 3) if hasattr(lgate, "trust") else 0.0
        trades = int(ex.get("trades", 0))
        dir_hit = round(ex.get("dir_hits", 0) / trades, 3) if trades else None
        avg_alpha = round(ex.get("sum_alpha", 0.0) / trades, 3) if trades else None
        hist = self.know["growth"].get("history", [])
        smp = [h for h in hist if "samples" in h]
        bri = [h for h in hist if "brier" in h]
        # Rate = samples gained / DAYS elapsed between snapshots (they aren't always consecutive days).
        # NOTE: "day" is an ISO date STRING — a self-patch (A0097) called int() on it and crashed every
        # cycle. Parse real dates; fall back to 1 day.
        samples_per_day = None
        if len(smp) >= 2:
            try:
                import datetime as _dt
                _days = max(1, (_dt.date.fromisoformat(str(smp[-1]["day"]))
                                - _dt.date.fromisoformat(str(smp[-2]["day"]))).days)
            except Exception:
                _days = 1
            samples_per_day = max(0, int((smp[-1]["samples"] - smp[-2]["samples"]) / _days))
        brier_7d = None
        if len(bri) >= 2:
            ref = bri[-8] if len(bri) >= 8 else bri[0]
            brier_7d = round(bri[-1]["brier"] - ref["brier"], 4)        # negative = calibration improving
        eps = float(vcfg.get("improving_eps", 0.002))
        learning = brier_7d is not None and brier_7d <= -eps
        no_edge = (n >= int(vcfg.get("no_edge_min_samples", 1500))
                   and brier >= float(vcfg.get("no_edge_brier", 0.245))
                   and (brier_7d is None or brier_7d > -eps))
        # ---- "Is it on the RIGHT PATH?" verdict — the one-glance answer ----
        # Key on the HONEST gauges: mastery on real conviction trades (the built-in ladder = "ready to
        # trade for real" gauge) + the forward learning-trades' directional alpha. NOT the observation-
        # Brier (which can look great predicting easy near-zero drift). Smarter-on-the-right-path = mastery
        # climbing off 0 AND/OR the practice trades genuinely beating the market.
        mst = [h for h in hist if "mastery" in h]
        mastery_now = float(mst[-1]["mastery"]) if mst else 0.0
        mastery_7d = (round(mastery_now - float((mst[-8] if len(mst) >= 8 else mst[0])["mastery"]), 2)
                      if len(mst) >= 2 else None)
        days = len(hist)
        real_improvement = (mastery_7d is not None and mastery_7d > 1.0) or \
                           (avg_alpha is not None and avg_alpha > 0 and dir_hit is not None and dir_hit > 0.5)
        enough = (trades >= int(vcfg.get("verdict_min_trades", 200))
                  or days >= int(vcfg.get("verdict_min_days", 7)))
        if not enough:
            verdict = "TOO EARLY"
        elif real_improvement:
            verdict = "ON TRACK"
        elif no_edge or (mastery_now <= 0.1 and (avg_alpha or 0) <= 0):
            verdict = "SPINNING"
        else:
            verdict = "SETTLING"
        return {"n": n, "brier": brier, "trust": trust, "brier_7d": brier_7d,
                "samples_per_day": samples_per_day, "explore_trades": trades,
                "dir_hit": dir_hit, "avg_explore_alpha": avg_alpha,
                "learning": learning, "no_edge": no_edge,
                "mastery_now": mastery_now, "mastery_7d": mastery_7d, "days": days, "verdict": verdict}

    # ---- newly-wired data streams: signal flow, regime over time, feature history ----
    def record_stream(self, day: str, active_sources: list, n_signals: int, top_tickers: list):
        """The live signal stream stored over time -> source co-occurrence + novelty (which tickers
        light up across many tentacles at once = a fresh, real catalyst)."""
        s = self.know["stream"]
        row = {"day": day, "active_sources": active_sources[:30], "signals": n_signals,
               "top_tickers": top_tickers[:15]}
        if s and s[-1].get("day") == day:
            s[-1] = row
        else:
            s.append(row)
        self.know["stream"] = s[-120:]

    def record_regime(self, day: str, regime: str):
        """The macro regime timeline -> the brain can learn how each setup behaves ACROSS regime shifts."""
        tl = self.know["regime_timeline"]
        if not tl or tl[-1].get("regime") != regime:
            tl.append({"day": day, "regime": regime})
            self.know["regime_timeline"] = tl[-365:]

    def record_features(self, feats: dict, win: bool):
        """Persisted feature history -> which fundamental/quality features actually precede winners."""
        fs = self.know["feature_stats"]
        side = "win" if win else "loss"
        for k, v in (feats or {}).items():
            if isinstance(v, (int, float)):
                d = fs.setdefault(k, {"win_n": 0, "win_sum": 0.0, "loss_n": 0, "loss_sum": 0.0})
                d[f"{side}_n"] += 1
                d[f"{side}_sum"] += float(v)

    # ---- the whole brain in one view — fed to the coach so it reasons across EVERYTHING at once ----
    def snapshot(self) -> dict:
        k = self.know
        top_src = sorted(((s, round(v["wins"] / v["trades"], 2), v["trades"])
                          for s, v in k["sources"].items() if v.get("trades", 0) >= self.min_samples),
                         key=lambda r: -r[1])
        return {
            "source_precision": [{"src": s, "win": w, "n": n} for s, w, n in top_src[:12]],
            "weights_top": dict(sorted(self.weights.items(), key=lambda kv: -kv[1])[:10]),
            "leadtime": {s: round(self.lead_score(s), 2) for s in list(k["leadtime"])[:12]},
            "catalyst_rules": k["catalyst_rules"], "calibration_factor": self.calib_factor(0),
            "decayed_patterns": k.get("decay", {}).get("decayed", []),
            "studies": k["studies"], "benchmark": k["benchmark"], "redteam": k["redteam"],
            "synth_runs": k["meta"]["synth_runs"], "trajectory": self.trajectory(),
            "regime_now": (k["regime_timeline"][-1]["regime"] if k["regime_timeline"] else "n/a"),
            "regime_shifts": len(k["regime_timeline"]), "stream_days": len(k["stream"]),
            "feature_edge": {f: round(d["win_sum"] / d["win_n"] - d["loss_sum"] / d["loss_n"], 3)
                             for f, d in k["feature_stats"].items()
                             if d.get("win_n", 0) >= 5 and d.get("loss_n", 0) >= 5},
            "totals": {"sources_tracked": len(k["sources"]), "catalysts": len(k["catalysts"]),
                       "patterns_learned": len(k["patterns"]), "lessons": len(k["lessons"]),
                       "leadtime_tracked": len(k["leadtime"]), "catalyst_rules": len(k["catalyst_rules"]),
                       "growth_days": len(k["growth"].get("history", []))},
        }
