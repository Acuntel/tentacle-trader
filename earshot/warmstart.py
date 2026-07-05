"""Warm-start the predictive models on boot so a fresh volume / new deploy isn't a cold start.

Two priors, both bounded and deliberately WEAKER than live learning:
  1. REPLAY the resolved-observation ledger (state/resolved_obs.jsonl) — REAL dossier feature vectors
     with real win/loss labels — back through the P(win) model at a low learning rate.
  2. A momentum/trend PRIOR from a batch of historical sim episodes, mapped into the model's feature
     space at an even lower rate (live updates dominate it within a day).

Mastery and the graduation ladder are deliberately NOT warm-started — those stay earned on real
FORWARD outcomes only, so the bot can never warm-start itself into a high score (anti-self-deception).
This only makes the model that decides WHICH setups to trade start smarter — never the safety gate.
"""
from __future__ import annotations

import json
import os

RESOLVED = "state/resolved_obs.jsonl"


def _replay_ledger(lgate, cap: int) -> int:
    if not os.path.exists(RESOLVED):
        return 0
    try:
        with open(RESOLVED) as f:
            rows = f.read().splitlines()[-cap:]
    except Exception:
        return 0
    old_lr, lgate.lr = lgate.lr, min(lgate.lr, 0.03)
    n = 0
    for line in rows:
        try:
            r = json.loads(line)
        except Exception:
            continue
        feat = r.get("feat")
        if feat is not None and r.get("win") is not None:
            lgate.update(feat, bool(r["win"]))
            n += 1
    lgate.lr = old_lr
    return n


def _sim_prior(lgate, sim, episodes: int) -> int:
    from . import history
    tickers = history.cached_tickers()
    if len(tickers) < 5:
        return 0
    old_lr, lgate.lr = lgate.lr, min(lgate.lr, 0.01)      # a WEAK prior — live updates overrun it fast
    n, tries = 0, 0
    while n < episodes and tries < episodes * 8:
        tries += 1
        fe = sim.feature_episode(tickers)
        if not fe:
            continue
        lgate.update(fe[0], fe[1])
        n += 1
    lgate.lr = old_lr
    return n


def run(lgate, learner, sim, cfg) -> dict:
    w = cfg.get("warmstart", {})
    if not bool(w.get("enabled", True)):
        return {"skipped": "disabled"}
    # only warm-start a THIN model (fresh volume / post-reset) — never trample an already-trained one
    if lgate.n >= int(w.get("skip_above_n", 150)):
        return {"skipped": f"model already warm (n={lgate.n})"}
    replayed = _replay_ledger(lgate, int(w.get("ledger_cap", 5000)))
    primed = _sim_prior(lgate, sim, int(w.get("sim_episodes", 300)))
    learner.know.setdefault("meta", {})["warmstarted_n"] = lgate.n
    learner._persist_know()
    return {"replayed": replayed, "sim_prior": primed, "model_n": lgate.n}
