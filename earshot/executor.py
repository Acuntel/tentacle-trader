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


def _gauntlet_passed() -> tuple[bool, str]:
    """Has the Gauntlet CERTIFIED the code that is running now?

    Hardened 2026-09-02. The old check read one number out of state/gauntlet.json and nothing else.
    That file had been a stale artifact since 2026-06-18 — written once, by hand, by a backtest of
    a strategy this bot does not run — so the money gate's first condition was being satisfied by
    a ten-week-old measurement of something else. Three conditions now, all required:

      * the file exists and is v3+ (a forward-record verdict, not a legacy backtest),
      * it says every one of its own checks passed,
      * it was computed recently enough to describe today's code.

    Strictly harder than before: a legacy file, an old file, or a partial pass all read as FAIL.
    """
    try:
        with open("state/gauntlet.json") as f:
            g = json.load(f)
    except Exception:
        return False, "no Gauntlet verdict on disk — real money locked"
    if not str(g.get("version", "")).startswith("gauntlet-v3"):
        return False, ("Gauntlet verdict predates the forward-record rewrite (2026-09-02) — "
                       "re-run `python -m earshot.backtest`")
    if not g.get("passed") or float(g.get("overall", {}).get("mastery", 0)) < GAUNTLET_PASS_MASTERY:
        failed = ", ".join(g.get("failed") or []) or "mastery below the bar"
        return False, f"Gauntlet not passed ({failed}) — real money locked"
    age_d = (time.time() - float(g.get("ts", 0))) / 86400.0
    stale = float(g.get("stale_after_days", 7.0))
    if age_d > stale:
        return False, (f"Gauntlet verdict is {age_d:.1f} days old (> {stale:.0f}) — a stale pass is "
                       "not a pass; re-run it against the current ledger")
    return True, "Gauntlet passed on the current forward record"


def live_allowed(rung: int, armed: bool) -> tuple[bool, str]:
    ok, why = _gauntlet_passed()
    if not ok:
        return False, why
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
