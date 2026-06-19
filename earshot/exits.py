"""Exit engine — the bot now KNOWS when to sell. Checked on EVERY cycle against the live price,
not just at a fixed horizon. Five ways out, in priority order:

  1. STOP      — price fell to the per-catalyst stop -> cut the loss, protect the child.
  2. TARGET    — price hit the profit target -> take the win.
  3. TRAILING  — after a real gain, price gave back `trail_pct` from its peak -> lock it in.
  4. TIME      — held past the catalyst's drift window -> the edge is spent, move on.
  5. DECAY     — a fresh BEARISH signal on the name showed up -> the thesis broke, get out.
"""
from __future__ import annotations

import time


class ExitEngine:
    def __init__(self, cfg: dict):
        e = cfg.get("exits", {})
        self.trail_pct = float(e.get("trail_pct", 5.0))        # give back this much from peak -> sell
        self.trail_arm_pct = float(e.get("trail_arm_pct", 4.0))  # only trail after this much gain

    def evaluate(self, pos: dict, price: float, bearish_tickers: set | None = None,
                 now: float | None = None) -> tuple[str | None, float]:
        now = now or time.time()
        entry = pos.get("entry_price") or price
        pos["peak"] = max(pos.get("peak", entry), price)            # ratchet the high-water mark
        chg = (price / entry - 1) * 100
        peak_gain = (pos["peak"] / entry - 1) * 100
        drop_from_peak = (price / pos["peak"] - 1) * 100
        held_days = (now - pos.get("entry_ts", now)) / 86400

        if chg <= -pos.get("stop_pct", 8):
            return "stop", chg
        if chg >= pos.get("target_pct", 12):
            return "target", chg
        if peak_gain >= self.trail_arm_pct and drop_from_peak <= -self.trail_pct:
            return "trailing", chg
        if bearish_tickers and pos["ticker"] in bearish_tickers:
            return "decay", chg
        if held_days >= pos.get("hold_days", 5):
            return "time", chg
        return None, chg
