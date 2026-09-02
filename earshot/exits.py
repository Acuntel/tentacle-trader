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
from datetime import datetime, timezone

from . import market, technicals, options


def _et_date(ts: float):
    """ET calendar date of an epoch timestamp — daily candles roll over on ET trading dates."""
    return market.now_et(datetime.fromtimestamp(ts, timezone.utc)).date()


class ExitEngine:
    def __init__(self, cfg: dict):
        e = cfg.get("exits", {})
        self.trail_pct = float(e.get("trail_pct", 5.0))        # give back this much from peak -> sell
        self.trail_arm_pct = float(e.get("trail_arm_pct", 4.0))  # only trail after this much gain
        self.tech = cfg.get("technicals", {})
        self.tech_on = bool(self.tech.get("enabled", True))
        self.opt = cfg.get("options", {})
        self.opt_on = bool(self.opt.get("enabled", True))
        # DRIFT DISCIPLINE (ledger-proven 2026-07-25): the reactive exits were chopping the very
        # drift the entries hunt — 32/70 conviction closes were `technical` at −0.04% avg and only
        # 2/70 ever reached target, while explore trades (stop/target/time only) hit target on
        # 16/45 fda_approval setups at +6.2%. A drift catalyst needs DAYS; a daily reversal candle
        # mid-hold is noise, not thesis-break. With this on, CLASSIFIED catalysts exit only on
        # stop/target/trailing/time; `unknown` (the discovery pool) keeps every protective exit.
        self.drift = bool(cfg.get("exits", {}).get("drift_discipline", True))

    def evaluate(self, pos: dict, price: float, bearish_tickers: set | None = None,
                 now: float | None = None, extended: bool = False) -> tuple[str | None, float]:
        now = now or time.time()
        entry = pos.get("entry_price") or price
        pos["peak"] = max(pos.get("peak", entry), price)            # ratchet the high-water mark
        pos["trough"] = min(pos.get("trough", entry), price)        # ...and the low-water mark (MAE)
        chg = (price / entry - 1) * 100
        peak_gain = (pos["peak"] / entry - 1) * 100
        drop_from_peak = (price / pos["peak"] - 1) * 100
        held_days = (now - pos.get("entry_ts", now)) / 86400

        if chg <= -pos.get("stop_pct", 8):
            # AUTOPSY FIX 2026-09-01: on the thin extended tape a single bad 1-minute print fired
            # the stop (LLY, $450, one after-hours print). An extended-hours stop needs the price
            # to sit through the stop on two consecutive reads; regular-session stops are immediate.
            if extended:
                pos["ext_stop_hits"] = int(pos.get("ext_stop_hits", 0)) + 1
                if pos["ext_stop_hits"] < 2:
                    return None, chg
            return "stop", chg
        pos["ext_stop_hits"] = 0
        if chg >= pos.get("target_pct", 12):
            return "target", chg
        # PRE-EVENT (pdufa_runup, 2026-08-05): a hard exit-by date stamped at entry (pre_event_days
        # before the FDA decision). Fires regardless of P&L — the strategy harvests the run-up and
        # NEVER holds the binary event; past this date every remaining day is pure coin-flip risk.
        if pos.get("exit_by_ts") and now >= float(pos["exit_by_ts"]):
            return "pre_event", chg
        if peak_gain >= self.trail_arm_pct and drop_from_peak <= -self.trail_pct:
            return "trailing", chg
        # Reactive exits (technicals / options tape / signal decay) fire only where they're still
        # protective: unknown-catalyst positions, or everything when drift_discipline is off.
        protective = (not self.drift) or (pos.get("catalyst") or "unknown") == "unknown"
        # PRICE ACTION: a fresh bearish reversal candle (overbought / trend-break) -> the move is done.
        # ARMED only after a full daily bar has COMPLETED since entry: technicals read completed
        # candles, so on entry day the "fresh" bearish pattern is YESTERDAY'S static bar — the entry
        # gate already priced it, and firing on it churned buy->exit within minutes (VRTX 8x in 1h).
        if protective and self.tech_on and _et_date(now) > _et_date(pos.get("entry_ts", now)):
            should, why = technicals.exit_read(pos["ticker"], self.tech)
            if should:
                return why, chg
        # OPTIONS TAPE: a strong bearish positioning shift on a name we hold long -> get out.
        if protective and self.opt_on:
            should, why = options.exit_read(pos["ticker"], self.opt)
            if should:
                return why, chg
        if protective and bearish_tickers and pos["ticker"] in bearish_tickers:
            return "decay", chg
        if held_days >= pos.get("hold_days", 5):
            return "time", chg
        return None, chg
