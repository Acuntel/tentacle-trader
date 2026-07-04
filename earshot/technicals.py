"""Price action — candlesticks + indicators tell the bot WHEN to enter and WHEN to exit.

The catalyst engine decides WHAT to trade; the chart decides WHEN. We only read daily candles for
a name that ALREADY cleared the catalyst gate (so we price-check qualifiers, not 20k tickers — the
same anti-ban discipline as prices.py) plus the handful of open positions. Pure-python pattern math,
no TA-Lib / C deps. Output: a bias in [-1, +1], the patterns we saw, and explicit enter/exit reads.
"""
from __future__ import annotations

import math

from . import prices

try:
    import yfinance as yf
    _YF = True
except Exception:  # pragma: no cover
    _YF = False

BULL_REVERSALS = {"hammer", "bullish_engulfing", "morning_star", "piercing",
                  "three_white_soldiers", "bullish_harami", "tweezer_bottom"}
BEAR_REVERSALS = {"shooting_star", "hanging_man", "bearish_engulfing", "evening_star",
                  "dark_cloud", "three_black_crows", "bearish_harami", "tweezer_top"}


def _fetch(ticker: str, days: int):
    if not _YF:
        return None
    try:
        h = yf.Ticker(ticker).history(period=f"{days}d")
        if len(h) < 6:
            return None
        o, hi, lo, cl, v = [], [], [], [], []
        for oo, hh, ll, cc, vv in zip(h["Open"], h["High"], h["Low"], h["Close"], h["Volume"]):
            oo, hh, ll, cc, vv = float(oo), float(hh), float(ll), float(cc), float(vv)
            if not all(math.isfinite(x) for x in (oo, hh, ll, cc)):
                continue                                  # drop NaN/inf rows (yfinance holiday/partial gaps)
            o.append(oo); hi.append(hh); lo.append(ll); cl.append(cc)
            v.append(vv if math.isfinite(vv) else 0.0)
        return {"o": o, "h": hi, "l": lo, "c": cl, "v": v} if len(cl) >= 6 else None
    except Exception:
        return None


def ohlc(ticker: str, days: int = 60):
    """Daily OHLCV, cached 30 min (anti-ban)."""
    return prices._cached(("ohlc", ticker, days), 1800, lambda: _fetch(ticker, days))


# ---- indicators ----
def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def rsi(c, n=14):
    if len(c) < n + 1:
        return None
    g = s = 0.0
    for i in range(len(c) - n, len(c)):
        ch = c[i] - c[i - 1]
        g += max(ch, 0.0); s += max(-ch, 0.0)
    if g == 0 and s == 0:
        return 50.0
    if s == 0:
        return 100.0
    rs = (g / n) / (s / n)
    return 100 - 100 / (1 + rs)


def atr(d, n=14):
    h, l, c = d["h"], d["l"], d["c"]
    if len(c) < n + 1:
        return None
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
           for i in range(len(c) - n, len(c))]
    return sum(trs) / n


def _ema(xs, n):
    k = 2 / (n + 1)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def macd_line(c):
    return (_ema(c, 12) - _ema(c, 26)) if len(c) >= 26 else None


# ---- candlestick detection (operates on the most recent candles) ----
def _candle(d, i):
    o, h, l, c = d["o"][i], d["h"][i], d["l"][i], d["c"][i]
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return o, h, l, c, rng, body, upper, lower


def patterns(d) -> tuple[list[str], list[str]]:
    """Return (bullish_patterns, bearish_patterns) detected on the latest 1-3 daily candles."""
    bull, bear = [], []
    n = len(d["c"])
    if n < 3:
        return bull, bear
    o1, h1, l1, c1, r1, b1, u1, lo1 = _candle(d, -1)   # latest
    o2, h2, l2, c2, r2, b2, u2, lo2 = _candle(d, -2)   # prior
    o3, h3, l3, c3, r3, b3, u3, lo3 = _candle(d, -3)
    up_prior = c2 < o2 or c3 < o3                       # context: was it falling into this candle
    down_prior = c2 > o2 or c3 > o3

    # single-candle
    if b1 <= 0.1 * r1:
        (bull if c1 >= o1 else bear).append("doji")
    if lo1 >= 2 * b1 and u1 <= b1 and b1 > 0:
        (bull if up_prior else bear).append("hammer" if up_prior else "hanging_man")
    if u1 >= 2 * b1 and lo1 <= b1 and b1 > 0:
        (bear if down_prior else bull).append("shooting_star" if down_prior else "inverted_hammer")
    if b1 >= 0.9 * r1:
        (bull if c1 > o1 else bear).append("marubozu")

    # two-candle
    if c2 < o2 and c1 > o1 and c1 >= o2 and o1 <= c2 and b1 > b2:
        bull.append("bullish_engulfing")
    if c2 > o2 and c1 < o1 and o1 >= c2 and c1 <= o2 and b1 > b2:
        bear.append("bearish_engulfing")
    if c2 < o2 and c1 > o1 and o1 < c2 and c1 > (o2 + c2) / 2 and c1 < o2:
        bull.append("piercing")
    if c2 > o2 and c1 < o1 and o1 > c2 and c1 < (o2 + c2) / 2 and c1 > o2:
        bear.append("dark_cloud")
    if c2 < o2 and b1 < b2 * 0.6 and max(o1, c1) <= o2 and min(o1, c1) >= c2:
        bull.append("bullish_harami")
    if c2 > o2 and b1 < b2 * 0.6 and max(o1, c1) <= c2 and min(o1, c1) >= o2:
        bear.append("bearish_harami")
    if abs(l1 - l2) <= 0.0015 * c1 and c2 < o2 and c1 > o1:
        bull.append("tweezer_bottom")
    if abs(h1 - h2) <= 0.0015 * c1 and c2 > o2 and c1 < o1:
        bear.append("tweezer_top")

    # three-candle
    if c3 < o3 and b2 <= 0.3 * r2 and c1 > o1 and c1 > (o3 + c3) / 2:
        bull.append("morning_star")
    if c3 > o3 and b2 <= 0.3 * r2 and c1 < o1 and c1 < (o3 + c3) / 2:
        bear.append("evening_star")
    if c1 > o1 and c2 > o2 and c3 > o3 and c1 > c2 > c3:
        bull.append("three_white_soldiers")
    if c1 < o1 and c2 < o2 and c3 < o3 and c1 < c2 < c3:
        bear.append("three_black_crows")
    return bull, bear


def read(ticker: str) -> dict | None:
    """A full price-action read for one ticker: bias, trend, RSI, and the patterns seen."""
    d = ohlc(ticker)
    if not d:
        return None
    c = d["c"]
    bull, bear = patterns(d)
    r = rsi(c)
    s20, s50 = sma(c, 20), sma(c, 50)
    price = c[-1]
    vol_ratio = (d["v"][-1] / (sma(d["v"], 20) or d["v"][-1])) if sma(d["v"], 20) else 1.0
    trend = "up" if (s50 and price > s50) else ("down" if s50 else "flat")

    bias = 0.0
    bias += 0.25 * len(bull) - 0.25 * len(bear)
    if r is not None:
        if r < 30:
            bias += 0.2                      # oversold -> mean-revert up
        elif r > 70:
            bias -= 0.25                     # overbought -> exhaustion risk
    if trend == "up":
        bias += 0.15
    elif trend == "down":
        bias -= 0.15
    if s20 and price > s20:
        bias += 0.1
    if vol_ratio > 1.5 and bull:
        bias += 0.1                          # volume confirms the bullish candle
    bias = max(-1.0, min(1.0, bias))
    return {"bias": round(bias, 3), "rsi": None if r is None else round(r, 1), "trend": trend,
            "vol_ratio": round(vol_ratio, 2), "bull": bull, "bear": bear,
            "near_high": price >= max(d["h"][-20:]) * 0.985}


def entry_read(ticker: str, cfg: dict) -> tuple[bool, float, list[str]]:
    """(allow_entry, score_multiplier, reasons). Degrades to neutral when no chart data."""
    r = read(ticker)
    if not r:
        return True, 1.0, []
    notes, mult, allow = [], 1.0 + 0.18 * r["bias"], True
    if r["bull"]:
        notes.append("bullish candles: " + ", ".join(r["bull"]))
    if r["bear"]:
        notes.append("bearish candles: " + ", ".join(r["bear"]))
    notes.append(f"RSI {r['rsi']} / trend {r['trend']} / vol x{r['vol_ratio']}")
    # veto: don't BUY into a fresh bearish reversal or an overbought, extended top
    if cfg.get("entry_veto_on_bearish_reversal", True):
        bearish_rev = any(p in BEAR_REVERSALS for p in r["bear"])
        if bearish_rev and (r["near_high"] or (r["rsi"] or 0) > cfg.get("rsi_overbought", 70)):
            allow = False
            notes.append("VETO: bearish reversal at the highs / overbought — wait for a better entry")
    return allow, round(max(0.55, min(1.2, mult)), 3), notes


def exit_read(ticker: str, cfg: dict) -> tuple[bool, str]:
    """(should_exit, reason). A bearish reversal (+ overbought or trend break) says take the exit."""
    r = read(ticker)
    if not r:
        return False, ""
    if not cfg.get("exit_on_bearish_reversal", True):
        return False, ""
    bearish_rev = [p for p in r["bear"] if p in BEAR_REVERSALS]
    if bearish_rev and ((r["rsi"] or 0) >= cfg.get("rsi_overbought", 70) or r["trend"] == "down"):
        return True, "technical:" + bearish_rev[0]
    return False, ""
