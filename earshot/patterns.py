"""Pattern recognition — the brain that learns WHICH setups actually win, and how to profit.

Beyond "N sources agree," EARSHOT fingerprints every setup into a PATTERN signature
(catalyst + which signal families aligned + the macro regime) and learns each pattern's real
win-rate and average winning move from resolved outcomes. Then it:
  • AMPLIFIES proven patterns (confidence multiplier up),
  • VETOES proven losers (a pattern that keeps losing is blocked, even with 2 sources),
  • sets a DATA-DRIVEN profit target per pattern (how much that pattern usually pays).
This is how it self-improves and analyzes where it went wrong — per pattern, not just per trade.
"""
from __future__ import annotations

import math


def _wilson_lower(wins: int, n: int, z: float = 1.64) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (c - m) / d)


class PatternMemory:
    def __init__(self, know: dict):
        self.p = know.setdefault("patterns", {})        # signature -> stats (persisted in knowledge.json)

    @staticmethod
    def signature(catalyst: str, families, macro: str = "") -> str:
        sig = f"{catalyst}|{'+'.join(sorted(families))}"
        return f"{sig}|{macro}" if macro else sig

    def _stats(self, sig: str) -> dict:
        return self.p.get(sig, {"trades": 0, "wins": 0, "sum_ret": 0.0, "sum_win_ret": 0.0})

    def multiplier(self, sig: str) -> float:
        """Confidence multiplier from this pattern's proven track record (neutral until 5+ trades)."""
        st = self._stats(sig)
        if st["trades"] < 5:
            return 1.0
        lb = _wilson_lower(st["wins"], st["trades"])    # conservative win-rate
        return max(0.5, min(1.5, 1.0 + (lb - 0.5) * 1.5))

    def is_loser(self, sig: str) -> bool:
        """A pattern with enough samples and a conservatively-bad win-rate -> veto it."""
        st = self._stats(sig)
        return st["trades"] >= 12 and _wilson_lower(st["wins"], st["trades"]) < 0.30

    def suggested_target(self, sig: str, default: float) -> float:
        """How much this pattern usually pays when it wins -> the profit target to aim for."""
        st = self._stats(sig)
        if st["wins"] >= 5:
            return round(max(4.0, min(25.0, st["sum_win_ret"] / st["wins"])), 1)
        return default

    def record(self, sig: str, win: bool, pnl_pct: float):
        if not sig:
            return
        st = self.p.setdefault(sig, {"trades": 0, "wins": 0, "sum_ret": 0.0, "sum_win_ret": 0.0})
        st["trades"] += 1
        st["sum_ret"] += pnl_pct
        if win:
            st["wins"] += 1
            st["sum_win_ret"] += pnl_pct

    def report(self, k: int = 5) -> dict:
        rows = [{"sig": s, "n": v["trades"], "win": round(v["wins"] / v["trades"], 2),
                 "avg": round(v["sum_ret"] / v["trades"], 2)}
                for s, v in self.p.items() if v["trades"] >= 3]
        rows.sort(key=lambda r: -r["win"])
        return {"best": rows[:k], "worst": [r for r in rows if r["win"] < 0.4][-k:]}
