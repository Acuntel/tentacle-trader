"""Historical market-adjustment for the sims + the strategy tournament.

A trade's real edge is its return MINUS the market's over the SAME dates (alpha) — otherwise, in a
rising tape, random longs all "win" and the loop learns beta, not skill. This loads the cached daily
SPY history (ensuring it's downloaded once) and returns SPY's return between two calendar dates.
Degrades to None (callers then fall back to the raw return) if SPY isn't cached yet.
"""
from __future__ import annotations

import time

from . import history

_SPY = {"map": {}, "ts": 0.0, "ensured": False}


def _spy_map() -> dict:
    now = time.time()
    if _SPY["map"] and now - _SPY["ts"] < 3600:
        return _SPY["map"]
    bars = history.load("SPY")
    # AUDIT FIX: refresh on STALENESS too, not only once when the cache is missing — SPY.csv froze at
    # its first download (the 'ensured' latch) while every peer refreshed via the regular top-ups
    # (SPY isn't in the stock universe), so alpha labels for post-freeze dates silently degraded to
    # raw returns. ensure_slice's own _fresh check makes this a cheap no-op when SPY is current.
    stale = False
    try:
        import datetime as _dt
        stale = bool(bars) and (_dt.date.today() - _dt.date.fromisoformat(bars[-1][0])).days > 3
    except Exception:
        pass
    if (len(bars) < 30 or stale) and history.have_yf():
        try:
            history.ensure_slice(10, 1, tickers=["SPY"])       # cache/refresh the market proxy
        except Exception:
            pass
        _SPY["ensured"] = True
        bars = history.load("SPY")
    _SPY["map"] = {d: c for d, c in bars}
    _SPY["ts"] = now
    return _SPY["map"]


def market_return(date_start: str, date_end: str):
    """SPY % return between two ISO dates (same calendar span as a held trade). None if unknown."""
    m = _spy_map()
    a, b = m.get(date_start), m.get(date_end)
    if a and b and a > 0:
        return (b / a - 1) * 100
    return None
