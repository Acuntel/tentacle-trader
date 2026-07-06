"""Fundamentals, float & short interest — how VIOLENTLY a name can move, and whether it's real.

A catalyst on a small-float, heavily-shorted name moves multiples of the same catalyst on a mega-cap;
a 'catalyst' on a cash-burning zombie is a trap. We pull yfinance's free fundamentals snapshot
(short interest, float, valuation, margins, growth, analyst target) — cached long since fundamentals
move slowly — only for names that already cleared the gate. Output: a SQUEEZE gauge, a QUALITY gauge,
and the raw feature dict the pattern model (Sprint 4) learns on. Degrades to neutral when unavailable.
"""
from __future__ import annotations

from . import prices

try:
    import yfinance as yf
    _YF = True
except Exception:  # pragma: no cover
    _YF = False


def _info(ticker: str):
    if not _YF:
        return None
    try:
        info = yf.Ticker(ticker).info
        return info if (info and len(info) >= 5) else None
    except Exception:
        return None


def _read(ticker: str):
    info = _info(ticker)
    if not info:
        return None
    g = info.get
    float_shares = g("floatShares") or g("sharesOutstanding")
    short_pct = g("shortPercentOfFloat")
    days_cover = g("shortRatio")
    s_now, s_prev = g("sharesShort"), g("sharesShortPriorMonth")
    short_change = ((s_now - s_prev) / s_prev) if (s_now is not None and s_prev) else None
    fpe = g("forwardPE")
    margin = g("profitMargins")
    rev_g = g("revenueGrowth")
    mcap = g("marketCap")
    price = prices.last_price(ticker) or g("currentPrice")
    tgt = g("targetMeanPrice")
    target_upside = round((tgt / price - 1) * 100, 1) if (tgt and price) else None

    # SQUEEZE potential 0..1 — short interest + days-to-cover + rising shorts + small float
    sq = 0.0
    if short_pct:
        sq += min(0.5, float(short_pct) * 2.5)        # 20% of float short -> +0.5
    if days_cover:
        sq += min(0.25, float(days_cover) / 40)       # high days-to-cover -> slow to unwind
    if short_change and short_change > 0:
        sq += 0.15                                    # shorts piling in -> more fuel
    if float_shares and float_shares < 50e6:
        sq += 0.15                                    # small float amplifies any catalyst
    squeeze = round(min(1.0, sq), 2)

    # QUALITY 0..1 — the zombie filter (profitable + growing + not absurdly valued)
    q = 0.5
    if margin is not None:
        q += 0.2 if margin > 0 else -0.2
    if rev_g is not None:
        q += 0.15 if rev_g > 0.05 else (-0.15 if rev_g < 0 else 0.0)
    if fpe is not None and fpe > 0:
        q += 0.1 if fpe < 40 else -0.1
    quality = round(max(0.0, min(1.0, q)), 2)

    return {"market_cap": mcap, "float_shares": float_shares, "short_pct_float": short_pct,
            "days_to_cover": days_cover, "short_change": round(short_change, 3) if short_change else None,
            "fwd_pe": fpe, "profit_margin": margin, "rev_growth": rev_g, "target_upside": target_upside,
            "squeeze": squeeze, "quality": quality}


def read(ticker: str):
    """Cached 6h — fundamentals move slowly, and yf.info is heavy/rate-limited."""
    return prices._cached(("fund", ticker), 21600, lambda: _read(ticker))


def _read_cached(ticker: str, max_age: int = 21600):
    """Allow callers to request fresher data when learning velocity matters."""
    return prices._cached(("fund", ticker), max_age, lambda: _read(ticker))


def features(ticker: str) -> dict:
    """Compact feature dict for the learned pattern model (Sprint 4). Always returns a dict."""
    r = read(ticker)
    if not r:
        return {}
    fs = r.get("float_shares") or 0
    return {"small_float": bool(fs and fs < 50e6), "micro_cap": bool((r.get("market_cap") or 1e12) < 3e8),
            "high_si": bool((r.get("short_pct_float") or 0) > 0.15), "squeeze": r["squeeze"],
            "quality": r["quality"], "profitable": bool((r.get("profit_margin") or 0) > 0),
            "growing": bool((r.get("rev_growth") or 0) > 0.05)}


def entry_read(ticker: str, cfg: dict) -> tuple[float, list[str]]:
    """(score_multiplier, notes). Squeeze fuel boosts a confirmed long; a clear zombie shrinks it."""
    r = read(ticker)
    if not r:
        return 1.0, []
    notes = []
    if r["short_pct_float"] is not None:
        notes.append(f"short {r['short_pct_float']*100:.0f}% float / {r['days_to_cover']}d cover")
    if r["float_shares"]:
        notes.append(f"float {r['float_shares']/1e6:.0f}M")
    if r["rev_growth"] is not None:
        notes.append(f"rev growth {r['rev_growth']*100:+.0f}%")
    if r["target_upside"] is not None:
        notes.append(f"analyst target {r['target_upside']:+.0f}%")
    mult = 1.0 + cfg.get("squeeze_boost", 0.2) * r["squeeze"]
    if r["quality"] < cfg.get("zombie_quality_floor", 0.3):
        mult *= 0.9                                   # cash-burning zombie -> trust the 'catalyst' less
        notes.append("⚠ low fundamental quality")
    if r["squeeze"] > 0.7 and r["quality"] < 0.3:
        mult *= 0.85                                  # avoid high-squeeze junk traps
        notes.append("⚠ squeeze trap")
    return round(max(0.7, min(1.3, mult)), 3), notes
