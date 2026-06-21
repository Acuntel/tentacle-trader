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
        learned = self.know["catalyst_rules"].get(catalyst)
        if learned:
            r.update({k: learned[k] for k in ("stop_pct", "target_pct", "hold_days") if k in learned})
        return r

    # ---- calibration (#3): does a stated confidence match reality? ----
    def calib_factor(self, conf: float) -> float:
        return float(self.know["calibration"].get("factor", 1.0))

    # ---- THE simultaneous fusion — everything sharpens everything, in one pass ----
    def synthesize(self, patterns) -> dict:
        srcs = self.know["sources"]
        moved = []
        for src, st in srcs.items():
            if st.get("trades", 0) < self.min_samples:
                continue
            precision = st["wins"] / st["trades"]
            lead = self.lead_score(src)                       # earliness bonus
            base = self.weights.get(src, 0.5)
            target = base * (0.6 + precision) * (0.95 + 0.1 * lead)
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
            "synth_runs": k["meta"]["synth_runs"],
        }
