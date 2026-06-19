"""Price data for earliness checks, regime filter, and shadow outcome scoring.

Uses yfinance if installed; degrades gracefully to None so the bot still runs without it.
A short TTL cache protects Yahoo from being hammered when the loop ticks fast (anti-ban).
"""
from __future__ import annotations

import time

try:
    import yfinance as yf
    _HAVE_YF = True
except Exception:  # pragma: no cover
    _HAVE_YF = False

_CACHE: dict = {}


def _cached(key, ttl: float, producer):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = producer()
    _CACHE[key] = (now, val)
    return val


def have_prices() -> bool:
    return _HAVE_YF


def _last_price(ticker: str):
    if not _HAVE_YF:
        return None
    try:
        h = yf.Ticker(ticker).history(period="1d", interval="5m")
        if len(h):
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def last_price(ticker: str):
    return _cached(("price", ticker), 300, lambda: _last_price(ticker))


def _dollar_volume(ticker: str):
    if not _HAVE_YF:
        return None
    try:
        h = yf.Ticker(ticker).history(period="5d")
        if len(h):
            return float((h["Close"] * h["Volume"]).mean())
    except Exception:
        pass
    return None


def dollar_volume(ticker: str):
    return _cached(("dv", ticker), 3600, lambda: _dollar_volume(ticker))


def _runup(ticker: str):
    if not _HAVE_YF:
        return None
    try:
        h = yf.Ticker(ticker).history(period="1d", interval="5m")
        if len(h) >= 2:
            return float((h["Close"].iloc[-1] / h["Open"].iloc[0] - 1) * 100)
    except Exception:
        pass
    return None


def runup_pct_today(ticker: str):
    """How much it has already moved today (earliness gate). Cached 5 min."""
    return _cached(("runup", ticker), 300, lambda: _runup(ticker))


def _regime(proxy: str, lookback_days: int):
    if not _HAVE_YF:
        return False
    try:
        h = yf.Ticker(proxy).history(period=f"{lookback_days + 5}d")
        if len(h) >= lookback_days:
            ma = float(h["Close"].tail(lookback_days).mean())
            return float(h["Close"].iloc[-1]) < ma
    except Exception:
        pass
    return False


def regime_downtrend(proxy: str, lookback_days: int) -> bool:
    """True if the broad tape is below its lookback average. Cached 10 min."""
    return _cached(("regime", proxy, lookback_days), 600, lambda: _regime(proxy, lookback_days))


def forward_return_pct(ticker: str, entry_ts: float, hold_days: int):
    """Realized return from entry to ~hold_days later — for resolving shadow outcomes (not cached)."""
    if not _HAVE_YF:
        return None
    if time.time() - entry_ts < hold_days * 86400:
        return None
    try:
        h = yf.Ticker(ticker).history(period=f"{hold_days + 10}d")
        if len(h) >= 2:
            return float((h["Close"].iloc[-1] / h["Close"].iloc[0] - 1) * 100)
    except Exception:
        pass
    return None
