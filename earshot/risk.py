"""Hard risk law — enforced OUTSIDE the model. The bot cannot hope or reason its way past these.
Emotion (survival.py) guides choices; this binds them."""
from __future__ import annotations

from .models import Candidate
from . import prices, market

SOCIAL_SOURCES = {"apewisdom", "x_relay", "reddit", "discord", "telegram", "stocktwits"}


class RiskGate:
    def __init__(self, cfg: dict):
        self.r = cfg["risk"]
        self.market = cfg.get("market", {})
        self.armed = False             # never armed in this build; flipping requires a live rung + human
        self.exec_count = 0

    def check(self, c: Candidate, instinct, open_positions, today_pnl: float) -> tuple[bool, str]:
        # HARD LAW: a closed market cannot be traded. No entry off-hours, ever — the price isn't real.
        ok, why, _ = market.can_open(self.market)
        if not ok:
            return False, why
        if instinct.dead:
            return False, "bot is DEAD (ruin floor hit) — re-qualification required"
        if today_pnl <= -abs(self.r["daily_max_loss_usd"]):
            return False, f"daily max-loss kill switch tripped ({today_pnl:.2f})"
        if c.ticker in {p["ticker"] for p in open_positions}:
            return False, f"already holding {c.ticker} — no doubling up on one name"
        if len(open_positions) >= self.r["max_open_positions"]:
            return False, f"max open positions ({self.r['max_open_positions']}) reached"
        if self.exec_count >= self.r["repeated_exec_limit"]:
            return False, "repeated-execution limit -> auto-disarmed until re-armed"

        # pump / penny / liquidity filters (graceful if price data is unavailable)
        px = prices.last_price(c.ticker)
        if px is not None and px < self.r["min_price"]:
            return False, f"price ${px:.2f} below min ${self.r['min_price']} (penny/pump filter)"
        dv = prices.dollar_volume(c.ticker)
        if dv is not None and dv < self.r["min_dollar_volume"]:
            return False, f"avg $ volume ${dv:,.0f} below liquidity floor"

        # a microcap moved ONLY by social chatter = classic pump -> block
        if self.r.get("block_microcap_social_only"):
            srcs = {s.source for s in c.signals}
            if srcs and srcs.issubset(SOCIAL_SOURCES):
                return False, "social-only signal with no primary catalyst (pump risk)"

        return True, "cleared hard risk law"

    def on_execute(self):
        self.exec_count += 1
