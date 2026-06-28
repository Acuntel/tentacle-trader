"""The Forge — TENTACLE's keystone. A FREE, deterministic, self-running recursive search engine wired to
the brain. Every tick it spawns a POPULATION of lightweight experiments (NO LLM — pure CPU, effectively
unlimited) that backtest candidate parameter values against the brain's REAL outcome ledger (walk-forward,
alpha-based), evolves toward whatever beats the live setting, and persists the population to the brain so
nothing is ever lost. It calls the LLM only as a RARE oracle when it PLATEAUS (a few tokens to suggest a
new direction). A robust winner becomes a ✅/❌ Telegram card -> CI gate.

This is the architecture Brady described: cheap massive search + rare tiny guidance. The LLM diagnoses;
the free engine executes the recursive improvement loop. The 3 rails are never in the search space.
"""
from __future__ import annotations

import json
import time

from .models import iso

LEDGER = "state/outcomes.jsonl"


def _load_ledger(limit: int = 2000):
    """Real outcomes as (confidence, alpha) pairs, in chronological (append) order."""
    rows = []
    try:
        with open(LEDGER) as f:
            for line in f:
                try:
                    o = json.loads(line)
                    c = o.get("confidence", o.get("conf"))
                    a = o.get("alpha", o.get("pnl_pct"))
                    if c is not None and a is not None:
                        rows.append((float(c), float(a)))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    return rows[-limit:]


def _score(thresh: float, rows, folds: int = 4):
    """Walk-forward objective: accept rows with conf>=thresh; require a POSITIVE mean alpha across the
    majority of time-ordered folds + enough accepted trades (anti-overfit). Returns overall mean alpha
    of accepted, or None if it doesn't clear the robustness bar."""
    n = len(rows)
    if n < 20:
        return None
    size = n // folds
    fold_means, total_acc = [], 0
    for k in range(folds):
        seg = rows[k * size:(k + 1) * size] if k < folds - 1 else rows[k * size:]
        acc = [a for c, a in seg if c >= thresh]
        total_acc += len(acc)
        if acc:
            fold_means.append(sum(acc) / len(acc))
    if total_acc < max(8, n * 0.05):                      # must still trade enough to matter
        return None
    if not fold_means or sum(1 for m in fold_means if m > 0) < (len(fold_means) + 1) // 2:
        return None                                       # not robust across time
    allacc = [a for c, a in rows if c >= thresh]
    return sum(allacc) / len(allacc) if allacc else None


class Forge:
    def __init__(self, cfg, know, proposer=None, persist=None):
        f = cfg.get("forge", {})
        self.cfg = cfg
        self.on = bool(f.get("enabled", True))
        self.every = int(f.get("every_minutes", 20)) * 60
        self.pop = int(f.get("population", 64))
        self.plateau_ticks = int(f.get("plateau_ticks", 6))
        self.min_gain = float(f.get("min_alpha_gain", 0.15))
        self.know = know
        self.proposer = proposer
        self.persist = persist or (lambda: None)
        self.fg = know.setdefault("forge", {})
        self.fg.setdefault("best", None)
        self.fg.setdefault("hist", [])
        self.fg.setdefault("flat", 0)
        self.last = 0.0

    def due(self) -> bool:
        return self.on and (time.time() - self.last) >= self.every

    def tick(self) -> dict:
        self.last = time.time()
        try:
            rows = _load_ledger()
            if len(rows) < 20:
                return {"skipped": "ledger too small", "n": len(rows)}
            confs = [c for c, _ in rows]
            lo, hi = min(confs), max(confs)
            if hi <= lo:
                return {"skipped": "no spread"}
            knob = "min_score_to_trade" if hi > 1.5 else "min_pwin"   # auto-detect scale (0-100 vs 0-1)
            # POPULATION of free experiments: a sweep across the observed range + mutations around the best
            cands = [round(lo + (hi - lo) * (i / (self.pop - 1)), 4) for i in range(self.pop)]
            best = self.fg.get("best")
            if best:
                cands += [round(best["thresh"] + (hi - lo) * d, 4) for d in (-0.05, -0.02, 0.02, 0.05)]
            scored = [(t, _score(t, rows)) for t in cands]
            scored = [(t, s) for t, s in scored if s is not None]
            if not scored:
                return {"experiments": len(cands), "viable": 0}
            scored.sort(key=lambda x: -x[1])
            top_t, top_s = scored[0]
            prev = (self.fg.get("best") or {}).get("score")
            improved = prev is None or top_s > prev + 1e-9
            self.fg["best"] = {"thresh": top_t, "score": round(top_s, 4), "knob": knob, "ts": iso(time.time())}
            self.fg["hist"] = (self.fg["hist"] + [round(top_s, 4)])[-50:]
            self.fg["flat"] = 0 if improved else self.fg.get("flat", 0) + 1
            proposed = self._maybe_propose(knob, top_t, top_s, rows)
            escalated = False
            if self.fg["flat"] >= self.plateau_ticks:          # plateau -> rare LLM oracle
                escalated = self._escalate(knob)
                self.fg["flat"] = 0
            self.persist()
            return {"experiments": len(cands), "viable": len(scored), "knob": knob,
                    "best_thresh": top_t, "best_alpha": round(top_s, 3),
                    "proposed": proposed, "escalated": escalated}
        except Exception as e:
            return {"error": str(e)[:140]}

    def _maybe_propose(self, knob, thresh, score, rows) -> bool:
        if self.proposer is None or not hasattr(self.proposer, "add_config"):
            return False
        try:
            cur = self.proposer._current_str(self.proposer._text(), knob)
        except Exception:
            cur = None
        if cur is None:
            return False
        cur_s = _score(float(cur), rows)
        if cur_s is None or score < cur_s + self.min_gain:     # must robustly beat the live value
            return False
        val = round(thresh) if knob == "min_score_to_trade" else round(thresh, 2)
        if str(val) == str(cur):
            return False
        label = "conviction bar" if knob == "min_score_to_trade" else "minimum win-chance"
        plain = f"Change my {label} from {cur} to {val}."
        why = (f"Free Forge search over {len(rows)} real outcomes: gating at {val} would have earned "
               f"~{score:.2f}% alpha vs ~{cur_s:.2f}% at the current {cur} (walk-forward, positive across "
               f"a majority of time folds).")
        return bool(self.proposer.add_config(knob, val, plain, why, impact=64))

    def _escalate(self, knob) -> bool:
        """Plateau -> enqueue ONE cheap Hive task asking the LLM for a new search direction (rare oracle)."""
        try:
            hv = self.know.setdefault("hive", {})
            q = hv.setdefault("queue", [])
            nid = hv.get("next_id", 1)
            hv["next_id"] = nid + 1
            h = f"design_strategy:forge-stuck-{knob}"
            if any(t.get("hash") == h for t in q):
                return False
            q.append({"id": f"H{nid:04d}", "hash": h, "kind": "design_strategy",
                      "spec": f"The free search plateaued tuning {knob}. Propose ONE new high-value "
                              f"parameter or signal the bot should start searching to find edge.",
                      "why": "forge plateau escalation", "prio": 70, "ts": iso(time.time())})
            return True
        except Exception:
            return False
