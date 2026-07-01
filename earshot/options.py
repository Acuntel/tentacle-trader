"""Options & volatility intelligence — where informed money positions BEFORE the catalyst.

The options market prices in events: the implied move, put/call skew, unusual volume, max-pain, and
how rich IV is vs realized (is a move EXPECTED?). We read the chain (yfinance, free) only for names
that already cleared the catalyst gate + open positions (anti-ban). Output: a directional read that
CONFIRMS or vetoes entries and flags exits. Degrades to neutral when chains are unavailable.
"""
from __future__ import annotations

from math import sqrt, isfinite
from statistics import pstdev

from . import prices, technicals

try:
    import yfinance as yf
    _YF = True
except Exception:  # pragma: no cover
    _YF = False


def _num(x) -> float:
    """NaN-safe float (yfinance volume/OI cells are often NaN, which `x or 0` does NOT catch)."""
    try:
        v = float(x)
        return v if v == v else 0.0      # NaN != NaN
    except (TypeError, ValueError):
        return 0.0


def _realized_vol(ticker: str):
    d = technicals.ohlc(ticker, 40)
    if not d or len(d["c"]) < 21:
        return None
    c = d["c"]
    rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - 20, len(c)) if c[i - 1]]
    rets = [r for r in rets if isfinite(r)]               # never let a NaN reach pstdev
    return pstdev(rets) * sqrt(252) if len(rets) >= 10 else None


def _chain(ticker: str):
    if not _YF:
        return None
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        if not exps:
            return None
        ch = t.option_chain(exps[0])                 # nearest expiry only — keep the call count low
        return exps[0], ch.calls.to_dict("records"), ch.puts.to_dict("records")
    except Exception:
        return None


def _mid(row: dict) -> float:
    b, a = _num(row.get("bid")), _num(row.get("ask"))
    return (b + a) / 2 if (b > 0 and a > 0) else _num(row.get("lastPrice"))


def _read(ticker: str):
    spot = prices.last_price(ticker)
    c = _chain(ticker)
    if not spot or not c:
        return None
    expiry, calls, puts = c
    if not calls or not puts:
        return None

    atm_c = min(calls, key=lambda r: abs(float(r["strike"]) - spot))
    atm_p = min(puts, key=lambda r: abs(float(r["strike"]) - spot))
    straddle = _mid(atm_c) + _mid(atm_p)
    implied_move = round(straddle / spot * 100, 2) if straddle else None
    ivs = [_num(x.get("impliedVolatility")) for x in (atm_c, atm_p)]
    ivs = [x for x in ivs if x > 0]
    atm_iv = sum(ivs) / len(ivs) if ivs else None

    cv = sum(_num(r.get("volume")) for r in calls)
    pv = sum(_num(r.get("volume")) for r in puts)
    coi = sum(_num(r.get("openInterest")) for r in calls)
    poi = sum(_num(r.get("openInterest")) for r in puts)
    pcr_vol = round(pv / cv, 2) if cv else None
    pcr_oi = round(poi / coi, 2) if coi else None

    def avg_iv(rows, lo, hi):
        xs = [_num(r.get("impliedVolatility")) for r in rows
              if lo <= float(r.get("strike", 0)) / spot <= hi and _num(r.get("impliedVolatility")) > 0]
        return sum(xs) / len(xs) if xs else None

    put_iv, call_iv = avg_iv(puts, 0.88, 0.97), avg_iv(calls, 1.03, 1.12)
    skew = round((put_iv - call_iv) * 100, 1) if (put_iv and call_iv) else None  # >0 = fear (puts bid up)

    def unusual(rows):
        return sum(1 for r in rows
                   if _num(r.get("volume")) > max(_num(r.get("openInterest")), 200))
    ucall, uput = unusual(calls), unusual(puts)

    strikes = sorted({float(r["strike"]) for r in calls + puts})

    def payout(s):
        return (sum(_num(r.get("openInterest")) * max(0.0, s - float(r["strike"])) for r in calls)
                + sum(_num(r.get("openInterest")) * max(0.0, float(r["strike"]) - s) for r in puts))
    max_pain = min(strikes, key=payout) if strikes else None
    rv = _realized_vol(ticker)
    iv_rv = round(atm_iv / rv, 2) if (atm_iv and rv) else None

    bias = 0.0
    if pcr_vol is not None:
        bias += 0.2 if pcr_vol < 0.7 else (-0.2 if pcr_vol > 1.3 else 0.0)
    if skew is not None:
        bias += -0.15 if skew > 3 else (0.1 if skew < -1 else 0.0)
    if ucall >= 3 and ucall > uput * 1.5:
        bias += 0.2
    if uput >= 3 and uput > ucall * 1.5:
        bias -= 0.2
    bias = max(-1.0, min(1.0, bias))
    return {"expiry": expiry, "implied_move": implied_move, "atm_iv": round(atm_iv, 3) if atm_iv else None,
            "pcr_vol": pcr_vol, "pcr_oi": pcr_oi, "skew": skew, "unusual_calls": ucall,
            "unusual_puts": uput, "max_pain": max_pain, "iv_rv": iv_rv, "bias": round(bias, 3)}


def read(ticker: str):
    """Cached 30-min options read (anti-ban — option chains are heavy)."""
    return prices._cached(("opt", ticker), 1800, lambda: _read(ticker))


def entry_read(ticker: str, cfg: dict) -> tuple[float, bool, list[str]]:
    """(score_multiplier, is_orthogonal_confirmation, notes)."""
    r = read(ticker)
    if not r:
        return 1.0, False, []
    notes = []
    if r["implied_move"] is not None:
        notes.append(f"implied move ±{r['implied_move']}% by {r['expiry']}")
    if r["pcr_vol"] is not None:
        notes.append(f"P/C vol {r['pcr_vol']}")
    if r["skew"] is not None:
        notes.append(f"skew {r['skew']:+}")
    if r["unusual_calls"] or r["unusual_puts"]:
        notes.append(f"unusual C/P {r['unusual_calls']}/{r['unusual_puts']}")
    if r["iv_rv"] is not None:
        notes.append(f"IV/RV {r['iv_rv']}")
    mult = 1.0 + 0.15 * r["bias"]
    if r["bias"] <= -cfg.get("contra_bias", 0.35):
        mult *= 0.8                                          # heavy put positioning into a long = caution
    confirm = r["bias"] >= cfg.get("confirm_bias", 0.25)     # options agree bullishly = orthogonal pillar
    return round(max(0.6, min(1.25, mult)), 3), confirm, notes


def exit_read(ticker: str, cfg: dict) -> tuple[bool, str]:
    r = read(ticker)
    if r and r["bias"] <= -cfg.get("exit_bias", 0.5):
        return True, "options:bearish_flow"
    return False, ""
