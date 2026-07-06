"""The Forge — TENTACLE's keystone. A FREE, deterministic, self-running recursive search engine wired to
the brain. Every tick it runs many GENERATIONS of a POPULATION of lightweight experiments (NO LLM — pure
CPU, effectively unlimited): hundreds of candidate gating POLICIES are scored, the best breed the next
hundreds, the worst are retired — generation over generation. It backtests against the brain's 1,500+ REAL
labeled setups (`resolved_obs.jsonl`: a 10-feature vector + the real market-adjusted return per setup) and
persists the champion to the brain so nothing is ever lost.

A policy = a weight per feature + a keep-fraction: score each setup = Σ wᵢ·featureᵢ, accept the top
keep-fraction. Scored WALK-FORWARD (cutoff learned on other folds, applied out-of-sample) on market-beating
return, robustness-gated. The LLM is touched only as a RARE oracle: on a strong finding or a plateau it
enqueues ONE cheap Hive task to turn the discovery into a concrete, human-gated change.

Architecture: cheap massive free search + rare tiny LLM guidance. The 3 rails are never in the search
space; the Forge only proposes — a human + the test-gate decide what ships.
"""
from __future__ import annotations

import json
import math
import random
import time

from .models import iso

RESOLVED = "state/resolved_obs.jsonl"
RET_CLIP = 25.0   # clip extreme returns so a few outliers can't dominate the objective


def _load_obs(limit: int = 4000):
    """Real labeled setups as (sanitized feature-dict, finite clipped return), chronological order.
    Skips rows with non-finite returns (delisted/no-price) and zeroes any non-finite feature."""
    rows = []
    try:
        with open(RESOLVED) as f:
            for line in f:
                try:
                    o = json.loads(line)
                    feat, ret = o.get("feat") or {}, o.get("ret")
                    if not feat or ret is None:
                        continue
                    ret = float(ret)
                    if not math.isfinite(ret):
                        continue
                    ret = max(-RET_CLIP, min(RET_CLIP, ret))
                    clean = {}
                    for k, v in feat.items():
                        try:
                            fv = float(v)
                            clean[k] = fv if math.isfinite(fv) else 0.0
                        except Exception:
                            clean[k] = 0.0
                    rows.append((clean, ret))
                except Exception:
                    continue
    except FileNotFoundError:
        return []
    return rows[-limit:]


def champion_pwin(know, feats, min_edge: float = 0.3, min_fwd: float = 0.0):
    """Score a setup by the Forge's CHAMPION policy → a 0-1 signal the live gate can blend in, but ONLY
    once the free search has PROVEN an edge that survives BOTH the walk-forward folds (champion.edge >=
    min_edge) AND a truly out-of-time FORWARD holdout (champion.forward_edge >= min_fwd). Until the
    forward holdout exists and is positive it returns None — historical search PROPOSES, forward data
    DISPOSES. This is how free deterministic search shapes live decisions — guarded + bounded."""
    ch = (know.get("forge") or {}).get("champion")
    if not ch or float(ch.get("edge", 0) or 0) < min_edge:
        return None
    fe = ch.get("forward_edge")
    if fe is None or float(fe) < min_fwd:              # must also survive the most-recent forward slice
        return None
    w, fn = ch.get("w") or [], ch.get("feats") or []
    if len(w) != len(fn) or not fn:
        return None
    # AUDIT FIX: the raw score z is a RANKING value with arbitrary scale/offset — squashing it through
    # a 0.5-centered sigmoid dragged nearly every live P(win) toward 0 (measured median 0.0023).
    # Anchor at the champion's own accept CUTOFF (score there = field-neutral) scaled by the training
    # score std, and center the output on the field base rate with bounded influence.
    cut, std = ch.get("cut"), ch.get("std")
    if cut is None or not std:
        return None                                    # pre-calibration champion -> no live influence
    from .patterns import field_rate
    fld = field_rate(know)
    z = sum(w[i] * float(feats.get(fn[i], 0) or 0) for i in range(len(fn)))
    return max(0.05, min(0.95, fld + 0.25 * math.tanh((z - float(cut)) / (2.0 * float(std)))))


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, max(0, int(q * (len(sorted_vals) - 1))))]


def _score_policy(weights, keep_frac, vecs, rets, folds=4):
    """Walk-forward objective with NO lookahead: for each time fold (test), learn the score cutoff from the
    OTHER folds (train) at the keep-fraction, apply out-of-sample, measure mean return of accepted. Returns
    overall mean return of accepted, or None if not robust (positive across a majority of folds) / too few
    accepted."""
    n = len(vecs)
    if n < 40:
        return None
    scores = [sum(w * v for w, v in zip(weights, vec)) for vec in vecs]
    size = n // folds
    fold_means, accepted_rets = [], []
    for k in range(folds):
        a, b = k * size, ((k + 1) * size if k < folds - 1 else n)
        cut = _percentile(sorted(scores[:a] + scores[b:]), 1.0 - keep_frac)
        acc = [rets[i] for i in range(a, b) if scores[i] >= cut]
        if acc:
            fold_means.append(sum(acc) / len(acc))
            accepted_rets += acc
    if len(accepted_rets) < max(20, int(0.03 * n)):
        return None
    if not fold_means or sum(1 for m in fold_means if m > 0) < (len(fold_means) + 1) // 2:
        return None
    return sum(accepted_rets) / len(accepted_rets)


class Forge:
    def __init__(self, cfg, know, proposer=None, persist=None):
        f = cfg.get("forge", {})
        self.cfg = cfg
        self.on = bool(f.get("enabled", True))
        self.every = int(f.get("every_minutes", 20)) * 60
        self.pop = int(f.get("population", 300))
        self.gens = int(f.get("generations_per_tick", 6))
        self.elite = max(2, int(self.pop * float(f.get("elite_frac", 0.2))))
        self.explore = max(1, int(self.pop * float(f.get("explore_frac", 0.1))))
        self.plateau_ticks = int(f.get("plateau_ticks", 6))
        self.min_gain = float(f.get("min_alpha_gain", 0.3))   # >=0.3% return-lift over accept-all to flag
        self.know = know
        self.proposer = proposer
        self.persist = persist or (lambda: None)
        self.fg = know.setdefault("forge", {})
        self.fg.setdefault("champion", None)
        self.fg.setdefault("hist", [])
        self.fg.setdefault("flat", 0)
        self.fg.setdefault("generation", 0)
        self.last = 0.0

    def due(self) -> bool:
        return self.on and (time.time() - self.last) >= self.every

    def _rand(self, nf):
        return {"w": [random.uniform(-1, 1) for _ in range(nf)], "keep": random.uniform(0.2, 0.7)}

    def _breed(self, a, b, nf):
        w = [max(-2.0, min(2.0, (a["w"][i] if random.random() < 0.5 else b["w"][i]) + random.gauss(0, 0.15)))
             for i in range(nf)]
        keep = min(0.7, max(0.2, (a["keep"] + b["keep"]) / 2 + random.gauss(0, 0.05)))
        return {"w": w, "keep": keep}

    # NOTE: a dead `_min_fold_return()` helper was DELETED here (zero callers; it referenced
    # self._percentile which is module-level, so it would have CRASHED if ever called — and it
    # had already absorbed one approved self-change, A0080). Self-coder: only patch called code.

    def tick(self) -> dict:
        self.last = time.time()
        try:
            rows = _load_obs()
            if len(rows) < 40:
                return {"skipped": "need >=40 labeled setups", "have": len(rows)}
            feats = sorted({k for feat, _ in rows for k in feat})
            vecs = [[float(feat.get(k, 0) or 0) for k in feats] for feat, _ in rows]
            rets = [r for _, r in rows]
            nf = len(feats)
            # WS4 FORWARD HOLDOUT: fit + select on the OLDER portion; hold out the most-recent slice as a
            # truly out-of-time test. The champion only earns LIVE influence (champion_pwin) if it ALSO
            # beats accept-all on this forward holdout — historical search proposes, forward data disposes.
            n = len(rows)
            fwd_frac = float(self.cfg.get("forge", {}).get("forward_frac", 0.2))
            cut = max(40, int(n * (1.0 - fwd_frac)))
            if n - cut < 15:                                    # not enough forward data yet -> no holdout
                cut = n
            train_vecs, train_rets = vecs[:cut], rets[:cut]
            hold_vecs, hold_rets = vecs[cut:], rets[cut:]
            # seed: the saved champion (continuity) + fresh randoms (the free swarm)
            pop = []
            champ = self.fg.get("champion")
            if champ and len(champ.get("w", [])) == nf:
                pop.append({"w": list(champ["w"]), "keep": champ.get("keep", 0.4)})
            while len(pop) < self.pop:
                pop.append(self._rand(nf))
            best_s, best_g, experiments = None, None, 0
            for _ in range(self.gens):                          # GENERATIONS: each breeds the next
                scored = []
                for g in pop:
                    experiments += 1
                    s = _score_policy(g["w"], g["keep"], train_vecs, train_rets)
                    if s is not None:
                        scored.append((s, g))
                if not scored:
                    pop = [self._rand(nf) for _ in range(self.pop)]
                    continue
                scored.sort(key=lambda x: -x[0])
                if best_s is None or scored[0][0] > best_s:
                    best_s, best_g = scored[0]
                elites = [g for _, g in scored[:self.elite]]
                pop = list(elites)
                while len(pop) < self.pop - self.explore:
                    pop.append(self._breed(random.choice(elites), random.choice(elites), nf))
                pop += [self._rand(nf) for _ in range(self.explore)]
            if best_g is None:
                return {"experiments": experiments, "viable": 0, "setups": len(rows)}
            self.fg["generation"] = self.fg.get("generation", 0) + self.gens
            prev = (self.fg.get("champion") or {}).get("alpha")
            improved = prev is None or best_s > prev + 1e-9
            self.fg["flat"] = 0 if improved else self.fg.get("flat", 0) + 1
            top = sorted(zip(feats, best_g["w"]), key=lambda x: -abs(x[1]))[:4]
            self.fg["champion"] = {"w": [round(x, 3) for x in best_g["w"]], "keep": round(best_g["keep"], 3),
                                   "alpha": round(best_s, 4), "feats": feats,
                                   "top": [f"{k}:{v:+.2f}" for k, v in top], "ts": iso(time.time())}
            self.fg["hist"] = (self.fg["hist"] + [round(best_s, 4)])[-50:]
            # calibration for champion_pwin: the accept CUTOFF (the field-neutral score) + score std,
            # so the live blend is anchored to what the champion actually means by "accept"
            bscores = sorted(sum(w * v for w, v in zip(best_g["w"], vec)) for vec in train_vecs)
            if bscores:
                bmean = sum(bscores) / len(bscores)
                bvar = sum((s - bmean) ** 2 for s in bscores) / max(1, len(bscores) - 1)
                self.fg["champion"]["cut"] = round(_percentile(bscores, 1.0 - best_g["keep"]), 4)
                self.fg["champion"]["std"] = round(max(1e-6, bvar ** 0.5), 4)
            baseline = sum(train_rets) / len(train_rets)        # accept-everything = the "do nothing" bar
            edge = best_s - baseline
            self.fg["champion"]["edge"] = round(edge, 4)        # walk-forward edge over accept-all (train)
            # FORWARD HOLDOUT edge: apply the champion to the held-out most-recent setups (out-of-time)
            fwd_edge = None
            if hold_rets:
                hscores = [sum(w * v for w, v in zip(best_g["w"], vec)) for vec in hold_vecs]
                hcut = _percentile(sorted(hscores), 1.0 - best_g["keep"])
                acc = [hold_rets[k] for k in range(len(hold_rets)) if hscores[k] >= hcut]
                if len(acc) >= 20:   # AUDIT FIX: 5 accepted holdout rows was a coin-flip sample — a
                                     # lucky handful could hand the champion live influence
                    fwd_edge = round(sum(acc) / len(acc) - sum(hold_rets) / len(hold_rets), 4)
            self.fg["champion"]["forward_edge"] = fwd_edge      # None until enough forward data; gates live use
            escalated = False
            if (edge >= self.min_gain and improved) or self.fg["flat"] >= self.plateau_ticks:
                escalated = self._escalate(self.fg["champion"], edge)
                self.fg["flat"] = 0
            self.persist()
            return {"generation": self.fg["generation"], "experiments": experiments, "viable": len(scored),
                    "setups": len(rows), "champion_alpha": round(best_s, 3), "vs_baseline": round(edge, 3),
                    "forward_edge": fwd_edge, "top_features": self.fg["champion"]["top"], "escalated": escalated}
        except Exception as e:
            return {"error": str(e)[:140]}

    def _escalate(self, champ, edge) -> bool:
        """Rare LLM oracle: hand the Hive the Forge's finding to turn into a concrete, reviewed, human-gated
        change. One cheap task — not per experiment."""
        try:
            hv = self.know.setdefault("hive", {})
            q = hv.setdefault("queue", [])
            nid = hv.get("next_id", 1)
            hv["next_id"] = nid + 1
            h = f"improve_code:forge-gate-gen{self.fg.get('generation')}"
            if any(t.get("hash") == h for t in q):
                return False
            q.append({"id": f"H{nid:04d}", "hash": h, "kind": "improve_code", "spec": "learnedgate.py",
                      "why": (f"Free Forge search found a gating policy worth ~{champ.get('alpha')}% return "
                              f"(+{round(edge, 2)}% vs accept-all); the features that matter most are "
                              f"{champ.get('top')}. ONLY touch code on the live path: featurize()/predict()/"
                              "update() are called from brain.evaluate + learning.resolve_*; the champion "
                              "already reaches the gate via forge.champion_pwin() in brain.evaluate. Do NOT "
                              "create new helper functions — nothing will call them."),
                      "prio": 75, "ts": iso(time.time())})
            return True
        except Exception:
            return False
