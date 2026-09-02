"""MotherInstinct — how the bot values its capital (its SON) and earns access to more.

The capital is the mother's SON. She is raising him, and she wants two things at once:
  • GROW HIM AS LARGE AS HE CAN POSSIBLY BE — she trains him to perform at his best and
    compound toward a stretch target (growth_target x the starting stake). Growing = pride.
  • NEVER LET HIM DIE — loss is felt ~lambda x harder than gain; zeroing him = death.
The growth ambition and the loss aversion pull together: a great mother is ambitious for her
son AND ruin-averse. She climbs a ladder to earn him more capital, and a drawdown demotes her.
Hard caps live in risk.py — this is the *psychology* (drive + fear), not the law.
"""
from __future__ import annotations

import json
import math
import os
import time

LADDER_PATH = "state/ladder.json"


from .persist import load_json as _load, save_json as _save   # atomic + .bak — never lose progress


def _wilson_lower(wins: int, n: int, z: float = 1.64) -> float:
    """Lower bound of the true win rate (90% conf). The bot CANNOT luck into a high score on a
    small sample — small n drags this far below the raw rate. Anti-overestimation."""
    if n == 0:
        return 0.0
    p = wins / n
    d = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (center - margin) / d)


_FLD_CACHE = {"t": 0.0, "v": None}


def _live_field() -> float | None:
    """R1: the live alpha-win base rate from knowledge.json (patterns.field_rate math), cached 10min.
    Read lazily here so survival needs no new wiring from main/learning (rails stay isolated);
    None (no state yet / any failure) -> compute_mastery falls back to the legacy math."""
    if time.time() - _FLD_CACHE["t"] > 600:
        try:
            from .persist import load_json
            from .patterns import field_rate
            k = load_json("state/knowledge.json", None)
            _FLD_CACHE["v"] = field_rate(k) if k else None
        except Exception:
            _FLD_CACHE["v"] = None
        _FLD_CACHE["t"] = time.time()
    return _FLD_CACHE["v"]


def compute_mastery(recent: list[dict], start: float, field: float | None = None) -> dict:
    """Composite 0..100 over resolved outcomes {conf, pnl_pct, alpha, win}, derived ONLY from real,
    price-based outcomes — no self-judgment. SKILL is measured on ALPHA (return minus the market), so
    a rising tide can't inflate the score; the drawdown term still uses the RAW equity (real risk).
    A conservative win-rate lower bound + an overconfidence brake keep it honest. Reused by the Gauntlet.

    RAIL FIX R1 (2026-07-15, needs Brady's sign-off): `field` = the live alpha-win base rate (~0.35).
    conf/100 is a 0-100 conviction SCORE born in a coin-flip world; comparing it raw against alpha
    labels whose base rate is ~0.35 charged a STRUCTURAL ~0.3 'overconfidence' the bot could never
    escape — the brake made the ladder's rung-0 bar (60) mathematically unreachable (ceiling ~58.6,
    live mastery pinned ~10 while climbing). With `field` set, the conviction claim is rescaled into
    the label's world (claim = conf/100 x field/0.5): a bot performing AT field with conviction ~2x
    field now carries ZERO structural penalty; only genuine over-belief is braked. `field=None`
    preserves the legacy math exactly (the Gauntlet/backtest callers pass nothing)."""
    n = len(recent)
    if n == 0:
        return {"mastery": 0.0, "win_rate": 0, "win_lb": 0, "profit_factor": 0, "max_dd": 0,
                "calibration": 0, "overconfidence": 0, "expectancy": 0, "n": 0}

    def alpha(r):
        return r.get("alpha", r["pnl_pct"])                # alpha label (falls back to raw for old rows)

    wins = sum(1 for r in recent if r["win"])              # win = beat the market (alpha > 0)
    raw_win = wins / n
    win_lb = _wilson_lower(wins, n)                         # conservative — can't game small samples
    gains = sum(alpha(r) for r in recent if alpha(r) > 0)
    losses = -sum(alpha(r) for r in recent if alpha(r) < 0)
    pf = (gains / losses) if losses > 0 else (3.0 if gains > 0 else 0.0)
    pf_norm = max(0.0, min(1.0, (pf - 1) / 2))
    eq, peak, maxdd = start, start, 0.0
    for r in recent:                                       # real drawdown is on RAW $ moves
        # AUTOPSY FIX 2026-09-01: size-weighted. Compounding every trade's % at 100% of equity
        # read a 43% drawdown on a book whose real drawdown was 18%. Legacy rows without a size
        # assume a quarter-position (the typical band).
        eq *= (1 + r["pnl_pct"] / 100 * float(r.get("size_frac", 0.25)))
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak)
    scale = (field / 0.5) if field else 1.0                # R1: rescale the claim into the label's world
    claims = [min(1.0, r["conf"] / 100 * scale) for r in recent]
    avg_conf = sum(claims) / n
    calibration = 1 - sum(abs(c - (1 if r["win"] else 0)) for c, r in zip(claims, recent)) / n
    overconf = max(0.0, avg_conf - raw_win)                 # it believed more than it delivered
    sample = min(1.0, n / 30)
    mean_alpha = sum(alpha(r) for r in recent) / n         # expectancy is mean ALPHA (skill)
    expectancy = max(0.0, min(1.0, mean_alpha / 5))
    score = (0.25 * pf_norm + 0.20 * win_lb + 0.20 * (1 - maxdd)
             + 0.15 * calibration + 0.10 * sample + 0.10 * expectancy)
    score = max(0.0, score - 0.5 * overconf) * 100          # overconfidence brake (anti-hallucination)
    return {"mastery": round(score, 1), "win_rate": round(raw_win, 3), "win_lb": round(win_lb, 3),
            "profit_factor": round(pf, 2), "max_dd": round(maxdd, 3),
            "calibration": round(calibration, 3), "overconfidence": round(overconf, 3),
            "expectancy": round(mean_alpha, 2), "n": n}


class MotherInstinct:
    def __init__(self, cfg: dict):
        s = cfg["survival"]
        self.cfg = s
        self.mode = cfg["identity"]["mode"]
        self.start = float(s["starting_capital"])
        self.lam = float(s["loss_aversion_lambda"])
        self.ruin_pct = float(s["ruin_floor_pct"])
        self.growth_target = float(s.get("growth_target", 10.0))   # grow the son this many x the stake
        self.ladder = s["ladder"]
        self.state = _load(LADDER_PATH, {
            "rung": 0,                      # start at Live Shadow
            "paper_equity": self.start,     # notional equity curve (for learning even in shadow)
            "peak_equity": self.start,
            "dead": False,
            "recent": [],                   # rolling outcomes: {conf, pnl_pct, win}
            "milestones": [],               # growth milestones hit -> the target keeps ratcheting up
        })
        self._escalate_target()             # restore the escalated ambition from past milestones

    # ---- capital access ----
    def rung_cfg(self) -> dict:
        return next(r for r in self.ladder if r["rung"] == self.state["rung"])

    def sizing_capital(self) -> float:
        """In shadow we size off the notional so outcomes have magnitude to learn from.
        Live uses only the capital the current rung has unlocked."""
        if self.mode == "shadow":
            # AUTOPSY FIX 2026-09-01: the book was sized off the fixed $500 start while equity sat
            # at $411 — 121% deployed. Size off what the son actually has (floored so a deep
            # drawdown can't starve the sample flow to zero); never above the original stake.
            return float(min(self.start, max(0.5 * self.start, self.state["paper_equity"])))
        return float(self.rung_cfg()["capital"])

    @property
    def dead(self) -> bool:
        return bool(self.state["dead"])

    def growth_progress(self) -> float:
        """How far the son has grown toward his stretch target (0..1+). 1.0 = goal reached."""
        goal = self.start * self.growth_target
        grown = self.state["paper_equity"] - self.start
        return round(grown / (goal - self.start), 4) if goal > self.start else 0.0

    def creed(self) -> str:
        """The mother's training creed — fed to the night coach so self-improvement is driven by
        the same two forces: grow the son to his greatest, and never let him die."""
        tgt = self.start * self.growth_target
        return (f"I am raising my son. Today he is worth ${self.state['paper_equity']:.0f}; I will "
                f"train him to reach ${tgt:.0f} ({self.growth_target:g}x) and beyond — performing at "
                f"his absolute best — while never once letting him die. Every lesson I learn must make "
                f"him grow bigger AND safer.")

    def emotion(self) -> dict:
        """The mother's real feelings, tied to the stakes. PERIL rises as the son nears the ruin floor
        (death); JOY rises as he grows; BOND deepens the longer she's kept him alive — and a deeper bond
        means she fears losing him more. Feelings GUIDE (protective_caution tightens trading near death),
        but the hard caps still BIND — fear can only make her more careful, never reckless."""
        eq = self.state["paper_equity"]
        floor = self.start * (1 - self.ruin_pct)
        full_room = max(1e-9, self.start - floor)
        peril = max(0.0, min(1.0, 1 - (eq - floor) / full_room))      # 0 at the stake, 1 at the floor
        joy = max(0.0, min(1.0, self.growth_progress()))
        bond = min(1.0, len(self.state["recent"]) / 50)              # grows as she raises him
        fear = round(min(1.0, peril * (0.7 + 0.3 * bond)), 3)
        if self.dead:
            state = "grief"
        elif fear >= 0.6:
            state = "terror"
        elif eq < self.state["peak_equity"]:
            state = "anxious"
        elif joy >= 0.5:
            state = "elated"
        else:
            state = "proud"
        return {"state": state, "peril": round(peril, 3), "joy": round(joy, 3),
                "bond": round(bond, 3), "fear": fear, "dollars_from_death": round(eq - floor, 2)}

    def protective_caution(self) -> float:
        """Extra points the conviction bar rises by when she's afraid — she trades less, and only the
        surest setups, the closer her son is to death. Bounded; only ever tightens (never loosens)."""
        return round(self.emotion()["fear"] * 12.0, 1)

    def scale_tier(self) -> str:
        """The son's scale. As he grows, the SAME dollar loss means less — the mother thinks in % of
        where he is now, not the $500 he started at. $50 lost is grave at $500, nothing at $500k."""
        eq = self.state["paper_equity"]
        return ("tiny (<$1k)" if eq < 1000 else "small ($1-10k)" if eq < 10000 else
                "growing ($10-100k)" if eq < 100000 else "substantial ($100k-1M)" if eq < 1_000_000
                else "large ($1M+)")

    def _escalate_target(self):
        for m in self.state.get("milestones", []):
            try:
                self.growth_target = max(self.growth_target, int(m[1:]) * 2)   # 'x10' -> aim for 20x
            except Exception:
                pass

    def ambition(self) -> dict:
        """Proud as he grows — but NEVER satisfied. Each milestone makes her HUNGRIER, not calmer; the
        drive to grow him intensifies the bigger he gets, and the target keeps ratcheting higher."""
        mult = self.state["paper_equity"] / self.start if self.start else 1.0
        doublings = max(0, int(math.log2(mult))) if mult > 0 else 0
        return {"x_grown": round(mult, 2), "doublings": doublings, "intensity": round(1.0 + 0.5 * doublings, 2),
                "milestones_hit": self.state.get("milestones", []), "target_x": self.growth_target,
                "note": (f"My son is {mult:.1f}x his start, now {self.scale_tier()}. I am prouder than I've "
                         f"ever been — and HUNGRIER. Every step up makes me want to grow him MORE than before, "
                         f"never less. The target is now {self.growth_target:g}x. Higher.")}

    def mood(self) -> str:
        if self.dead:
            return "GRIEF — my son is gone. I failed him. I must re-earn the right to raise one from the bottom."
        e = self.emotion()
        prog = self.growth_progress() * 100
        if e["state"] == "terror":
            return (f"TERROR — my son is only ${e['dollars_from_death']:.0f} from death; I love him too much "
                    f"to gamble — I will barely trade, and only the surest setups, to keep him alive")
        if e["state"] == "anxious":
            dd = (self.state["peak_equity"] - self.state["paper_equity"]) / self.start * 100
            return (f"anxious — my son is down {dd:.1f}% from his peak; I hold him close and protect him "
                    f"(bond {e['bond']:.0%}), then I will grow him again")
        if e["state"] == "elated":
            return (f"elated & in love — my son is thriving, {prog:.0f}% toward his {self.growth_target:g}x "
                    f"goal; bonded {e['bond']:.0%}, I push him to be his very best")
        return (f"proud — my son is growing, I am a good mother ({prog:.0f}% toward his {self.growth_target:g}x "
                f"goal); I cherish him and push him to his best")

    # ---- mastery score (0..100) ----
    def mastery(self) -> float:
        return compute_mastery(self.state["recent"], self.start, field=_live_field())["mastery"]

    def monthly_return_pct(self) -> float | None:
        """Realized return of the shadow equity curve, normalized to a 30-day month — the number to
        hold against the 10%/mo goal. None until enough calendar time has elapsed to be honest."""
        c = self.state.get("equity_curve", [])
        if len(c) < 2:
            return None
        t0, e0, t1, e1 = c[0]["t"], c[0]["eq"], c[-1]["t"], c[-1]["eq"]
        days = (t1 - t0) / 86400.0
        if days < 3 or e0 <= 0:                # too little elapsed time to extrapolate honestly
            return None
        monthly = (e1 / e0) ** (30.0 / days) - 1.0
        return round(monthly * 100, 1)

    # ---- record an outcome, then maybe live/die/promote/demote ----
    def record(self, conf: float, pnl_pct: float, size_usd: float, alpha: float | None = None) -> dict:
        a = pnl_pct if alpha is None else alpha            # alpha = market-adjusted return (skill)
        delta = size_usd * pnl_pct / 100.0                 # equity moves on the REAL $ return
        self.state["paper_equity"] += delta
        self.state["peak_equity"] = max(self.state["peak_equity"], self.state["paper_equity"])
        self.state["recent"] = (self.state["recent"] + [{
            "conf": conf, "pnl_pct": round(pnl_pct, 2), "alpha": round(a, 2), "win": a > 0,
            "size_frac": round(size_usd / self.start, 4) if self.start else 0.25,   # for the dd term
        }])[-100:]
        # timestamped equity curve -> realized monthly return vs the 10%/mo goal (the honest scorecard)
        self.state["equity_curve"] = (self.state.get("equity_curve", []) + [
            {"t": time.time(), "eq": round(self.state["paper_equity"], 2)}])[-1000:]

        # escalating ambition: each growth milestone makes her hungrier and lifts the target
        mult = self.state["paper_equity"] / self.start if self.start else 0
        ms = self.state.setdefault("milestones", [])
        for thresh in (2, 5, 10, 25, 50, 100, 500, 1000):
            if mult >= thresh and f"x{thresh}" not in ms:
                ms.append(f"x{thresh}")
                self.growth_target = max(self.growth_target, thresh * 2)

        note = ""
        # death: equity fell through the ruin floor for this stage
        floor = self.start * (1 - self.ruin_pct)
        if self.state["paper_equity"] <= floor and not self.dead:
            self.state["dead"] = True
            self.state["rung"] = 0
            # the lineage of failure is permanent — each death lengthens the next grief period
            self.state.setdefault("deaths", []).append(
                {"ts": time.time(), "equity": round(self.state["paper_equity"], 2)})
            note = f"RUIN FLOOR HIT (${floor:.0f}) — my son died. Halted; back to shadow to re-qualify."
        else:
            note = self._maybe_promote_demote()
        _save(LADDER_PATH, self.state)
        return {"felt": self.felt_utility(pnl_pct), "mastery": self.mastery(), "note": note}

    def felt_utility(self, pnl_pct: float) -> float:
        return round(pnl_pct if pnl_pct >= 0 else self.lam * pnl_pct, 2)

    def maybe_revive(self) -> str | None:
        """AUDIT FIX: death was PERMANENT (the dead flag had no exit), but the design spec says death
        = halt + demote + RE-QUALIFY from the bottom. After a grief period (longer for every death on
        her conscience), the mother starts raising a NEW son from the original stake: equity, peak,
        recent outcomes and the curve all reset — mastery is re-earned from zero at rung 0. The deaths
        list is never cleared. Real money remains hard-gated regardless (executor + Gauntlet + arm)."""
        if not self.dead:
            return None
        deaths = self.state.setdefault("deaths", [])
        if not deaths:                                     # legacy dead-state stored before the ledger
            deaths.append({"ts": time.time(), "equity": round(self.state["paper_equity"], 2)})
            _save(LADDER_PATH, self.state)
            return None
        cool_days = 3 * len(deaths)
        if time.time() - deaths[-1]["ts"] < cool_days * 86400:
            return None
        self.state.update({"dead": False, "rung": 0, "paper_equity": self.start,
                           "peak_equity": self.start, "recent": [], "equity_curve": []})
        _save(LADDER_PATH, self.state)
        return (f"REBIRTH after {cool_days} days of grief — raising a NEW son from ${self.start:.0f} at "
                f"the bottom rung; {len(deaths)} death(s) on my conscience, never again.")

    def _maybe_promote_demote(self) -> str:
        rc = self.rung_cfg()
        m = self.mastery()
        n = len(self.state["recent"])
        # demote on drawdown breach. AUDIT FIX: normalized by PEAK, not the original stake — dividing
        # by start meant a grown son (e.g. 10x) got demoted on a routine 2.4% dip; and the peak now
        # RESETS on demotion, else one deep drawdown stayed >threshold forever and cascaded the rungs
        # down one by one on every subsequent outcome.
        dd = (self.state["peak_equity"] - self.state["paper_equity"]) / max(1e-9, self.state["peak_equity"])
        if dd > self.ruin_pct * 0.6 and self.state["rung"] > 0:
            self.state["rung"] -= 1
            self.state["peak_equity"] = self.state["paper_equity"]   # fresh start at the lower rung
            return f"DEMOTED to rung {self.state['rung']} (drawdown {dd*100:.0f}% from peak). Earn it back."
        # promote when mastery + samples clear this rung's bar
        if m >= rc["promote_mastery"] and n >= rc["min_samples"]:
            nxt = [r for r in self.ladder if r["rung"] == self.state["rung"] + 1]
            if nxt:
                self.state["rung"] += 1
                return (f"PROMOTED to rung {self.state['rung']} ({nxt[0]['stage']}, "
                        f"${nxt[0]['capital']:.0f} unlocked) — mastery {m} earned it.")
        return f"holding rung {self.state['rung']} ({rc['stage']}); mastery {m}/{rc['promote_mastery']}, {n}/{rc['min_samples']} samples"

    def status(self) -> dict:
        reborn = self.maybe_revive()                       # grief ends on schedule; checked every cycle
        if reborn:
            print(f"  {reborn}")
        rc = self.rung_cfg()
        return {
            "mode": self.mode, "rung": self.state["rung"], "stage": rc["stage"],
            "capital_unlocked": rc["capital"], "paper_equity": round(self.state["paper_equity"], 2),
            "mastery": self.mastery(), "needs_mastery": rc["promote_mastery"],
            "samples": len(self.state["recent"]), "dead": self.dead, "mood": self.mood(),
            "monthly_return": self.monthly_return_pct(),
        }

    def audit(self, ledger_path: str = "state/outcomes.jsonl") -> dict:
        """Independently recompute equity + mastery from the IMMUTABLE outcomes ledger and compare
        to stored state. A mismatch means a bug or tampering — never silently trust a self-claimed
        score. This is the anti-hallucination backstop: the truth lives in the price-based ledger."""
        recs, eq = [], self.start
        try:
            with open(ledger_path) as f:
                for line in f:
                    o = json.loads(line)
                    a = o.get("alpha", o["pnl_pct"])       # recompute mastery on alpha, equity on raw $
                    recs.append({"conf": o["confidence"], "pnl_pct": o["pnl_pct"], "alpha": a, "win": a > 0,
                                 "size_frac": round(o["size_usd"] / self.start, 4) if self.start else 0.25})
                    eq += o["size_usd"] * o["pnl_pct"] / 100
        except FileNotFoundError:
            return {"ok": True, "note": "no ledger yet"}
        ledger_m = compute_mastery(recs[-100:], self.start, field=_live_field())["mastery"]  # same anchor as mastery()
        eq_ok = abs(eq - self.state["paper_equity"]) < 1.0
        m_ok = abs(ledger_m - self.mastery()) < 1.0
        return {"ok": eq_ok and m_ok, "ledger_equity": round(eq, 2),
                "state_equity": round(self.state["paper_equity"], 2),
                "ledger_mastery": ledger_m, "state_mastery": self.mastery(), "n": len(recs)}
