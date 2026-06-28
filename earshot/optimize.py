"""Portfolio allocation — how to split capital across candidate positions to maximize expected ALPHA per
unit of risk, subject to the hard caps (per-name limit, budget, long-only).

For TENTACLE's reality ($500, a handful of long-only positions) this is a tiny convex mean-variance
problem solvable in closed form — no GPU, no external dependency, no rate limit. That's the PRIMARY path
(`allocate`). cuOpt (NVIDIA's GPU LP/MIP/routing solver) is genuine overkill here and its managed API is
a heavy async flow; it's wired as a documented ESCALATION (`cuopt_allocate`) for when the universe grows
to hundreds of names — until then it returns None and we use the local optimizer.

Mean-variance: maximize  Σ wᵢ·alphaᵢ − λ·Σ wᵢ²·varᵢ   s.t.  Σ wᵢ ≤ capital,  0 ≤ wᵢ ≤ cap·capital.
Per-name unconstrained optimum wᵢ* = alphaᵢ / (2λ·varᵢ); clip to [0, cap]; if the book exceeds capital,
scale proportionally. Negative-alpha names get 0 (long-only).
"""
from __future__ import annotations

import os


def allocate(candidates, capital: float, max_per_name: float = 0.5, risk_aversion: float = 8.0) -> dict:
    """candidates: [{ticker, alpha, risk}] — alpha = expected return (e.g. 0.04), risk = variance proxy
    (e.g. vol², default small). Returns {ticker: dollars}. Long-only, capped, budget-respecting."""
    cap_dollars = max(0.0, float(capital)) * float(max_per_name)
    raw = {}
    for c in candidates or []:
        t = c.get("ticker")
        a = float(c.get("alpha", 0) or 0)
        if not t or a <= 0:                      # long-only: skip non-positive expected alpha
            continue
        var = max(1e-4, float(c.get("risk", 0.04) or 0.04))
        w = a / (2.0 * float(risk_aversion) * var)      # mean-variance optimum
        w = max(0.0, min(cap_dollars, w * capital))     # clip to per-name cap (w is a fraction -> dollars)
        if w > 0:
            raw[t] = w
    total = sum(raw.values())
    if total <= 0:
        return {}
    if total > capital:                           # respect the budget — scale the whole book down
        scale = capital / total
        raw = {t: round(v * scale, 2) for t, v in raw.items()}
    else:
        raw = {t: round(v, 2) for t, v in raw.items()}
    return raw


def cuopt_allocate(candidates, capital: float, **kw):
    """ESCALATION: solve the allocation on NVIDIA cuOpt when the universe is large enough to justify a
    GPU LP solve. cuOpt's managed endpoint is an async NVCF submit/poll flow (heavyweight) and is overkill
    for a few-name book — so this stays OFF (returns None -> caller uses allocate()) until we have a large
    universe and turn it on. Key is staged as NV_CUOPT_KEY. See Acuntel/nvidia-skills for the API."""
    if not os.getenv("NV_CUOPT_KEY") or not kw.get("force"):
        return None
    return None  # intentionally not wired live yet — local optimizer is sufficient at current scale


def best_allocation(candidates, capital, max_per_name=0.5, risk_aversion=8.0):
    """Use cuOpt if explicitly enabled + worthwhile, else the local optimizer."""
    if len(candidates or []) > 50:
        r = cuopt_allocate(candidates, capital, force=True)
        if r:
            return r
    return allocate(candidates, capital, max_per_name, risk_aversion)
