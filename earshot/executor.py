"""Execution backends.

Shadow is wired and active. The LIVE backends are fully WRITTEN (ready to arm) but are
held behind a hard multi-condition gate: they refuse unless the bot has PASSED the Gauntlet,
reached a live rung, AND been explicitly armed. As of now the Gauntlet has NOT passed, so
no real order can be placed — by design, not by accident.
"""
from __future__ import annotations

import json
import os
import time

from .models import Candidate
from . import prices

AGENTIC_ACCOUNT = "469628218"   # the ••8218 cash account — the only agent-tradeable one
GAUNTLET_PASS_MASTERY = 80


def _gauntlet_passed() -> bool:
    try:
        return json.load(open("state/gauntlet.json"))["overall"]["mastery"] >= GAUNTLET_PASS_MASTERY
    except Exception:
        return False


def live_allowed(rung: int, armed: bool) -> tuple[bool, str]:
    if not _gauntlet_passed():
        return False, "Gauntlet not passed (mastery < 80) — real money locked"
    if rung < 1:
        return False, "still on a $0 rung (shadow) — no live capital unlocked"
    if not armed:
        return False, "not armed — a human must explicitly arm live trading"
    return True, "live trading permitted"


class Executor:
    name = "base"

    def buy(self, c: Candidate, **kw) -> dict:  # pragma: no cover
        raise NotImplementedError


class ShadowExecutor(Executor):
    """Paper fills at the live price (or a nominal price if data is unavailable). No money moves."""
    name = "shadow"

    def buy(self, c: Candidate, extended: bool = False, **kw) -> dict:
        if extended:
            # Extended-hours fill = a real, FRESH pre/post print or NO fill at all. A stale
            # regular-session price here would book an entry no live order could get (the whole
            # point of off-hours entry is the price BEFORE the crowd — faking it inflates mastery).
            px = prices.last_price(c.ticker, extended=True)
            return {"status": "shadow_fill_ext" if px else "no_fill", "ticker": c.ticker,
                    "price": px, "usd": c.size_usd, "ts": time.time()}
        px = prices.last_price(c.ticker) or 100.0
        return {"status": "shadow_fill", "ticker": c.ticker, "price": px,
                "usd": c.size_usd, "ts": time.time()}


class RobinhoodMCPExecutor(Executor):
    """Sanctioned path: orders flow through the Claude agent runtime, which holds the Robinhood
    MCP connection, into the agentic account ••8218. A standalone Python process cannot reach the
    MCP, so this raises here — it executes only when TENTACLE runs as a scheduled Claude agent."""
    name = "robinhood_mcp"

    def buy(self, c: Candidate, rung=0, armed=False, **kw) -> dict:
        ok, why = live_allowed(rung, armed)
        if not ok:
            raise RuntimeError(f"LIVE refused: {why}")
        raise RuntimeError(
            "RH MCP execution must be driven by the Claude agent runtime (place_equity_order "
            f"on account {AGENTIC_ACCOUNT}); not reachable from a standalone process.")


class RobinStocksExecutor(Executor):
    """Standalone 24/7 path (Railway). Real order code is written below but gated OFF until the
    bot earns it. Requires ROBINHOOD_USERNAME/PASSWORD in the environment."""
    name = "robin_stocks"

    def buy(self, c: Candidate, rung=0, armed=False, **kw) -> dict:
        ok, why = live_allowed(rung, armed)
        if not ok:
            raise RuntimeError(f"LIVE refused: {why}")
        import robin_stocks.robinhood as rh   # imported only on the live path
        rh.login(os.environ["ROBINHOOD_USERNAME"], os.environ["ROBINHOOD_PASSWORD"])
        # marketable-limit buy, fractional by dollar amount, into the cash account
        return rh.order_buy_fractional_by_price(
            c.ticker, c.size_usd, account_number=AGENTIC_ACCOUNT, timeInForce="gfd")


def get_executor(mode: str) -> Executor:
    # This build always returns Shadow. The live backends exist and are wired — they simply
    # refuse until the Gauntlet passes, a live rung is reached, and a human arms them.
    return ShadowExecutor()
