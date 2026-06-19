"""Price data for earliness checks, regime filter, and shadow outcome scoring.

Uses yfinance if installed; degrades gracefully to None so the bot still runs without it.
"""
from __future__ import annotations

import time

try:
    import yfinance as yf
    _HAVE_YF = True
except Exception:  # pragma: no cover
    _HAVE_YF = False


def have_prices() -> bool:
    return _HAVE_YF


def last_price(ticker: str) -> float | None:
    if not _HAVE_YF:
        return None
    try:
        h = yf.Ticker(ticker).history(period="1d", interval="5m")
        if len(h):
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def dollar_volume(ticker: str) -> float | None:
    if not _HAVE_YF:
        return None
    try:
        h = yf.Ticker(ticker).history(period="5d")
        if len(h):
            return float((h["Close"] * h["Volume"]).mean())
    except Exception:
        pass
    return None


def runup_pct_today(ticker: str) -> float | None:
    """How much has it already moved today? Used for the earliness gate."""
    if not _HAVE_YF:
        return None
    try:
        h = yf.Ticker(ticker).history(period="1d", interval="5m")
        if len(h) >= 2:
            return float((h["Close"].iloc[-1] / h["Open"].iloc[0] - 1) * 100)
    except Exception:
        pass
    return None


def regime_downtrend(proxy: str, lookback_days: int) -> bool:
    """True if the broad tape is below its lookback average (don't buy longs into a downtrend)."""
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


def forward_return_pct(ticker: str, entry_ts: float, hold_days: int) -> float | None:
    """Realized return from entry to ~hold_days later — for resolving shadow outcomes."""
    if not _HAVE_YF:
        return None
    if time.time() - entry_ts < hold_days * 86400:
        return None  # not enough time has passed yet
    try:
        h = yf.Ticker(ticker).history(period=f"{hold_days + 10}d")
        if len(h) >= 2:
            return float((h["Close"].iloc[-1] / h["Close"].iloc[0] - 1) * 100)
    except Exception:
        pass
    return None
