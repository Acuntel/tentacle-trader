"""Pattern recognition — the brain that learns WHICH setups actually win, and how to profit.

Beyond "N sources agree," TENTACLE fingerprints every setup into a PATTERN signature
(catalyst + which signal families aligned + the macro regime) and learns each pattern's real
win-rate and average winning move from resolved outcomes. Then it:
  • AMPLIFIES proven patterns (confidence multiplier up),
  • VETOES proven losers (a pattern that keeps losing is blocked, even with 2 sources),
  • sets a DATA-DRIVEN profit target per pattern (how much that pattern usually pays).
This is how it self-improves and analyzes where it went wrong — per pattern, not just per trade.
"""
from __future__ import annotations

import math

# ---- THE FIELD RATE: what fraction of ALL labeled setups beat the market (alpha-win base rate). ----
# Since v3.4 every label is ALPHA (beat SPY), so the base win-rate is ~0.34, NOT 0.5 — most stocks lose
# to the index. Every consumer that judged "good vs bad" against an absolute 0.5 anchor (source weights,
# pattern multipliers, loser vetoes, calibration) was therefore dragging EVERYTHING toward the floor and
# throwing away the real signal: RELATIVE skill vs the field. These helpers give every learner the one
# shared, honest anchor. Persisted in knowledge.json (survives redeploys, like everything learned).
BASE_KEY = "base_rate"


def update_field_rate(know: dict, win: bool, ema: float = 0.02):
    """One labeled outcome -> nudge the rolling field rate (plain average first 50, then a slow EMA)."""
    b = know.setdefault(BASE_KEY, {"n": 0, "ema": 0.5})
    y = 1.0 if win else 0.0
    b["n"] = int(b.get("n", 0)) + 1
    prev = float(b.get("ema", 0.5))
    if b["n"] <= 50:
        b["ema"] = round(prev + (y - prev) / b["n"], 4)
    else:
        b["ema"] = round((1 - ema) * prev + ema * y, 4)


def field_rate(know: dict, default: float = 0.5) -> float:
    """The current base rate of alpha-wins across everything we label. Until the rolling tracker has
    enough of its own samples, derive it from the (much larger) per-source ledger so a redeploy onto an
    old volume is instantly anchored to reality instead of the 0.5 fantasy."""
    b = know.get(BASE_KEY) or {}
    if int(b.get("n", 0)) >= 30:
        return float(b.get("ema", default))
    tot = wins = 0
    for v in (know.get("sources") or {}).values():
        tot += int(v.get("trades", 0))
        wins += int(v.get("wins", 0))
    return round(wins / tot, 4) if tot >= 200 else default


def _wilson_lower(wins: int, n: int, z: float = 1.64) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (c - m) / d)


def _wilson_upper(wins: int, n: int, z: float = 1.64) -> float:
    if n == 0:
        return 1.0
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return min(1.0, (c + m) / d)


class PatternMemory:
    def __init__(self, know: dict):
        self.know = know
        self.p = know.setdefault("patterns", {})        # exact signature -> stats (persisted)
        self.agg = know.setdefault("patterns_agg", {})  # HIERARCHICAL backoff levels (catalyst / +families)
        # FORWARD-ONLY graduation ledger (EDGE doctrine): catalyst -> stats counted from doctrine
        # ship-time (2026-07-17) onward. The lifetime agg mixes label eras (pre-de-pollution, pre-
        # base-rate-anchor) against TODAY's field EMA — judging graduation on it marked every thesis
        # catalyst dead on day one and would have cut their explore budget. Only outcomes the CURRENT
        # configuration produced count as evidence for or against it.
        self.grad = know.setdefault("patterns_grad", {})

    @staticmethod
    def signature(catalyst: str, families, macro: str = "") -> str:
        sig = f"{catalyst}|{'+'.join(sorted(families))}"
        return f"{sig}|{macro}" if macro else sig

    @staticmethod
    def _levels(sig: str) -> list[str]:
        """Coarser views of a signature: catalyst|families, then catalyst alone. The full sig includes
        the multi-part macro regime, which fragments the space (a one-bit FRED flip = a fresh bucket that
        restarts at n=0). Backoff lets a rare/new exact sig borrow its family's track record — n-gram
        style — instead of flying blind. Sim/study signatures keep their own namespace (no backoff)."""
        if sig.startswith(("sim_", "study_")):
            return []
        parts = sig.split("|")
        out = []
        if len(parts) > 2:
            out.append("|".join(parts[:2]))             # catalyst|families (macro stripped)
        if len(parts) > 1:
            out.append(parts[0])                        # catalyst alone
        return out

    def field(self) -> float:
        return field_rate(self.know)

    def _stats(self, sig: str) -> dict:
        return self.p.get(sig, {"trades": 0, "wins": 0, "sum_ret": 0.0, "sum_win_ret": 0.0})

    def _backoff(self, sig: str, min_n: int) -> dict:
        """The FINEST level with enough data: exact sig, else catalyst|families, else catalyst."""
        st = self._stats(sig)
        if st["trades"] >= min_n:
            return st
        for lvl in self._levels(sig):
            a = self.agg.get(lvl)
            if a and a["trades"] >= min_n:
                return a
        return st

    def multiplier(self, sig: str) -> float:
        """Confidence multiplier from the pattern's track record — judged RELATIVE to the field base
        rate, not an absolute 0.5 (in an alpha-labeled world ~0.34 of everything wins, so 0.5-anchoring
        haircut EVERY pattern). Empirical-Bayes: the win-rate is shrunk toward the field prior by 10
        pseudo-counts, so an at-field pattern is exactly neutral at any sample size (a Wilson lower
        bound vs the field MEAN would punish every small-n pattern for being small-n, which is the same
        floor-drag bug in a new coat). Backs off to coarser levels when the exact sig is data-poor."""
        st = self._backoff(sig, 5)
        if st["trades"] < 5:
            return 1.0
        fld = self.field()
        post = (st["wins"] + 10 * fld) / (st["trades"] + 10)   # shrunk toward the field prior
        return max(0.5, min(1.5, 1.0 + (post - fld) * 2.5))

    def is_loser(self, sig: str) -> bool:
        """Veto a pattern that is conservatively WORSE THAN THE FIELD by a clear margin — not one that
        merely wins less than a coin-flip (almost everything does, on alpha labels). Wilson keeps the
        veto conservative; the 0.15 margin leaves room for the bound's small-n pessimism."""
        st = self._backoff(sig, 12)
        return st["trades"] >= 12 and _wilson_lower(st["wins"], st["trades"]) < self.field() - 0.15

    def is_decayed(self, sig: str) -> bool:
        """CONCEPT DRIFT: a once-good pattern whose RECENT window is much worse than its lifetime —
        a regime shift killed it. Veto it until it proves itself again, instead of trusting stale stats."""
        st = self._stats(sig)
        rec = st.get("recent", [])
        if len(rec) < 10 or st["trades"] < 12:
            return False
        recent_wr = sum(rec) / len(rec)
        life_wr = st["wins"] / st["trades"]
        return recent_wr < life_wr - 0.25 and recent_wr < max(0.2, self.field() - 0.05)

    def refresh_decay(self) -> list[str]:
        """All patterns currently in decay — computed in the brain core's simultaneous synthesis."""
        return [s for s in self.p if self.is_decayed(s)]

    def suggested_target(self, sig: str, default: float) -> float:
        """How much this pattern usually pays when it wins -> the profit target to aim for."""
        st = self._stats(sig)
        if st["wins"] < 5:
            st = self._backoff(sig, 12)                 # borrow the family's payoff profile if data-poor
        if st["wins"] >= 5:
            return round(max(4.0, min(25.0, st["sum_win_ret"] / st["wins"])), 1)
        return default

    def record(self, sig: str, win: bool, pnl_pct: float):
        if not sig:
            return
        for key, table in [(sig, self.p)] + [(lvl, self.agg) for lvl in self._levels(sig)]:
            st = table.setdefault(key, {"trades": 0, "wins": 0, "sum_ret": 0.0, "sum_win_ret": 0.0})
            st["trades"] += 1
            st["sum_ret"] += pnl_pct
            if win:
                st["wins"] += 1
                st["sum_win_ret"] += pnl_pct
            st["recent"] = (st.get("recent", [])[-29:] + [1 if win else 0])   # rolling window for decay
        # forward graduation ledger: catalyst-level, current-configuration outcomes only
        if not sig.startswith(("sim_", "study_")):
            g = self.grad.setdefault(sig.split("|")[0], {"trades": 0, "wins": 0})
            g["trades"] += 1
            if win:
                g["wins"] += 1

    # ---- Catalyst graduation (EDGE_MASTERPROMPT): a catalyst's forward ledger must PROVE alpha
    # before live conviction entries flow to it. Judged on the catalyst-level agg (resolved
    # out-of-sample outcomes only) against the live field rate — one graduated catalyst = one
    # real, provable edge. 'unknown' is the discovery pool: never graduates, never killed.
    GRAD_MIN_N = 30
    DEAD_MIN_N = 50

    def _cat_stats(self, catalyst: str) -> dict | None:
        st = self.grad.get(catalyst)
        return st if st and st.get("trades") else None

    def catalyst_graduated(self, catalyst: str) -> bool:
        if not catalyst or catalyst == "unknown":
            return False
        st = self._cat_stats(catalyst)
        return bool(st and st["trades"] >= self.GRAD_MIN_N
                    and _wilson_lower(st["wins"], st["trades"]) > self.field())

    def catalyst_dead(self, catalyst: str) -> bool:
        """Conservatively worse than the field at real n — stop pouring explore budget into it."""
        if not catalyst or catalyst == "unknown":
            return False
        st = self._cat_stats(catalyst)
        return bool(st and st["trades"] >= self.DEAD_MIN_N
                    and _wilson_upper(st["wins"], st["trades"]) < self.field() - 0.05)

    def catalyst_explore_slots(self, catalyst: str, base: int, unknown_slots: int = 1) -> int:
        """Per-cycle explore budget for a catalyst — keep buying samples of what wins, starve what
        loses (ledger-driven, 2026-07-25). Graduated catalysts earn extra sampling; a catalyst
        LAGGING the field at real n keeps a heartbeat (it may come back) but stops flooding; dead
        keeps zero (learning_picks already blocks it). `unknown` is the discovery pool and gets
        only the configured trickle — at 640/1210 explore trades and −11.4%/trade it was the
        single biggest drag on the pooled ledger, drowning the real per-catalyst edges."""
        if not catalyst or catalyst == "unknown":
            return unknown_slots
        if self.catalyst_dead(catalyst):
            return 0
        if self.catalyst_graduated(catalyst):
            return base + 2
        st = self._cat_stats(catalyst)
        if st and st["trades"] >= self.DEAD_MIN_N and (st["wins"] / st["trades"]) < self.field():
            return max(1, base // 4)
        return base

    def graduation_report(self) -> dict:
        out = {"graduated": [], "pending": [], "dead": []}
        for cat, st in self.grad.items():
            if cat == "unknown" or not st.get("trades"):
                continue
            row = f"{cat} {st['wins']}/{st['trades']}"
            if self.catalyst_graduated(cat):
                out["graduated"].append(row)
            elif self.catalyst_dead(cat):
                out["dead"].append(row)
            else:
                out["pending"].append(row)
        return out

    def report(self, k: int = 5) -> dict:
        rows = [{"sig": s, "n": v["trades"], "win": round(v["wins"] / v["trades"], 2),
                 "avg": round(v["sum_ret"] / v["trades"], 2)}
                for s, v in self.p.items() if v["trades"] >= 3]
        rows.sort(key=lambda r: -r["win"])
        return {"best": rows[:k], "worst": [r for r in rows if r["win"] < 0.4][-k:]}
