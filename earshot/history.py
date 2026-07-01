"""Ten years of daily price history — the old data the bot studies and simulates on 24/7.

Universe = S&P 500 (pulled live) + the hand-picked meme/quantum/biotech watchlist (~1,200 names).
Bars are cached one CSV per ticker under state/history/ (on the Railway volume, so a decade only
downloads once and survives redeploys). Filling is INCREMENTAL and rate-safe: each call fetches a
small slice, so the loop never blocks and Yahoo never gets hammered. Degrades to empty if yfinance
is absent — the rest of the bot keeps running.
"""
from __future__ import annotations

import json
import logging
import os
import time

try:
    import yfinance as yf
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)   # mute per-ticker "possibly delisted" spam
    _HAVE = True
except Exception:  # pragma: no cover
    _HAVE = False

from .sources.fleet import _sp500, CORE_TICKERS, EXTRA_TICKERS

HIST_DIR = "state/history"
STALE_DAYS = 7
DEAD_PATH = "state/dead_tickers.json"
DEAD_AFTER = 3                       # consecutive empty fetches -> treat as delisted; stop requesting it

_DEAD: dict[str, int] = {}
_DEAD_LOADED = False


def _load_dead() -> dict:
    global _DEAD, _DEAD_LOADED
    if not _DEAD_LOADED:
        try:
            _DEAD = json.load(open(DEAD_PATH))
        except Exception:
            _DEAD = {}
        _DEAD_LOADED = True
    return _DEAD


def _save_dead() -> None:
    try:
        os.makedirs(os.path.dirname(DEAD_PATH) or ".", exist_ok=True)
        json.dump(_DEAD, open(DEAD_PATH, "w"))
    except Exception:
        pass


def is_dead(ticker: str) -> bool:
    """A name that returned no data DEAD_AFTER times running — almost certainly delisted/renamed."""
    return _load_dead().get(ticker, 0) >= DEAD_AFTER


def _mark_fail(ticker: str) -> None:
    d = _load_dead()
    d[ticker] = d.get(ticker, 0) + 1
    _save_dead()


def _mark_ok(ticker: str) -> None:
    d = _load_dead()
    if ticker in d:                 # any successful fetch clears the strikes (handles transient blips)
        del d[ticker]
        _save_dead()


def have_yf() -> bool:
    return _HAVE


_UNI: list[str] = []


def universe() -> list[str]:
    """S&P 500 (live, falls back to a core list) + the watchlist, de-duped — the names we study.
    Memoized per process so topping up the cache never re-scrapes the S&P list."""
    global _UNI
    if not _UNI:
        sp = _sp500() or list(CORE_TICKERS)
        _UNI = [t for t in dict.fromkeys(sp + list(EXTRA_TICKERS)) if not is_dead(t)]
    return _UNI


def _path(ticker: str) -> str:
    return os.path.join(HIST_DIR, ticker.replace("/", "-") + ".csv")


def _fresh(ticker: str) -> bool:
    p = _path(ticker)
    try:
        return (time.time() - os.path.getmtime(p)) < STALE_DAYS * 86400 and os.path.getsize(p) > 64
    except OSError:
        return False


def _fetch_one(ticker: str, years: int) -> int:
    """Download ~`years` of daily closes for one ticker and cache them. Returns rows written (0 = fail)."""
    if not _HAVE:
        return 0
    try:
        h = yf.Ticker(ticker).history(period=f"{years}y", interval="1d", auto_adjust=True)
    except Exception:
        return 0
    if h is None or not len(h):
        return 0
    os.makedirs(HIST_DIR, exist_ok=True)
    rows = []
    for ts, close in h["Close"].items():
        try:
            rows.append(f"{ts.date().isoformat()},{float(close):.4f}")
        except Exception:
            continue
    if not rows:
        return 0
    with open(_path(ticker), "w", newline="") as f:
        f.write("\n".join(rows))
    return len(rows)


def ensure_slice(years: int = 10, limit: int = 40, tickers: list[str] | None = None) -> dict:
    """Top up the cache by at most `limit` missing/stale tickers. Call this repeatedly (e.g. each
    study pass) and the full decade fills in over time without ever blocking the loop."""
    if not _HAVE:
        return {"fetched": 0, "have": 0, "total": 0, "note": "no yfinance"}
    uni = tickers or universe()
    todo = [t for t in uni if not _fresh(t) and not is_dead(t)][:limit]
    fetched = 0
    for t in todo:
        if _fetch_one(t, years):
            fetched += 1
            _mark_ok(t)
        else:
            _mark_fail(t)                       # 3 strikes -> the reaper drops it from the universe
        time.sleep(0.2)                         # polite spacing — anti-ban
    have = sum(1 for t in uni if _fresh(t))
    return {"fetched": fetched, "have": have, "total": len(uni)}


def coverage(tickers: list[str] | None = None) -> dict:
    uni = tickers or universe()
    have = sum(1 for t in uni if _fresh(t))
    return {"have": have, "total": len(uni), "pct": round(100 * have / max(1, len(uni)), 1)}


def load(ticker: str) -> list[tuple[str, float]]:
    """Return [(date, close), ...] for a cached ticker (empty if not yet downloaded)."""
    try:
        with open(_path(ticker)) as f:
            out = []
            for line in f:
                d, c = line.strip().split(",")
                out.append((d, float(c)))
            return out
    except Exception:
        return []


def cached_tickers() -> list[str]:
    """Tickers that already have a decade on disk — what the sim can replay right now."""
    try:
        return [f[:-4] for f in os.listdir(HIST_DIR) if f.endswith(".csv")]
    except OSError:
        return []
