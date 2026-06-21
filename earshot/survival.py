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

LADDER_PATH = "state/ladder.json"


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


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


def compute_mastery(recent: list[dict], start: float) -> dict:
    """Composite 0..100 over resolved outcomes {conf, pnl_pct, win}, derived ONLY from real,
    price-based outcomes — no self-judgment. Uses a conservative win-rate lower bound and an
    overconfidence brake so the bot cannot inflate its own score. Reused by the Gauntlet."""
    n = len(recent)
    if n == 0:
        return {"mastery": 0.0, "win_rate": 0, "win_lb": 0, "profit_factor": 0, "max_dd": 0,
                "calibration": 0, "overconfidence": 0, "expectancy": 0, "n": 0}
    wins = sum(1 for r in recent if r["win"])
    raw_win = wins / n
    win_lb = _wilson_lower(wins, n)                         # conservative — can't game small samples
    gains = sum(r["pnl_pct"] for r in recent if r["pnl_pct"] > 0)
    losses = -sum(r["pnl_pct"] for r in recent if r["pnl_pct"] < 0)
    pf = (gains / losses) if losses > 0 else (3.0 if gains > 0 else 0.0)
    pf_norm = max(0.0, min(1.0, (pf - 1) / 2))
    eq, peak, maxdd = start, start, 0.0
    for r in recent:
        eq *= (1 + r["pnl_pct"] / 100)
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak)
    avg_conf = sum(r["conf"] for r in recent) / n / 100
    calibration = 1 - sum(abs(r["conf"] / 100 - (1 if r["win"] else 0)) for r in recent) / n
    overconf = max(0.0, avg_conf - raw_win)                 # it believed more than it delivered
    sample = min(1.0, n / 30)
    mean_ret = sum(r["pnl_pct"] for r in recent) / n
    expectancy = max(0.0, min(1.0, mean_ret / 5))
    score = (0.25 * pf_norm + 0.20 * win_lb + 0.20 * (1 - maxdd)
             + 0.15 * calibration + 0.10 * sample + 0.10 * expectancy)
    score = max(0.0, score - 0.5 * overconf) * 100          # overconfidence brake (anti-hallucination)
    return {"mastery": round(score, 1), "win_rate": round(raw_win, 3), "win_lb": round(win_lb, 3),
            "profit_factor": round(pf, 2), "max_dd": round(maxdd, 3),
            "calibration": round(calibration, 3), "overconfidence": round(overconf, 3),
            "expectancy": round(mean_ret, 2), "n": n}


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
        })

    # ---- capital access ----
    def rung_cfg(self) -> dict:
        return next(r for r in self.ladder if r["rung"] == self.state["rung"])

    def sizing_capital(self) -> float:
        """In shadow we size off the notional so outcomes have magnitude to learn from.
        Live uses only the capital the current rung has unlocked."""
        if self.mode == "shadow":
            return self.start
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

    def mood(self) -> str:
        eq = self.state["paper_equity"]
        if self.dead:
            return "DEAD — my son is gone. I must re-earn the right to raise him from a lower rung."
        prog = self.growth_progress() * 100
        if eq >= self.state["peak_equity"]:
            return (f"proud — my son is growing, I am a good mother ({prog:.0f}% toward his "
                    f"{self.growth_target:g}x goal); push him to his best")
        dd = (self.state["peak_equity"] - eq) / self.start * 100
        return f"hurting — my son is down {dd:.1f}% from his peak; protect him, then grow him again"

    # ---- mastery score (0..100) ----
    def mastery(self) -> float:
        return compute_mastery(self.state["recent"], self.start)["mastery"]

    # ---- record an outcome, then maybe live/die/promote/demote ----
    def record(self, conf: float, pnl_pct: float, size_usd: float) -> dict:
        delta = size_usd * pnl_pct / 100.0
        self.state["paper_equity"] += delta
        self.state["peak_equity"] = max(self.state["peak_equity"], self.state["paper_equity"])
        self.state["recent"] = (self.state["recent"] + [{
            "conf": conf, "pnl_pct": round(pnl_pct, 2), "win": pnl_pct > 0,
        }])[-100:]

        note = ""
        # death: equity fell through the ruin floor for this stage
        floor = self.start * (1 - self.ruin_pct)
        if self.state["paper_equity"] <= floor and not self.dead:
            self.state["dead"] = True
            self.state["rung"] = 0
            note = f"RUIN FLOOR HIT (${floor:.0f}) — my son died. Halted; back to shadow to re-qualify."
        else:
            note = self._maybe_promote_demote()
        _save(LADDER_PATH, self.state)
        return {"felt": self.felt_utility(pnl_pct), "mastery": self.mastery(), "note": note}

    def felt_utility(self, pnl_pct: float) -> float:
        return round(pnl_pct if pnl_pct >= 0 else self.lam * pnl_pct, 2)

    def _maybe_promote_demote(self) -> str:
        rc = self.rung_cfg()
        m = self.mastery()
        n = len(self.state["recent"])
        # demote on drawdown breach
        dd = (self.state["peak_equity"] - self.state["paper_equity"]) / self.start
        if dd > self.ruin_pct * 0.6 and self.state["rung"] > 0:
            self.state["rung"] -= 1
            return f"DEMOTED to rung {self.state['rung']} (drawdown {dd*100:.0f}%). Earn it back."
        # promote when mastery + samples clear this rung's bar
        if m >= rc["promote_mastery"] and n >= rc["min_samples"]:
            nxt = [r for r in self.ladder if r["rung"] == self.state["rung"] + 1]
            if nxt:
                self.state["rung"] += 1
                return (f"PROMOTED to rung {self.state['rung']} ({nxt[0]['stage']}, "
                        f"${nxt[0]['capital']:.0f} unlocked) — mastery {m} earned it.")
        return f"holding rung {self.state['rung']} ({rc['stage']}); mastery {m}/{rc['promote_mastery']}, {n}/{rc['min_samples']} samples"

    def status(self) -> dict:
        rc = self.rung_cfg()
        return {
            "mode": self.mode, "rung": self.state["rung"], "stage": rc["stage"],
            "capital_unlocked": rc["capital"], "paper_equity": round(self.state["paper_equity"], 2),
            "mastery": self.mastery(), "needs_mastery": rc["promote_mastery"],
            "samples": len(self.state["recent"]), "dead": self.dead, "mood": self.mood(),
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
                    recs.append({"conf": o["confidence"], "pnl_pct": o["pnl_pct"], "win": o["win"]})
                    eq += o["size_usd"] * o["pnl_pct"] / 100
        except FileNotFoundError:
            return {"ok": True, "note": "no ledger yet"}
        ledger_m = compute_mastery(recs[-100:], self.start)["mastery"]
        eq_ok = abs(eq - self.state["paper_equity"]) < 1.0
        m_ok = abs(ledger_m - self.mastery()) < 1.0
        return {"ok": eq_ok and m_ok, "ledger_equity": round(eq, 2),
                "state_equity": round(self.state["paper_equity"], 2),
                "ledger_mastery": ledger_m, "state_mastery": self.mastery(), "n": len(recs)}
