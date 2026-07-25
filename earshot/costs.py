"""Trading-friction model — the honesty layer between shadow fills and real fills.

Shadow/explore trades fill at the last cached price, which assumes zero spread and zero
slippage. On the small/under-covered names the EDGE doctrine hunts, a real round trip costs
0.1–1.6%+ — the same order as the ~1%/trade edge the forward ledgers show (2026-07-25). So
every resolved trade's return/alpha is debited a liquidity-tiered round-trip cost BEFORE it
trains mastery, patterns, weights, or the P(win) model: an edge that only exists gross of
costs is not an edge, and the graduation ladder must never promote one.

Tiered on DOLLAR VOLUME (liquidity is what sets the spread; already TTL-cached in prices.py —
no extra API surface). Configured, not learned: the model cannot reason its costs away.
Returns 0 until configure() is called, so offline tools (backtest/sim) keep their own
accounting unchanged.
"""
from __future__ import annotations

from . import prices

_cfg: dict | None = None

# [min daily $ volume, cost %/side] — approx half-spread + slippage for a $25-500 order
_DEFAULT_TIERS = [[500e6, 0.03], [50e6, 0.08], [10e6, 0.20], [5e6, 0.40], [0, 0.80]]


def configure(cfg: dict) -> dict:
    global _cfg
    _cfg = dict(cfg.get("costs") or {})
    _cfg.setdefault("enabled", True)
    _cfg.setdefault("default_side_pct", 0.30)
    _cfg.setdefault("tiers", _DEFAULT_TIERS)
    return _cfg


def per_side_pct(ticker: str) -> float:
    if not _cfg or not _cfg.get("enabled", True):
        return 0.0
    try:
        dv = prices.dollar_volume(ticker)
    except Exception:
        dv = None
    if dv is None:
        return float(_cfg["default_side_pct"])
    for min_dv, pct in _cfg["tiers"]:
        if dv >= float(min_dv):
            return float(pct)
    return float(_cfg["default_side_pct"])


def round_trip_pct(ticker: str) -> float:
    """The full entry+exit friction debit applied to a resolved trade's return/alpha."""
    return round(2.0 * per_side_pct(ticker), 3)
