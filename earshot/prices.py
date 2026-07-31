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


def _last_price_ext(ticker: str, max_age_min: float = 20.0):
    """A REAL pre/post-market print (prepost bars), no older than max_age_min. Returns None when
    nothing fresh is trading off-hours — an honest 'no fill possible', never a stale regular
    close dressed up as a quote. Thin names simply don't fill until the open."""
    if not _HAVE_YF:
        return None
    try:
        h = yf.Ticker(ticker).history(period="1d", interval="1m", prepost=True)
        if len(h) and time.time() - h.index[-1].timestamp() <= max_age_min * 60:
            return float(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


def last_price(ticker: str, extended: bool = False):
    if extended:
        return _cached(("price_ext", ticker), 120, lambda: _last_price_ext(ticker))
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
        days = min(400, max(hold_days + 10, int((time.time() - entry_ts) / 86400) + 6))
        h = yf.Ticker(ticker).history(period=f"{days}d")
        if len(h) >= 2:
            pos = None
            for i, ts in enumerate(h.index):
                if ts.timestamp() >= entry_ts:
                    pos = i
                    break
            # AUDIT FIX: mirror _mkt_return — the first daily bar ON/AFTER a mid-session entry is
            # TOMORROW, so the stock leg skipped day-1 while the SPY leg (fixed 07-06) included it:
            # mismatched alpha windows. Step back to the ENTRY day's close; window sized by elapsed
            # time (a late-resolving position used to fall off the front of a hold_days+10 window).
            if pos is not None and pos > 0:
                pos -= 1
            if pos is not None and pos < len(h):
                return float((h["Close"].iloc[-1] / h["Close"].iloc[pos] - 1) * 100)
    except Exception:
        pass
    return None


def _mkt_return(proxy: str, start_ts: float):
    """AUDIT FIX (2026-07-06): the alpha benchmark's window must match the STOCK leg's window (entry ts
    -> live price now). Daily bars can't do that for fresh entries — the first daily bar ON/AFTER a
    mid-session entry is stamped TOMORROW, so intraday resolutions returned None (alpha silently fell
    back to the RAW return: the 1h-horizon labels weren't alpha at all) and multi-day windows skipped
    ALL of day-1's market drift. Short windows now use 5-minute SPY bars aligned to the entry minute;
    longer windows use daily bars anchored one bar EARLIER (the entry day's close, not the next day's)."""
    if not _HAVE_YF:
        return None
    try:
        span_days = (time.time() - start_ts) / 86400
        if span_days <= 5:                               # match last_price()'s intraday 5m marking
            h = yf.Ticker(proxy).history(period=f"{max(2, int(span_days) + 2)}d", interval="5m")
            closes = h["Close"]
            if len(closes) < 2:
                return None
            pos = None
            for i, ts in enumerate(h.index):             # first 5m bar on/after entry
                if ts.timestamp() >= start_ts:
                    pos = i
                    break
            if pos is None:                              # entry after the last bar (e.g. after close)
                pos = len(closes) - 1
            c0, c1 = float(closes.iloc[pos]), float(closes.iloc[-1])
            return (c1 / c0 - 1) * 100 if c0 else None
        days = min(400, max(6, int(span_days) + 6))
        h = yf.Ticker(proxy).history(period=f"{days}d")
        closes = h["Close"]
        if len(closes) < 2:
            return None
        pos = None
        for i, ts in enumerate(h.index):                 # first bar on/after entry...
            if ts.timestamp() >= start_ts:
                pos = i
                break
        if pos is None or pos >= len(closes):
            return None
        if pos > 0:
            pos -= 1                                     # ...then step back to the ENTRY day's close
        c0 = float(closes.iloc[pos])
        c1 = float(closes.iloc[-1])
        return (c1 / c0 - 1) * 100 if c0 != 0 else None
    except Exception:
        return None


def market_return_since(start_ts: float, proxy: str = "SPY"):
    """Broad-market (SPY) return from ~start_ts to now. Used to ALPHA-adjust shadow outcomes — what we
    learn from is the trade's return MINUS the market's, so the bot can't mistake a rising tide for
    skill. Cached ~10 min by hour-bucketed start, degrades to None (then alpha falls back to raw)."""
    return _cached(("mktret", proxy, int(start_ts // 3600)), 600,
                   lambda: _mkt_return(proxy, start_ts))
