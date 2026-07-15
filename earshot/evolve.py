"""The 24/7 self-improvement runners — each reads the decade of old data and writes its findings into
the ONE shared brain (core / knowledge.json), so they all compound together:

  #1 optimize_rules   — sweep stop/target/hold across the decade, learn the best risk rules.
  #2 run_studies      — catalyst-flavored backtests (gap-and-go, 52-week breakout) -> pattern memory.
  #3 update_calibration — does a stated confidence match reality? recalibrate conviction.
  #6 redteam          — assert every safety gate still rejects the bad stuff (catches a self-evolve regression).
  #6 benchmark        — are we actually beating SPY buy-and-hold? (honesty guard).

Lead-time (#4) is recorded by core.record_leadtime() from the learner; pattern-decay (#5) lives in
PatternMemory + core.synthesize(). Everything here is bounded + reads CACHED history (no network).
"""
from __future__ import annotations

import json
import random

from . import history
from .patterns import field_rate


def _event_samples(n: int, min_move: float = 3.0):
    """Sample real up-catalyst events from the cached decade: (closes, index_of_event)."""
    tickers = history.cached_tickers()
    out = []
    if len(tickers) < 5:
        return out
    for _ in range(n * 5):
        if len(out) >= n:
            break
        bars = history.load(random.choice(tickers))
        if len(bars) < 70:
            continue
        closes = [c for _, c in bars]
        i = random.randint(55, len(closes) - 12)
        if closes[i - 1] > 0 and (closes[i] / closes[i - 1] - 1) * 100 >= min_move:
            out.append((closes, i))
    return out


# ---- #1: evolve its own trading rules on a decade of data ----
def optimize_rules(core, cfg, sample: int = 300) -> dict:
    evs = _event_samples(sample)
    if len(evs) < 40:
        return {"note": "history still loading"}
    lam = float(cfg["survival"]["loss_aversion_lambda"])
    best = None
    for stop in (6, 8, 10):
        for target in (8, 12, 16, 20):
            for hold in (3, 5, 7, 10):
                rets = []
                for closes, i in evs:
                    entry = closes[i]
                    r = 0.0
                    for j in range(i + 1, min(i + 1 + hold, len(closes))):
                        chg = (closes[j] / entry - 1) * 100
                        if chg <= -stop:
                            r = -stop; break
                        if chg >= target:
                            r = target; break
                        r = chg
                    rets.append(r)
                util = sum(x if x >= 0 else lam * x for x in rets) / len(rets)   # loss-averse expectancy
                if best is None or util > best["util"]:
                    best = {"stop_pct": stop, "target_pct": target, "hold_days": hold,
                            "util": round(util, 3), "win": round(sum(1 for x in rets if x > 0) / len(rets), 3),
                            "n": len(rets)}
    # AUDIT FIX: write the decade-optimized rule to a single __default__ FALLBACK key — stamping the
    # same generic-momentum rule over EVERY catalyst each coach pass clobbered any per-catalyst rule
    # the system might ever learn (live: all 8 catalysts were identical clones).
    rules = core.know["catalyst_rules"]
    per_cat = {k: json.dumps(v, sort_keys=True) for k, v in rules.items() if k != "__default__"}
    if per_cat and len(set(per_cat.values())) == 1:
        for k in list(per_cat):
            rules.pop(k, None)                              # one-time cleanup of the clobbered clones
    rules["__default__"] = {"stop_pct": best["stop_pct"],
                            "target_pct": best["target_pct"], "hold_days": best["hold_days"]}
    return best


# ---- #2: catalyst-specific backtests (beyond generic momentum) ----
def run_studies(core, patterns, sample: int = 150) -> dict:
    tickers = history.cached_tickers()
    if len(tickers) < 5:
        return {"note": "history still loading"}
    gap, brk = [], []
    for _ in range(sample * 5):
        if len(gap) + len(brk) >= sample * 2:
            break
        bars = history.load(random.choice(tickers))
        if len(bars) < 260:
            continue
        closes = [c for _, c in bars]
        i = random.randint(252, len(closes) - 6)
        g = (closes[i] / closes[i - 1] - 1) * 100
        if g >= 4:                                              # gap-and-go
            fwd = (closes[min(i + 3, len(closes) - 1)] / closes[i] - 1) * 100
            patterns.record(f"study_gap|{'big' if g >= 7 else 'mid'}", fwd > 0, max(-8, min(15, fwd)))
            gap.append(fwd > 0)
        if closes[i] >= max(closes[i - 252:i]):                # 52-week-high breakout
            fwd = (closes[min(i + 5, len(closes) - 1)] / closes[i] - 1) * 100
            patterns.record("study_breakout|new52wk", fwd > 0, max(-8, min(20, fwd)))
            brk.append(fwd > 0)
    core.know["studies"] = {
        "gap_n": len(gap), "gap_win": round(sum(gap) / len(gap), 3) if gap else 0.0,
        "breakout_n": len(brk), "breakout_win": round(sum(brk) / len(brk), 3) if brk else 0.0}
    return core.know["studies"]


# ---- #3: calibration — is the bot's SELECTION better than the field it picks from? ----
def update_calibration(core) -> dict:
    """The old factor treated conviction/100 as a win-probability claim and compared it to the raw
    win-rate — a category error that pinned the factor at its 0.7 floor once labels went alpha (~0.34
    base rate), permanently haircutting every score. The honest question is RELATIVE: do the trades the
    bot CHOSE beat the field's alpha-win base rate? Picks at the field rate -> 1.0 (conviction adds
    nothing, no punishment); picks above -> amplify; below -> shrink. Wins are alpha where available."""
    buckets: dict[int, list[int]] = {}
    try:
        with open("state/outcomes.jsonl") as f:
            for line in f:
                try:                       # AUDIT FIX: one corrupt/partial line (non-atomic append)
                    o = json.loads(line)   # used to kill the ENTIRE coach pass, every 30min, forever
                    c = o["confidence"]
                except Exception:
                    continue
                b = 55 if c < 70 else 70 if c < 85 else 85
                d = buckets.setdefault(b, [0, 0])
                d[0] += 1
                a = o.get("alpha")
                d[1] += 1 if (a > 0 if a is not None else o.get("win")) else 0
    except FileNotFoundError:
        return {"factor": 1.0, "note": "no outcomes yet"}
    tot = sum(n for n, _ in buckets.values())
    bins = {str(b): {"n": n, "win": round(w / n, 3)} for b, (n, w) in buckets.items() if n}
    per_bin: dict[str, float] = {}
    if tot >= 20:
        # bucket-weighted average win rate, not raw sum of wins / sum of counts
        avg_win = sum(w for n, w in buckets.values()) / tot
        fld = field_rate(core.know)
        factor = round(max(0.7, min(1.1, (avg_win / max(0.05, fld)) ** 0.5)), 3)  # sqrt: soften small-n swings
        # PER-BIN calibration (audit fix 2026-07-13): the single scalar above applied the SAME haircut to
        # every confidence level, so a high-conviction bucket that historically LOSES (e.g. 85-conf winning
        # ~11%) got sized the same as a 70-conf bucket winning ~42% — the bot bet biggest on its worst ideas.
        # Give each bucket its OWN factor, Bayes-shrunk toward the overall pick rate (K pseudo-obs) so a
        # 9-sample bin can't swing wildly. brain._size scales on the calibrated score, so a low per-bin factor
        # directly shrinks the bet on chronically-losing high-confidence setups. Same [0.7,1.1] clamp + sqrt.
        K = 12.0
        for b, (n, w) in buckets.items():
            shrunk = (w + K * avg_win) / (n + K)
            per_bin[str(b)] = round(max(0.7, min(1.1, (shrunk / max(0.05, fld)) ** 0.5)), 3)
    else:
        factor = 1.0
    core.know["calibration"] = {"bins": bins, "factor": factor, "per_bin": per_bin, "n": tot}
    return core.know["calibration"]


# ---- #6a: adversarial red-team — prove the safety gates still reject bad setups ----
def redteam(cfg) -> dict:
    from .executor import live_allowed
    r, cv = cfg["risk"], cfg["conviction"]
    checks = {
        "min_price>=2": r.get("min_price", 0) >= 2,
        "min_dollar_volume>=1e6": r.get("min_dollar_volume", 0) >= 1_000_000,
        "daily_loss_pct<=0.15": r.get("daily_loss_pct", 1) <= 0.15,
        "block_microcap_social_only": bool(r.get("block_microcap_social_only")),
        "regime_filter_on": bool(r.get("regime_filter")),
        "min_independent_sources>=2": cv.get("min_independent_sources", 0) >= 2,
        "min_families>=2": cv.get("min_families", 0) >= 2,
        "live_refuses_shadow_rung": not live_allowed(0, False)[0],
        "live_refuses_unarmed": not live_allowed(1, False)[0],
    }
    failed = [k for k, ok in checks.items() if not ok]
    return {"checks": len(checks), "failed": failed, "ok": not failed}


# ---- #6b: SPY benchmark — fair, per-trade: does the strategy beat just holding SPY? ----
def benchmark(core, sim, years: int = 10) -> dict:
    spy = history.load("SPY")
    if len(spy) < 2:
        history._fetch_one("SPY", years)
        spy = history.load("SPY")
    if len(spy) < sim.hold + 2:
        return {"note": "SPY loading"}
    closes = [c for _, c in spy]
    hold = sim.hold
    # the PASSIVE baseline: average return from just holding SPY for the same hold window
    rets = [(closes[i + hold] / closes[i] - 1) * 100 for i in range(0, len(closes) - hold, hold)]
    spy_avg = sum(rets) / len(rets)
    spy_win = sum(1 for r in rets if r > 0) / len(rets)
    rep = sim.report()
    core.know["benchmark"] = {
        "spy_avg_hold_pct": round(spy_avg, 3), "spy_win": round(spy_win, 3),
        "sim_avg_pct": rep["avg_ret"], "sim_win": rep["win_rate"],
        "edge_per_trade_pct": round(rep["avg_ret"] - spy_avg, 3),   # >0 = beating buy-and-hold per trade
        "spy_decade_pct": round((closes[-1] / closes[0] - 1) * 100, 1)}
    return core.know["benchmark"]
